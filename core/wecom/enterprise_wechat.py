"""企业微信消息处理模块"""
import asyncio
import datetime
import mimetypes
import os
import re
import threading
import urllib.parse
import xml.etree.ElementTree as ET
from typing import Optional, Callable, Dict, Any, Awaitable

import aiohttp
from aiohttp import TCPConnector
from fastapi import FastAPI, Request, Response, HTTPException, BackgroundTasks
from pydantic import BaseModel

from core.config import CONFIG, AGENT_CONFIGS
from .crypto import decrypt, verify_signature
from core.logger import get_logger
from core.plugin_manager import PluginManager
from core.redis_client import redis_client
from core.yaml_config import get_config

try:
    from aiohttp_socks import ProxyConnector  # type: ignore
except Exception:  # pragma: no cover
    ProxyConnector = None

# 配置选项
yaml_config = get_config()
ENABLE_MSG_DEDUP = yaml_config.get("wechat.enable_msg_dedup", True)  # 是否启用消息去重
MSG_DEDUP_TTL = yaml_config.get("wechat.msg_dedup_ttl", 30)      # 消息去重标记有效期（秒）
WECHAT_PROXY = yaml_config.get("wechat.proxy")  # 统一代理（http/https 均可复用）
WECHAT_DEBUG = yaml_config.get("wechat.debug", False)


def _normalize_proxy_url(proxy_url: str) -> str:
    """
    统一代理 URL 前缀。
    - s5:// -> socks5://
    - s5h:// -> socks5h://
    """
    p = proxy_url.strip()
    lower = p.lower()
    if lower.startswith("s5://"):
        return "socks5://" + p[5:]
    if lower.startswith("s5h://"):
        return "socks5h://" + p[6:]
    return p


def _get_wechat_proxy_mode() -> tuple[str, Optional[str]]:
    """
    解析 wechat.proxy，返回 (mode, normalized_proxy_url)。
    mode:
      - "none": 未配置
      - "http": http/https 代理（aiohttp 原生 proxy= 支持）
      - "socks": socks5/socks5h 代理（需要 aiohttp-socks connector）
    """
    if not WECHAT_PROXY:
        return ("none", None)
    p = _normalize_proxy_url(str(WECHAT_PROXY))
    lower = p.lower()
    if lower.startswith(("http://", "https://")):
        return ("http", p)
    if lower.startswith(("socks5://", "socks5h://")):
        return ("socks", p)
    raise ValueError(f"不支持的 wechat.proxy 协议前缀: {WECHAT_PROXY}")


def create_wechat_aiohttp_session(*, no_proxy: bool = False) -> aiohttp.ClientSession:
    """
    创建企业微信请求专用 ClientSession。
    - http/https 代理：使用普通 TCPConnector，并在请求时传 proxy 参数
    - socks5 代理：使用 ProxyConnector（aiohttp-socks），请求时不传 proxy 参数
    """
    if no_proxy:
        # 明确禁用代理：用于 gettoken 等必须直连的接口
        connector = TCPConnector(ssl=False)
        return aiohttp.ClientSession(connector=connector)

    mode, proxy_url = _get_wechat_proxy_mode()
    if mode == "socks":
        if ProxyConnector is None:
            raise RuntimeError("检测到 socks5 代理，但未安装 aiohttp-socks，请先安装依赖")
        connector = ProxyConnector.from_url(proxy_url, verify_ssl=False)
        return aiohttp.ClientSession(connector=connector)

    # 默认仍沿用项目现有行为：关闭 SSL 校验（如需生产可替换为 certifi.where()）
    connector = TCPConnector(ssl=False)
    return aiohttp.ClientSession(connector=connector)


def _get_request_proxy() -> Optional[str]:
    """返回 aiohttp 的 proxy 参数（仅 http/https 代理需要）。"""
    mode, proxy_url = _get_wechat_proxy_mode()
    if mode == "http":
        return proxy_url
    return None


def _maybe_append_debug_param(url: str) -> str:
    """
    当 wechat.debug=true 时，给企业微信接口 URL 追加 debug=1。
    保留原有 query，并避免重复追加。
    """
    if not WECHAT_DEBUG:
        return url
    try:
        parts = urllib.parse.urlsplit(url)
        q = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if any(k == "debug" for k, _ in q):
            return url
        q.append(("debug", "1"))
        new_query = urllib.parse.urlencode(q)
        return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path, new_query, parts.fragment))
    except Exception:
        # 兜底：URL 解析失败时不影响主流程
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}debug=1"


