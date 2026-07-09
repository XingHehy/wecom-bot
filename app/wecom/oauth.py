"""
企业微信网页授权模块
基于企业微信官方文档：https://developer.work.weixin.qq.com/document/path/91022
"""

import time
from typing import Dict, Any, Optional
from urllib.parse import urlencode

import aiohttp
from aiohttp import TCPConnector
from fastapi import HTTPException
from pydantic import BaseModel

from app.config.settings import CONFIG, AGENT_CONFIGS
from app.logger import get_logger
from app.redis_client import redis_client
from app.config.yaml_config import get_config

logger = get_logger("oauth")

class OAuthConfig:
    """OAuth配置类"""
    def __init__(self, agent_id: str):
        self.agent_id = agent_id
        self.agent_config = AGENT_CONFIGS.get(agent_id)
        if not self.agent_config:
            raise ValueError(f"未找到agent_id={agent_id}的配置")
        
        self.corp_id = CONFIG['CORP_ID']
        self.corp_secret = self.agent_config['corp_secret']
        
        # 获取配置的回调域名
        yaml_cfg = get_config()
        
        # 优先使用应用特定的OAuth配置
        agent_oauth_cfg = self.agent_config.get('oauth') if isinstance(self.agent_config, dict) else None
        if isinstance(agent_oauth_cfg, dict) and agent_oauth_cfg.get('callback_domain'):
            callback_domain = agent_oauth_cfg['callback_domain']
        else:
            # 其次使用全局OAuth配置
            global_callback_domain = yaml_cfg.get('wechat.oauth.callback_domain')
            if global_callback_domain:
                callback_domain = global_callback_domain
            else:
                # 最后使用部署配置中的域名
                deployment_domain = yaml_cfg.get('deployment.domain')
                if deployment_domain:
                    callback_domain = f"https://{deployment_domain}"
                else:
                    # 默认使用localhost（仅用于开发测试）
                    callback_domain = "http://127.0.0.1:4455"
        
        # 统一回调路径为 /oauth/{agent_id}/callback
        self.redirect_uri = f"{callback_domain}/oauth/{agent_id}/callback"

class OAuthState(BaseModel):
    """OAuth状态信息"""
    agent_id: str
    user_id: Optional[str] = None
    timestamp: float
    nonce: str

