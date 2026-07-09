import asyncio
import datetime
import mimetypes
import os
from typing import List

import requests

from app.logger import get_logger
from app.scheduler.task_registry import scheduled_task
from app.wecom.enterprise_wechat import (
    create_wechat_aiohttp_session,
    enqueue_active_message,
    upload_temporary_media,
)


logger = get_logger("today_60s_task")

API_URL = "https://60s-api.114128.xyz/v2/60s"
AGENT_ID = "1000003"

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
TEMP_DIR = os.path.join(BASE_DIR, "temp_media")


def _guess_image_extension(content_type: str, url: str) -> str:
    ct = (content_type or "").split(";")[0].strip().lower()
    mapping = {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/gif": "gif",
        "image/webp": "webp",
        "image/bmp": "bmp",
        "image/svg+xml": "svg",
    }
    if ct in mapping:
        return mapping[ct]

    # 从 URL 路径推断扩展名
    try:
        from urllib.parse import urlparse
        path = urlparse(url).path
        _, ext = os.path.splitext(path)
        if ext:
            return ext.lstrip(".").lower()
    except Exception:
        pass

    # 最后兜底：根据 mimetypes 推断
    if ct:
        ext = mimetypes.guess_extension(ct)
        if ext:
            return ext.lstrip(".").lower()

    return "img"


def _download_image_to_temp(image_url: str, date_str: str) -> str:
    os.makedirs(TEMP_DIR, exist_ok=True)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    }
    resp = requests.get(image_url, headers=headers, timeout=25, stream=True)
    resp.raise_for_status()

    content_type = resp.headers.get("Content-Type", "")
    ext = _guess_image_extension(content_type, image_url)
    filename = f"today_60s_{date_str}.{ext}"
    file_path = os.path.join(TEMP_DIR, filename)

    with open(file_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            f.write(chunk)

    return file_path


def fetch_today_60s_image() -> tuple[str, str]:
    """Fetch today's 60s image and save it to temp media.

    Returns (file_path, date_str). Caller is responsible for deleting file_path.
    """
    resp = requests.get(API_URL, timeout=25)
    resp.raise_for_status()
    payload = resp.json()
    data = payload.get("data") or {}

    image_url = data.get("image") or data.get("cover") or ""
    if not image_url:
        raise RuntimeError(f"today_60s：未找到图片链接，payload keys={list(data.keys())}")

    date_str = (data.get("date") or datetime.date.today().isoformat()).strip()
    logger.info(f"today_60s：准备发送图片，date={date_str}, image_url={image_url}")
    return _download_image_to_temp(image_url=image_url, date_str=date_str), date_str


async def send_today_60s_image(agent_id: str, users: List[str]) -> str:
    """Send today's 60s image to users and return a short status string."""
    cleaned_users = [u for u in users if u]
    if not cleaned_users:
        return "发送失败：用户列表为空"

    file_path, date_str = fetch_today_60s_image()
    try:
        async with create_wechat_aiohttp_session() as session:
            up_res = await upload_temporary_media(
                session=session,
                agent_id=agent_id,
                file_path=file_path,
                media_type="image",
            )
            media_id = up_res.get("media_id") if isinstance(up_res, dict) else None
            if not media_id:
                logger.error(f"today_60s：临时素材上传失败，result={up_res}")
                return "发送失败：临时素材上传失败"

        touser = "|".join(cleaned_users)
        await enqueue_active_message(
            agent_id=agent_id,
            msgtype="image",
            media_id=media_id,
            user=touser,
        )
        logger.info(f"today_60s：已入队发送图片，touser={touser}, media_id={media_id}")
        return f"已发送 {date_str} 的 60 秒读懂世界图片"
    finally:
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                logger.debug(f"today_60s：已删除本地临时文件 file_path={file_path}")
        except Exception as cleanup_err:
            logger.warning(f"today_60s：删除本地临时文件失败 file_path={file_path}, err={cleanup_err}")


@scheduled_task(key="today_60s", mode="batch", default_time="09:50")
def send_today_60s_batch(user_configs: List[str]):
    """拉取 60s 每日素材图片，并发送到企业微信。"""
    try:
        if not user_configs:
            logger.warning("today_60s：用户列表为空，跳过")
            return

        asyncio.run(send_today_60s_image(AGENT_ID, user_configs))
    except Exception as e:
        logger.error(f"today_60s：任务执行失败，error={str(e)}", exc_info=True)