async def _wechat_request(
    session: aiohttp.ClientSession,
    method: str,
    url: str,
    handler: Callable[[aiohttp.ClientResponse], Awaitable[Any]],
    *,
    max_retries: int = 3,
    retry_delay: float = 1.0,
    logger_name: str = "wechat_http",
    append_debug_param: bool = True,
    use_proxy: bool = True,
    request_kwargs: Optional[Dict[str, Any]] = None,
) -> Any:
    """
    统一封装企业微信 HTTP 请求，带简单重试。
    handler 负责在单次请求中从响应中读取并返回结果。
    """
    logger = get_logger(logger_name)
    last_error: Optional[Exception] = None

    for attempt in range(1, max_retries + 1):
        try:
            request_url = _maybe_append_debug_param(url) if append_debug_param else url
            kwargs: Dict[str, Any] = {}
            if request_kwargs:
                kwargs.update(request_kwargs)
            async with session.request(
                method,
                request_url,
                proxy=_get_request_proxy() if use_proxy else None,
                **kwargs,
            ) as resp:
                return await handler(resp)
        except Exception as e:
            last_error = e
            logger.error(
                f"企业微信请求异常，method={method}, url={url}, 尝试次数: {attempt}, 错误: {str(e)}"
            )
            if attempt < max_retries:
                await asyncio.sleep(retry_delay)

    # 如果走到这里说明全部重试失败
    if last_error:
        raise last_error
    raise RuntimeError("企业微信请求失败且未知错误")


# 后台主动消息发送顺序控制：为每个接收人维护一个发送队列，保证严格 FIFO
_send_queues: Dict[str, asyncio.Queue] = {}
_queue_workers: Dict[str, asyncio.Task] = {}

# 持久后台事件循环（避免在临时 asyncio.run() 结束后 worker 消失）
_background_loop: Optional[asyncio.AbstractEventLoop] = None
_background_thread: Optional[threading.Thread] = None
_token_lock = threading.Lock()
_token_refresh_locks: Dict[str, threading.Lock] = {}


def _token_cache_key(agent_id: str) -> str:
    return f"wechat:access_token:{agent_id}"


def _token_block_key(agent_id: str) -> str:
    return f"wechat:access_token:block:{agent_id}"

def _ensure_background_loop():
    """确保存在一个持久运行的后台事件循环线程。"""
    global _background_loop, _background_thread
    if _background_loop is not None:
        return
    loop = asyncio.new_event_loop()
    _background_loop = loop

    def _run_loop_forever():
        asyncio.set_event_loop(loop)
        loop.run_forever()

    t = threading.Thread(target=_run_loop_forever, name="wechat-bg-loop", daemon=True)
    _background_thread = t
    t.start()


def _receiver_key(agent_id: str, user: str = "", party: str = "", tag: str = "") -> str:
    return f"agent_id:{agent_id},user:{user},party:{party},tag:{tag}"


async def _send_worker(key: str):
    logger = get_logger("wechat_send_worker")
    queue = _send_queues[key]
    logger.debug(f"发送队列worker已启动，key={key}")
    while True:
        item = await queue.get()
        try:
            # 轻微节流，避免过快触发平台限频
            await asyncio.sleep(0.3)
            await send_wechat_message(
                msg=item.get("msg"),
                agent_id=item["agent_id"],
                user=item.get("user", ""),
                party=item.get("party", ""),
                tag=item.get("tag", ""),
                msgtype=item.get("msgtype", "text"),
                media_id=item.get("media_id"),
                title=item.get("title"),
                description=item.get("description"),
                safe=item.get("safe", 0),
                enable_duplicate_check=item.get("enable_duplicate_check"),
                duplicate_check_interval=item.get("duplicate_check_interval"),
            )
        except Exception as e:
            logger.error(f"发送队列worker异常，key={key}，错误: {str(e)}")
        finally:
            queue.task_done()


async def _ensure_sender_worker_bg(key: str):
    """在后台事件循环中确保队列与worker已创建。"""
    logger = get_logger("wechat_send_worker")
    if key not in _send_queues:
        _send_queues[key] = asyncio.Queue()
        _queue_workers[key] = asyncio.create_task(_send_worker(key))
        logger.debug(f"创建发送队列并启动worker，key={key}")


async def enqueue_active_message(
    agent_id: str,
    msg: Optional[str] = None,
    user: str = "",
    party: str = "",
    tag: str = "",
    msgtype: str = "text",
    media_id: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    safe: int = 0,
    enable_duplicate_check: Optional[int] = None,
    duplicate_check_interval: Optional[int] = None,
):
    """将消息入队到持久后台事件循环，保证 worker 不随临时事件循环结束而退出。"""
    _ensure_background_loop()

    async def _bg_enqueue():
        key = _receiver_key(agent_id, user, party, tag)
        await _ensure_sender_worker_bg(key)
        await _send_queues[key].put({
            "agent_id": agent_id,
            "msg": msg,
            "user": user,
            "party": party,
            "tag": tag,
            "msgtype": msgtype,
            "media_id": media_id,
            "title": title,
            "description": description,
            "safe": safe,
            "enable_duplicate_check": enable_duplicate_check,
            "duplicate_check_interval": duplicate_check_interval,
        })

    # 将实际入队操作投递到后台事件循环，并等待其完成（保证“已入队”语义）
    future = asyncio.run_coroutine_threadsafe(_bg_enqueue(), _background_loop)  # type: ignore[arg-type]
    try:
        running_loop = asyncio.get_running_loop()
        # 在当前事件循环中以非阻塞方式等待线程安全的结果
        await running_loop.run_in_executor(None, future.result)
    except RuntimeError:
        # 当前线程没有事件循环，直接阻塞等待
        future.result()