class OAuthService:
    """企业微信OAuth服务"""
    
    def __init__(self, agent_id: str):
        self.config = OAuthConfig(agent_id)
        self.redis_key_prefix = f"oauth:{agent_id}"
    
    def generate_authorization_url(self, scope: str = "snsapi_base", state: Optional[str] = None) -> str:
        """
        生成企业微信授权链接
        
        Args:
            scope: 应用授权作用域，snsapi_base或snsapi_userinfo
            state: 用于保持请求和回调的状态，防止CSRF攻击
            
        Returns:
            授权链接
        """
        if not state:
            state = self._generate_state()
        
        # 保存state到Redis，用于验证回调
        state_data = OAuthState(
            agent_id=self.config.agent_id,
            timestamp=time.time(),
            nonce=state
        )
        redis_client.setex(
            f"{self.redis_key_prefix}:state:{state}",
            300,  # 5分钟过期
            state_data.model_dump_json()
        )
        
        params = {
            "appid": self.config.corp_id,
            "redirect_uri": self.config.redirect_uri,
            "response_type": "code",
            "scope": scope,
            "state": state
        }
        
        # 构建授权URL
        auth_url = "https://open.weixin.qq.com/connect/oauth2/authorize"
        query_string = urlencode(params)
        return f"{auth_url}?{query_string}#wechat_redirect"
    
    def _generate_state(self) -> str:
        """生成随机state字符串"""
        import secrets
        return secrets.token_urlsafe(16)
    
    async def get_access_token(self) -> Optional[str]:
        """获取企业微信access_token"""
        try:
            url = f"https://qyapi.weixin.qq.com/cgi-bin/gettoken"
            params = {
                "corpid": self.config.corp_id,
                "corpsecret": self.config.corp_secret
            }
            
            connector = TCPConnector(
                ssl=False,
                # 生产环境：
                # ssl=certifi.where()
            )
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(url, params=params) as resp:
                    result = await resp.json()
                    if result.get('errcode') == 0:
                        access_token = result.get('access_token')
                        # 缓存access_token到Redis
                        redis_client.setex(
                            f"{self.redis_key_prefix}:access_token",
                            7000,  # 企业微信access_token有效期为7200秒，提前200秒刷新
                            access_token
                        )
                        return access_token
                    else:
                        logger.error(f"获取access_token失败: {result.get('errmsg')}")
                        return None
        except Exception as e:
            logger.error(f"获取access_token异常: {str(e)}")
            return None
    
    async def get_user_info_by_code(self, code: str, state: str) -> Dict[str, Any]:
        """
        通过授权码获取用户信息
        
        Args:
            code: 企业微信授权码
            state: 状态参数
            
        Returns:
            用户信息字典
        """
        # 验证state
        if not await self._verify_state(state):
            raise HTTPException(status_code=400, detail="无效的state参数")
        
        # 获取access_token
        access_token = await self._get_cached_access_token()
        if not access_token:
            raise HTTPException(status_code=500, detail="获取access_token失败")
        
        try:
            # 通过code获取用户信息
            url = "https://qyapi.weixin.qq.com/cgi-bin/user/getuserinfo"
            params = {
                "access_token": access_token,
                "code": code
            }
            
            connector = TCPConnector(
                ssl=False,
                # 生产环境：
                # ssl=certifi.where()
            )
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(url, params=params) as resp:
                    result = await resp.json()
                    print("result",result)
                    if result.get('errcode') == 0:
                        # 兼容企业微信返回字段大小写差异
                        userid = result.get('userid') or result.get('UserId')
                        deviceid = result.get('deviceid') or result.get('DeviceId')
                        open_userid = (
                            result.get('open_userid')
                            or result.get('OpenId')
                            or result.get('openid')
                        )

                        user_info = {
                            "userid": userid,
                            "deviceid": deviceid,
                            "user_ticket": result.get('user_ticket'),
                            "expires_in": result.get('expires_in'),
                            "open_userid": open_userid,
                        }
                        
                        # 如果拿到了 userid，则进一步获取详细信息
                        if userid:
                            detailed_info = await self._get_detailed_user_info(access_token, userid)
                            user_info.update(detailed_info)
                        
                        # 清除已使用的state
                        redis_client.delete(f"{self.redis_key_prefix}:state:{state}")
                        
                        return user_info
                    else:
                        logger.error(f"通过code获取用户信息失败: {result.get('errmsg')}")
                        raise HTTPException(status_code=400, detail=f"获取用户信息失败: {result.get('errmsg')}")
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"获取用户信息异常: {str(e)}")
            raise HTTPException(status_code=500, detail="获取用户信息异常")
    
    async def _verify_state(self, state: str) -> bool:
        """验证state参数"""
        state_key = f"{self.redis_key_prefix}:state:{state}"
        state_data = redis_client.get(state_key)
        if not state_data:
            return False
        
        try:
            state_obj = OAuthState.model_validate_json(state_data)
            # 检查是否过期（5分钟）
            if time.time() - state_obj.timestamp > 300:
                redis_client.delete(state_key)
                return False
            return True
        except Exception:
            return False
    
    async def _get_cached_access_token(self) -> Optional[str]:
        """从缓存获取access_token，如果不存在则重新获取"""
        # 先从Redis获取
        cached_token = redis_client.get(f"{self.redis_key_prefix}:access_token")
        if cached_token:
            # 兼容 bytes 与 str
            if isinstance(cached_token, bytes):
                return cached_token.decode('utf-8')
            return str(cached_token)
        
        # 重新获取
        return await self.get_access_token()
    
    async def _get_detailed_user_info(self, access_token: str, userid: str) -> Dict[str, Any]:
        """获取详细用户信息"""
        try:
            url = "https://qyapi.weixin.qq.com/cgi-bin/user/get"
            params = {
                "access_token": access_token,
                "userid": userid
            }
            
            connector = TCPConnector(
                ssl=False,
                # 生产环境：
                # ssl=certifi.where()
            )
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.get(url, params=params) as resp:
                    result = await resp.json()
                    if result.get('errcode') == 0:
                        return {
                            "name": result.get('name'),
                            "department": result.get('department', []),
                            "position": result.get('position'),
                            "mobile": result.get('mobile'),
                            "gender": result.get('gender'),
                            "email": result.get('email'),
                            "avatar": result.get('avatar'),
                            "status": result.get('status'),
                            "enable": result.get('enable')
                        }
                    else:
                        logger.warning(f"获取详细用户信息失败: {result.get('errmsg')}")
                        return {}
        except Exception as e:
            logger.error(f"获取详细用户信息异常: {str(e)}")
            return {}
    
    async def get_user_info_by_userid(self, userid: str) -> Dict[str, Any]:
        """
        通过userid获取用户信息（用于已授权用户）
        
        Args:
            userid: 企业微信用户ID
            
        Returns:
            用户信息字典
        """
        access_token = await self._get_cached_access_token()
        if not access_token:
            raise HTTPException(status_code=500, detail="获取access_token失败")
        
        detailed_info = await self._get_detailed_user_info(access_token, userid)
        if detailed_info:
            detailed_info['userid'] = userid
            return detailed_info
        else:
            raise HTTPException(status_code=404, detail="用户不存在或获取用户信息失败")

class OAuthCallbackRequest(BaseModel):
    """OAuth回调请求模型"""
    code: str
    state: str

class OAuthUserInfo(BaseModel):
    """OAuth用户信息模型"""
    userid: str
    name: Optional[str] = None
    department: Optional[list] = None
    position: Optional[str] = None
    mobile: Optional[str] = None
    gender: Optional[str] = None
    email: Optional[str] = None
    avatar: Optional[str] = None
    status: Optional[int] = None
    enable: Optional[int] = None