# 主动发送消息相关
class SendMessageRequest(BaseModel):
    # 兼容文本与媒体消息
    message: Optional[str] = None
    user: str
    party: Optional[str] = None
    tag: Optional[str] = None
    msgtype: Optional[str] = "text"  # text | image | voice | video
    media_id: Optional[str] = None
    title: Optional[str] = None
    description: Optional[str] = None
    safe: Optional[int] = 0
    enable_duplicate_check: Optional[int] = None
    duplicate_check_interval: Optional[int] = None

async def get_access_token(
    session: aiohttp.ClientSession,
    agent_id: str,
    max_retries: int = 3,
    retry_delay: float = 1.0,
) -> Optional[str]:
    logger = get_logger("wechat_auth")
    agent_conf = AGENT_CONFIGS.get(agent_id)
    if not agent_conf:
        logger.error(f"未找到agent_id={agent_id}的配置")
        return None

    # 1) 优先读缓存，避免重复请求 gettoken 接口
    cached_token = redis_client.get(_token_cache_key(agent_id))
    if cached_token:
        return str(cached_token)

    # 2) 若命中过频保护窗口，直接失败返回，避免继续轰炸微信接口
    block_ttl = redis_client.ttl(_token_block_key(agent_id))
    if isinstance(block_ttl, int) and block_ttl > 0:
        logger.warning(f"access_token 获取被限频保护，agent_id: {agent_id}, 剩余阻断: {block_ttl}s")
        return None

    # 3) 同一 agent 在进程内串行刷新 token，防止并发雪崩
    with _token_lock:
        agent_lock = _token_refresh_locks.get(agent_id)
        if agent_lock is None:
            agent_lock = threading.Lock()
            _token_refresh_locks[agent_id] = agent_lock

    got_lock = agent_lock.acquire(timeout=5)
    if not got_lock:
        logger.warning(f"等待access_token刷新锁超时，agent_id: {agent_id}")
        return None
    try:
        # 双重检查，减少并发等待后重复请求
        cached_token = redis_client.get(_token_cache_key(agent_id))
        if cached_token:
            return str(cached_token)

        url = (
            f"https://qyapi.weixin.qq.com/cgi-bin/gettoken?"
            f"corpid={CONFIG['CORP_ID']}&corpsecret={agent_conf['corp_secret']}"
        )

        try:
            # gettoken 明确不走代理：即使全局配置了 socks/http 代理也要直连
            async with create_wechat_aiohttp_session(no_proxy=True) as token_session:
                result = await _wechat_request(
                    token_session,
                    "GET",
                    url,
                    lambda resp: resp.json(content_type=None),
                    max_retries=max_retries,
                    retry_delay=retry_delay,
                    logger_name="wechat_auth",
                    use_proxy=False,
                )
        except Exception as e:
            logger.error(f"获取access_token异常，agent_id: {agent_id}, 错误: {str(e)}")
            return None

        if result.get("errcode") == 0:
            access_token = result.get("access_token")
            if access_token:
                # 企业微信 access_token 有效期通常 7200s，提前刷新避免边界过期
                redis_client.set(_token_cache_key(agent_id), access_token, ex=7000)
            logger.debug(f"获取access_token成功，agent_id: {agent_id}")
            return access_token

        errcode = result.get("errcode")
        errmsg = result.get("errmsg")
        if errcode == 45009:
            # 限频后短暂阻断，防止持续重试造成雪崩
            redis_client.set(_token_block_key(agent_id), "1", ex=60)
        logger.error(
            f"获取access_token失败，agent_id: {agent_id}, 错误码: {errcode}, 错误: {errmsg}"
        )
        return None
    finally:
        agent_lock.release()

async def get_user_info(session: aiohttp.ClientSession, agent_id: str, user_id: str) -> Dict[str, Any]:
    """获取企业微信用户详细信息"""
    logger = get_logger("wechat_user_info")
    agent_conf = AGENT_CONFIGS.get(agent_id)
    if not agent_conf:
        logger.error(f"未找到agent_id={agent_id}的配置")
        return {}
    
    access_token = await get_access_token(session, agent_id)
    if not access_token:
        logger.error(f"获取access_token失败，无法获取用户信息，agent_id: {agent_id}")
        return {}
    
    try:
        url = f"https://qyapi.weixin.qq.com/cgi-bin/user/get?access_token={access_token}&userid={user_id}"
        result = await _wechat_request(
            session,
            "GET",
            url,
            lambda resp: resp.json(content_type=None),
            logger_name="wechat_user_info",
        )
        if result.get('errcode') == 0:
            user_info = {
                "userid": result.get('userid', ''),
                "name": result.get('name', ''),
                "department": result.get('department', []),
                "position": result.get('position', ''),
                "mobile": result.get('mobile', ''),
                "gender": result.get('gender', ''),
                "email": result.get('email', ''),
                "avatar": result.get('avatar', ''),
                "status": result.get('status', ''),
                "enable": result.get('enable', 0)
            }
            logger.debug(f"获取用户信息成功，agent_id: {agent_id}, 用户: {user_id}, 姓名: {user_info['name']}")
            return user_info
        else:
            logger.error(f"获取用户信息失败，agent_id: {agent_id}, 用户: {user_id}, 错误: {result.get('errmsg')}")
            return {}
    except Exception as e:
        logger.error(f"获取用户信息异常，agent_id: {agent_id}, 用户: {user_id}, 错误: {str(e)}")
        return {}

async def get_department_info(session: aiohttp.ClientSession, agent_id: str, dept_id: int) -> Dict[str, Any]:
    """获取企业微信部门信息"""
    logger = get_logger("wechat_dept_info")
    agent_conf = AGENT_CONFIGS.get(agent_id)
    if not agent_conf:
        logger.error(f"未找到agent_id={agent_id}的配置")
        return {}
    
    access_token = await get_access_token(session, agent_id)
    if not access_token:
        logger.error(f"获取access_token失败，无法获取部门信息，agent_id: {agent_id}")
        return {}
    
    try:
        url = f"https://qyapi.weixin.qq.com/cgi-bin/department/get?access_token={access_token}&id={dept_id}"
        result = await _wechat_request(
            session,
            "GET",
            url,
            lambda resp: resp.json(content_type=None),
            logger_name="wechat_dept_info",
        )
        if result.get('errcode') == 0:
            dept_info = {
                "id": result.get('id', ''),
                "name": result.get('name', ''),
                "parentid": result.get('parentid', ''),
                "order": result.get('order', '')
            }
            logger.debug(f"获取部门信息成功，agent_id: {agent_id}, 部门ID: {dept_id}, 名称: {dept_info['name']}")
            return dept_info
        else:
            logger.error(f"获取部门信息失败，agent_id: {agent_id}, 部门ID: {dept_id}, 错误: {result.get('errmsg')}")
            return {}
    except Exception as e:
        logger.error(f"获取部门信息异常，agent_id: {agent_id}, 部门ID: {dept_id}, 错误: {str(e)}")
        return {}

async def get_user_context(session: aiohttp.ClientSession, agent_id: str, user_id: str) -> Dict[str, Any]:
    """获取用户的完整上下文信息，包括用户信息和部门信息"""
    logger = get_logger("wechat_user_context")
    
    # 检查是否配置了获取用户信息
    agent_conf = AGENT_CONFIGS.get(agent_id)
    fetch_user_info = agent_conf.get('fetch_user_info', True) if agent_conf else True
    
    if not fetch_user_info:
        logger.debug(f"配置为不获取用户信息，使用基础上下文，agent_id: {agent_id}, 用户: {user_id}")
        return {
            "userid": user_id,
            "name": "用户",
            "department": [],
            "position": "",
            "mobile": "",
            "gender": "",
            "email": "",
            "avatar": "",
            "status": "",
            "enable": 0,
            "department_names": []
        }
    
    # 获取用户基本信息
    user_info = await get_user_info(session, agent_id, user_id)
    if not user_info:
        logger.warning(f"无法获取用户信息，使用默认上下文，agent_id: {agent_id}, 用户: {user_id}")
        return {
            "userid": user_id,
            "name": "未知用户",
            "department": [],
            "position": "",
            "mobile": "",
            "gender": "",
            "email": "",
            "avatar": "",
            "status": "",
            "enable": 0,
            "department_names": []
        }
    
    # # 获取部门名称信息
    # department_names = []
    # if user_info.get('department'):
    #     for dept_id in user_info['department']:
    #         dept_info = await get_department_info(session, agent_id, dept_id)
    #         if dept_info and dept_info.get('name'):
    #             department_names.append(dept_info['name'])
    
    # 构建完整的用户上下文
    user_context = {
        **user_info,
        # "department_names": department_names
    }
    
    logger.debug(f"构建用户上下文成功，agent_id: {agent_id}, 用户: {user_id}, 姓名: {user_context['name']}")
    return user_context


def _parse_filename_from_content_disposition(content_disposition: str) -> str:
    """
    从 Content-Disposition 里提取 filename。
    示例：attachment; filename="MEDIA_ID.jpg"
    """
    if not content_disposition:
        return ""
    # 宽松匹配 filename="xxx" 或 filename=xxx
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^\";]+)"?', content_disposition, flags=re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return ""


async def upload_temporary_media(
    session: aiohttp.ClientSession,
    agent_id: str,
    file_path: str,
    media_type: str,
) -> Dict[str, Any]:
    """
    上传临时素材（3天有效）。

    对应接口：
    POST https://qyapi.weixin.qq.com/cgi-bin/media/upload?access_token=ACCESS_TOKEN&type=TYPE

    Args:
        session: aiohttp 客户端会话（调用方负责生命周期）
        agent_id: 具体应用的 agent_id（用于拿该应用 access_token）
        file_path: 本地文件路径
        media_type: image|voice|video|file
    """
    logger = get_logger("wechat_media_upload")
    if not file_path:
        return {"errcode": -1, "errmsg": "file_path is required"}
    if media_type not in {"image", "voice", "video", "file"}:
        return {"errcode": -1, "errmsg": f"invalid media_type: {media_type}"}
    if not os.path.exists(file_path):
        return {"errcode": -1, "errmsg": f"file not found: {file_path}"}

    access_token = await get_access_token(session, agent_id)
    if not access_token:
        return {"errcode": -1, "errmsg": "failed to get access_token"}

    url = f"https://qyapi.weixin.qq.com/cgi-bin/media/upload?access_token={access_token}&type={media_type}"
    filename = os.path.basename(file_path)
    guessed_ct = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    # WeChat 要求上传 multipart/form-data，字段名为 media
    form = aiohttp.FormData()
    try:
        file_size = os.path.getsize(file_path)
        with open(file_path, "rb") as f:
            file_bytes = f.read()
            # 显式防御（避免读取失败/截断造成异常响应）
            if len(file_bytes) != file_size:
                logger.warning(f"读取文件大小与实际不一致，预计={file_size}，实际={len(file_bytes)}")

        form.add_field(
            name="media",
            value=file_bytes,
            filename=filename,
            content_type=guessed_ct,
        )

        result = await _wechat_request(
            session,
            "POST",
            url,
            lambda resp: resp.json(content_type=None),
            logger_name="wechat_media_upload",
            request_kwargs={"data": form},
        )
        if isinstance(result, dict) and result.get("errcode") == 0:
            logger.info(f"临时素材上传成功，agent_id={agent_id}, type={media_type}, filename={filename}")
        else:
            errcode = result.get("errcode") if isinstance(result, dict) else None
            errmsg = result.get("errmsg") if isinstance(result, dict) else None
            logger.error(
                f"临时素材上传失败，agent_id={agent_id}, type={media_type}, filename={filename}, "
                f"errcode={errcode}, errmsg={errmsg}"
            )
        return result
    except Exception as e:
        logger.error(f"临时素材上传异常，agent_id={agent_id}, file={file_path}, err={str(e)}")
        return {"errcode": -1, "errmsg": str(e)}


async def download_temporary_media(
    session: aiohttp.ClientSession,
    agent_id: str,
    media_id: str,
    save_path: Optional[str] = None,
    range_start: Optional[int] = None,
    range_end: Optional[int] = None,
    chunk_size: int = 64 * 1024,
) -> Dict[str, Any]:
    """
    获取临时素材（下载）。

    对应接口：
    GET https://qyapi.weixin.qq.com/cgi-bin/media/get?access_token=ACCESS_TOKEN&media_id=MEDIA_ID

    支持可选 Range 分块下载（用于 >20MB 时避免 830002）。
    """
    logger = get_logger("wechat_media_download")
    if not media_id:
        return {"errcode": -1, "errmsg": "media_id is required"}

    access_token = await get_access_token(session, agent_id)
    if not access_token:
        return {"errcode": -1, "errmsg": "failed to get access_token"}

    url = _maybe_append_debug_param(
        f"https://qyapi.weixin.qq.com/cgi-bin/media/get?access_token={access_token}&media_id={media_id}"
    )
    headers: Dict[str, str] = {}
    if range_start is not None or range_end is not None:
        start = "" if range_start is None else str(range_start)
        end = "" if range_end is None else str(range_end)
        headers["Range"] = f"bytes={start}-{end}"

    try:
        async with session.get(url, headers=headers, proxy=_get_request_proxy()) as resp:
            ct = resp.headers.get("Content-Type", "")
            cd = resp.headers.get("Content-Disposition", "")
            filename = _parse_filename_from_content_disposition(cd)

            if resp.status != 200:
                # 失败时通常是 json，例如 {"errcode":40007,"errmsg":"invalid media_id"}
                try:
                    result = await resp.json()
                except Exception:
                    raw = await resp.read()
                    result = {
                        "errcode": -1,
                        "errmsg": "download failed",
                        "http_status": resp.status,
                        "content_type": ct,
                        "raw": raw[:200],
                    }
                logger.error(
                    f"临时素材下载失败，agent_id={agent_id}, media_id={media_id}, http_status={resp.status}, result={result}"
                )
                return result

            # 如果 save_path 存在就流式写文件，否则直接读 bytes
            if save_path:
                os.makedirs(os.path.dirname(save_path) or ".", exist_ok=True)
                with open(save_path, "wb") as f:
                    async for chunk in resp.content.iter_chunked(chunk_size):
                        if not chunk:
                            continue
                        f.write(chunk)
                logger.info(f"临时素材下载完成，agent_id={agent_id}, media_id={media_id}, saved_to={save_path}")
                return {
                    "status": "success",
                    "content_type": ct,
                    "filename": filename or save_path,
                    "saved_to": save_path,
                }

            data = await resp.read()

            # 成功时返回二进制；失败时常见为 json
            if resp.status == 200 and ("application/json" not in (ct or "").lower()):
                logger.info(f"临时素材下载完成，agent_id={agent_id}, media_id={media_id}, bytes={len(data)}")
                return {
                    "status": "success",
                    "content_type": ct,
                    "filename": filename or f"{media_id}",
                    "data": data,
                }

            # 理论上这里不会到（resp.status!=200 已提前返回；但为了兜底仍保留解析分支）
            try:
                return await resp.json()
            except Exception:
                return {
                    "errcode": -1,
                    "errmsg": "unexpected download response",
                    "http_status": resp.status,
                    "content_type": ct,
                }
    except Exception as e:
        logger.error(f"临时素材下载异常，agent_id={agent_id}, media_id={media_id}, err={str(e)}")
        return {"errcode": -1, "errmsg": str(e)}


async def send_wechat_message(
    msg: Optional[str],
    agent_id: str,
    user: str = "",
    party: str = "",
    tag: str = "",
    msgtype: str = "text",
    media_id: Optional[str] = None,
    title: Optional[str] = None,
    description: Optional[str] = None,
    safe: int = 0,
    enable_duplicate_check: Optional[int] = None,
    duplicate_check_interval: Optional[int] = None,
) -> dict:
    logger = get_logger("wechat_send")
    receiver = user or party or tag
    if msgtype == "text":
        content_len = len(msg or "")
        logger.info(f"开始发送消息，agent_id: {agent_id}, 接收人: {receiver}, 类型: {msgtype}, 消息长度: {content_len}")
    else:
        logger.info(f"开始发送消息，agent_id: {agent_id}, 接收人: {receiver}, 类型: {msgtype}, media_id: {media_id}")

    async with create_wechat_aiohttp_session() as session:
        access_token = await get_access_token(session, agent_id)
        if not access_token:
            logger.error(f"获取access_token失败，无法发送消息，agent_id: {agent_id}")
            return {"status": "error", "message": "获取access_token失败"}

        agent_conf = AGENT_CONFIGS.get(agent_id)
        url = f"https://qyapi.weixin.qq.com/cgi-bin/message/send?access_token={access_token}"
        
        data: Dict[str, Any] = {
            "touser": user,
            "toparty": party,
            "totag": tag,
            "msgtype": msgtype,
            "agentid": agent_conf["agent_id"],
            "safe": safe,
        }

        if msgtype == "text":
            if not msg:
                logger.error("文本消息缺少 message 内容")
                return {"status": "error", "message": "缺少message"}
            data["text"] = {"content": msg}
        elif msgtype in ("image", "voice"):
            if not media_id:
                logger.error(f"{msgtype} 消息缺少 media_id")
                return {"status": "error", "message": "缺少media_id"}
            data[msgtype] = {"media_id": media_id}
        elif msgtype == "video":
            if not media_id:
                logger.error("video 消息缺少 media_id")
                return {"status": "error", "message": "缺少media_id"}
            data["video"] = {
                "media_id": media_id,
                "title": title or "",
                "description": description or "",
            }
        else:
            logger.error(f"不支持的 msgtype: {msgtype}")
            return {"status": "error", "message": f"不支持的 msgtype: {msgtype}"}

        if enable_duplicate_check is not None:
            data["enable_duplicate_check"] = enable_duplicate_check
        if duplicate_check_interval is not None:
            data["duplicate_check_interval"] = duplicate_check_interval
        try:
            result = await _wechat_request(
                session,
                "POST",
                url,
                lambda resp: resp.json(content_type=None),
                logger_name="wechat_send",
                request_kwargs={"json": data},
            )
            if result.get('errcode') == 0:
                if msgtype == "text":
                    display_msg = (msg or "")[:50] + "..." if len(msg or "") > 50 else (msg or "")
                    logger.info(f"消息发送成功，agent_id: {agent_id}, 接收人: {receiver}, 类型: {msgtype}, 内容: {display_msg}")
                else:
                    logger.info(f"消息发送成功，agent_id: {agent_id}, 接收人: {receiver}, 类型: {msgtype}, media_id: {media_id}")
            else:
                logger.error(f"消息发送失败，agent_id: {agent_id}, 接收人: {receiver}, 类型: {msgtype}, 错误: {result.get('errmsg')}, 错误码: {result.get('errcode')}")
            return result
        except Exception as e:
            logger.error(f"消息发送异常，agent_id: {agent_id}, 接收人: {receiver}, 类型: {msgtype}, 错误: {str(e)}")
            import traceback
            logger.error(f"异常堆栈: {traceback.format_exc()}")
            return {"status": "error", "message": f"发送失败: {str(e)}"}

def register_routes(app: FastAPI, get_plugin_manager: Callable[[str], PluginManager]):
    """注册所有API路由，所有路径加 /{agent_id} 前缀"""

    @app.post("/{agent_id}/send", summary="发送企业微信消息")
    async def send_message(agent_id: str, req: SendMessageRequest):
        logger = get_logger("wechat_api")
        receiver = req.user or req.party or req.tag
        logger.info(f"收到主动发送消息请求，agent_id: {agent_id}, 接收人: {receiver}")
        
        if agent_id not in AGENT_CONFIGS:
            logger.error(f"无效的agent_id: {agent_id}")
            raise HTTPException(status_code=404, detail="无效的agent_id")
        if not req.user and not req.party and not req.tag:
            logger.error(f"缺少接收人参数，agent_id: {agent_id}")
            raise HTTPException(status_code=400, detail="至少需要指定user、party或tag中的一个")
        
        result = await send_wechat_message(
            msg=req.message,
            agent_id=agent_id,
            user=req.user,
            party=req.party,
            tag=req.tag,
            msgtype=req.msgtype or "text",
            media_id=req.media_id,
            title=req.title,
            description=req.description,
            safe=req.safe or 0,
            enable_duplicate_check=req.enable_duplicate_check,
            duplicate_check_interval=req.duplicate_check_interval,
        )
        logger.info(f"主动发送消息完成，agent_id: {agent_id}, 结果: {result.get('errcode', 'unknown')}")
        return result

    @app.get("/{agent_id}/", summary="服务状态检查")
    async def root(agent_id: str):
        if agent_id not in AGENT_CONFIGS:
            raise HTTPException(status_code=404, detail="无效的agent_id")
        plugin_manager = get_plugin_manager(agent_id)
        return {
            "status": "running",
            "service": "企业微信智能对话系统",
            "description": "支持插件自动载入的企业微信消息处理服务，支持多应用",
            "author": "xinghehy",
            "agent_id": agent_id,
            "loaded_plugins": [{"name": p.name, "description": p.description} for p in plugin_manager.plugins],
            "timestamp": datetime.datetime.now().isoformat()
        }

    @app.get("/{agent_id}/wechat", summary="企业微信URL验证")
    async def wechat_verify(agent_id: str, msg_signature: str, timestamp: str, nonce: str, echostr: str) -> Response:
        logger = get_logger("wechat_verify")
        logger.info(f"收到企业微信URL验证请求，agent_id: {agent_id}")
        
        if agent_id not in AGENT_CONFIGS:
            logger.error(f"无效的agent_id: {agent_id}")
            raise HTTPException(status_code=404, detail="无效的agent_id")
        
        agent_conf = AGENT_CONFIGS[agent_id]
        try:
            echostr_decoded = urllib.parse.unquote(echostr)
            if not verify_signature(msg_signature, timestamp, nonce, echostr_decoded, agent_conf["token"]):
                logger.error(f"签名验证失败，agent_id: {agent_id}")
                raise HTTPException(status_code=403, detail="签名验证失败")
            
            msg_content, corp_id = decrypt(echostr_decoded, agent_conf["encoding_aes_key"])
            if corp_id != CONFIG["CORP_ID"]:
                logger.error(f"企业ID不匹配，agent_id: {agent_id}, 期望: {CONFIG['CORP_ID']}, 实际: {corp_id}")
                raise HTTPException(status_code=403, detail="企业ID不匹配")
            
            logger.debug(f"URL验证成功，agent_id: {agent_id}")
            return Response(
                content=msg_content.strip(),
                media_type="text/plain; charset=utf-8"
            )
        except Exception as e:
            logger.error(f"URL验证失败，agent_id: {agent_id}, 错误: {str(e)}")
            raise HTTPException(status_code=403, detail=f"验证失败: {str(e)}")

    @app.post("/{agent_id}/wechat", summary="接收企业微信消息")
    async def wechat_receive(agent_id: str, request: Request, background_tasks: BackgroundTasks) -> Response:
        logger = get_logger("wechat_receive")
        logger.info(f"收到企业微信消息，agent_id: {agent_id}")

        if agent_id not in AGENT_CONFIGS:
            logger.error(f"无效的agent_id: {agent_id}")
            raise HTTPException(status_code=404, detail="无效的agent_id")

        agent_conf = AGENT_CONFIGS[agent_id]
        plugin_manager = get_plugin_manager(agent_id)

        try:
            # 获取验证参数
            msg_signature = request.query_params.get('msg_signature', '')
            timestamp = request.query_params.get('timestamp', '')
            nonce = request.query_params.get('nonce', '')
            logger.debug(f"请求验证参数: signature={msg_signature[:8]}..., timestamp={timestamp}, nonce={nonce}")

            if not all([msg_signature, timestamp, nonce]):
                logger.error(f"缺少必要的验证参数，agent_id: {agent_id}")
                raise HTTPException(status_code=400, detail="缺少必要的参数")

            # 解析加密消息
            xml_data = await request.body()
            root = ET.fromstring(xml_data)
            encrypt_str = root.find('Encrypt').text

            if not encrypt_str:
                logger.error(f"缺少Encrypt字段，agent_id: {agent_id}")
                raise HTTPException(status_code=400, detail="缺少Encrypt字段")

            # 签名验证
            if not verify_signature(msg_signature, timestamp, nonce, encrypt_str, agent_conf["token"]):
                logger.error(f"签名验证失败，agent_id: {agent_id}")
                raise HTTPException(status_code=403, detail="签名验证失败")

            # 解密消息内容
            msg_content, corp_id = decrypt(encrypt_str, agent_conf["encoding_aes_key"])
            if corp_id != CONFIG["CORP_ID"]:
                logger.error(f"企业ID不匹配，期望: {CONFIG['CORP_ID']}, 实际: {corp_id}")
                raise HTTPException(status_code=403, detail="企业ID不匹配")

            # 解析消息结构
            msg_root = ET.fromstring(msg_content)
            msg_type = msg_root.find('MsgType').text
            from_user = msg_root.find('FromUserName').text
            to_user = msg_root.find('ToUserName').text

            # 消息去重处理（使用 SET NX EX 保证原子性，并受配置开关控制）
            msg_id = msg_root.find('MsgId')
            msg_id_str = msg_id.text if msg_id is not None else None
            if msg_id_str and ENABLE_MSG_DEDUP:
                dedup_key = f"msg_dedup:{agent_id}:{msg_id_str}"
                try:
                    # set 返回 True 表示首次写入，None/False 表示已存在（重复）
                    set_ok = redis_client.set(dedup_key, "1", ex=MSG_DEDUP_TTL, nx=True)
                    if not set_ok:
                        logger.info(f"消息重复，跳过处理，消息ID: {msg_id_str}, 发送人: {from_user}")
                        return Response(content="success", media_type="text/plain")
                    logger.debug(f"设置去重标记，键: {dedup_key}, 有效期: {MSG_DEDUP_TTL}秒")
                except Exception as e:
                    logger.error(f"设置去重标记失败（忽略去重继续处理）: {str(e)}")
            elif not msg_id_str:
                logger.warning(f"消息缺少MsgId字段，无法进行去重检查")

            logger.info(f"消息解析成功，类型: {msg_type}, 发送人: {from_user}")

            # 后台处理消息并通过主动消息发送回复（避免被动超时导致平台重试）
            async def process_and_send_replies_task(message_content):
                try:
                    logger.debug(f"开始后台处理任务，消息内容: {message_content}")
                    async with create_wechat_aiohttp_session() as session:
                        user_context = await get_user_context(session, agent_id, from_user)

                    context = {
                        "plugin_manager": plugin_manager,
                        "message_type": msg_type,
                        "timestamp": datetime.datetime.now().timestamp(),
                        "agent_id": agent_id,
                        "user_info": user_context
                    }

                    logger.debug(f"开始后台处理{msg_type}消息并主动发送")
                    logger.debug(f"调用插件管理器处理消息，插件管理器: {plugin_manager}")
                    replies = await plugin_manager.process_message(message_content, from_user, context)
                    logger.debug(f"插件管理器返回结果: {replies}")
                    if not replies:
                        logger.info("插件未生成回复或回复为空，跳过主动发送")
                        return
                    for i, reply in enumerate(replies, 1):
                        await enqueue_active_message(agent_id=agent_id, msg=reply, user=from_user)
                        logger.debug(f"后台主动回复{i}已入队，等待按序发送")
                except Exception as e:
                    logger.error(f"后台处理并发送回复异常: {str(e)}")
                    import traceback
                    logger.error(f"异常堆栈: {traceback.format_exc()}")

            # 初始化
            message_content = None

            # 处理不同类型消息
            if msg_type == 'text':
                content = msg_root.find('Content').text
                display_content = content[:50] + "..." if len(content) > 50 else content
                logger.info(f"收到文本消息，发送人: {from_user}, 内容: {display_content}")
                message_content = content

            elif msg_type == 'image':
                media_id = msg_root.find('MediaId').text
                pic_url = msg_root.find('PicUrl').text
                logger.info(f"收到图片消息，发送人: {from_user}, URL: {pic_url}")
                message_content = {'PicUrl': pic_url, 'MediaId': media_id}

            elif msg_type == 'event':
                event_type = msg_root.find('Event').text
                logger.info(f"收到事件消息，类型: {event_type}")
                if event_type == 'subscribe':
                    message_content = "欢迎关注！有什么可以帮助您的吗？"

            else:
                logger.info(f"收到不支持的消息类型: {msg_type}")
                return Response(content="success", media_type="text/plain")

            # 将插件处理放入后台任务，所有真实内容通过主动消息发送
            if message_content is not None:
                background_tasks.add_task(process_and_send_replies_task, message_content)

            # 直接返回 success，避免被动长耗时
            logger.info("被动确认success已返回（后台将主动发送真实回复）")
            return Response(content="success", media_type="text/plain")

        except Exception as e:
            logger.error(f"消息处理失败: {str(e)}", exc_info=True)
            # 异常情况下仍返回success避免企业微信重复推送
            return Response(content="success", media_type="text/plain")