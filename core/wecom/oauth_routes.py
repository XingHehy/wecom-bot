"""
企业微信OAuth路由模块
提供网页授权相关的API接口
"""

from typing import Optional
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from pydantic import BaseModel

from .oauth import OAuthService, OAuthCallbackRequest, OAuthUserInfo
from core.logger import get_logger
from core.redis_client import redis_client
import secrets
import json

logger = get_logger("oauth_routes")

# 创建OAuth路由
oauth_router = APIRouter(prefix="/oauth", tags=["OAuth"])

# 会话配置
SESSION_COOKIE_NAME = "wxbot_session"
SESSION_TTL_SECONDS = 86400  # 1 天

class AuthorizationUrlRequest(BaseModel):
    """生成授权链接请求模型"""
    scope: str = "snsapi_base"  # 默认使用snsapi_base
    state: Optional[str] = None

class AuthorizationUrlResponse(BaseModel):
    """授权链接响应模型"""
    authorization_url: str
    state: str
    scope: str

@oauth_router.get("/{agent_id}/authorize", summary="生成企业微信授权链接")
async def generate_authorization_url(
    agent_id: str,
    scope: str = Query("snsapi_base", description="授权作用域：snsapi_base或snsapi_userinfo"),
    state: Optional[str] = Query(None, description="自定义状态参数")
):
    """
    生成企业微信网页授权链接
    
    - **agent_id**: 应用ID
    - **scope**: 授权作用域
        - snsapi_base: 静默授权，只能获取用户的openid
        - snsapi_userinfo: 弹出授权页面，可以通过openid获取用户基本信息
    - **state**: 自定义状态参数，用于防止CSRF攻击
    
    Returns:
        包含授权链接的响应
    """
    try:
        oauth_service = OAuthService(agent_id)
        authorization_url = oauth_service.generate_authorization_url(scope, state)
        
        # 从URL中提取state参数
        import urllib.parse
        parsed_url = urllib.parse.urlparse(authorization_url)
        query_params = urllib.parse.parse_qs(parsed_url.query)
        generated_state = query_params.get('state', [None])[0]
        
        logger.info(f"生成授权链接成功，agent_id: {agent_id}, scope: {scope}, state: {generated_state}")
        
        return AuthorizationUrlResponse(
            authorization_url=authorization_url,
            state=generated_state,
            scope=scope
        )
    except ValueError as e:
        logger.error(f"生成授权链接失败，agent_id: {agent_id}, 错误: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"生成授权链接异常，agent_id: {agent_id}, 错误: {str(e)}")
        raise HTTPException(status_code=500, detail="生成授权链接失败")

@oauth_router.get("/{agent_id}/callback", summary="OAuth授权回调处理")
async def oauth_callback(
    agent_id: str,
    code: str = Query(..., description="企业微信授权码"),
    state: str = Query(..., description="状态参数"),
    request: Request = None
):
    """
    处理企业微信OAuth授权回调
    
    - **agent_id**: 应用ID
    - **code**: 企业微信返回的授权码
    - **state**: 状态参数，用于验证请求合法性
    
    Returns:
        HTML页面，显示授权结果和用户信息
    """
    try:
        oauth_service = OAuthService(agent_id)
        user_info = await oauth_service.get_user_info_by_code(code, state)
        print("user_info1",user_info)
        logger.info(f"OAuth回调处理成功，agent_id: {agent_id}, userid: {user_info.get('userid')}")
        
        # 创建会话并下发 Cookie
        session_id = secrets.token_urlsafe(32)
        redis_client.setex(
            f"oauth:{agent_id}:session:{session_id}",
            SESSION_TTL_SECONDS,
            json.dumps({"userid": user_info.get("userid"), "ts": int(__import__("time").time())})
        )

        secure_cookie = False
        try:
            if request is not None and getattr(request, "url", None):
                secure_cookie = (request.url.scheme == "https")
        except Exception:
            secure_cookie = False

        # 生成HTML响应页面并设置 Cookie
        html_content = _generate_success_html(user_info, agent_id)
        response = HTMLResponse(content=html_content, status_code=200)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=f"{agent_id}:{session_id}",
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=secure_cookie,
            path="/",
        )
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"OAuth回调处理异常，agent_id: {agent_id}, 错误: {str(e)}")
        html_content = _generate_error_html(str(e), agent_id)
        return HTMLResponse(content=html_content, status_code=500)

@oauth_router.get("/callback/{agent_id}", summary="OAuth授权回调处理（兼容旧路径）")
async def oauth_callback_legacy(
    agent_id: str,
    code: str = Query(..., description="企业微信授权码"),
    state: str = Query(..., description="状态参数"),
    request: Request = None
):
    """
    兼容旧回调路径 /oauth/callback/{agent_id}
    """
    return await oauth_callback(agent_id=agent_id, code=code, state=state, request=request)

@oauth_router.post("/{agent_id}/callback", summary="OAuth授权回调处理（POST方式）")
async def oauth_callback_post(
    agent_id: str,
    request: OAuthCallbackRequest
):
    """
    处理企业微信OAuth授权回调（POST方式）
    
    - **agent_id**: 应用ID
    - **request**: 包含code和state的请求体
    
    Returns:
        用户信息
    """
    try:
        oauth_service = OAuthService(agent_id)
        user_info = await oauth_service.get_user_info_by_code(request.code, request.state)
        print("user_info2",user_info)
        logger.info(f"OAuth回调处理成功（POST），agent_id: {agent_id}, userid: {user_info.get('userid')}")

        # 创建会话并下发 Cookie
        session_id = secrets.token_urlsafe(32)
        redis_client.setex(
            f"oauth:{agent_id}:session:{session_id}",
            SESSION_TTL_SECONDS,
            json.dumps({"userid": user_info.get("userid"), "ts": int(__import__("time").time())})
        )

        response = JSONResponse(content=OAuthUserInfo(**user_info).model_dump())
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=f"{agent_id}:{session_id}",
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=False,  # 若部署为 HTTPS，可改为 True
            path="/",
        )
        return response
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"OAuth回调处理异常（POST），agent_id: {agent_id}, 错误: {str(e)}")
        raise HTTPException(status_code=500, detail="处理授权回调失败")

# 为本人获取详细信息（需要会话）
@oauth_router.get("/{agent_id}/me", summary="获取当前登录用户详细信息")
async def get_my_user_info(
    agent_id: str,
    request: Request
):
    """
    获取当前登录用户的详细信息（基于回调设置的会话 Cookie）。
    """
    try:
        cookie_val = request.cookies.get(SESSION_COOKIE_NAME)
        if not cookie_val:
            raise HTTPException(status_code=401, detail="未登录或会话已失效")
        try:
            cookie_agent_id, session_id = cookie_val.split(":", 1)
        except ValueError:
            raise HTTPException(status_code=401, detail="无效会话")
        if cookie_agent_id != agent_id:
            raise HTTPException(status_code=401, detail="会话与应用不匹配")

        session_data = redis_client.get(f"oauth:{agent_id}:session:{session_id}")
        if not session_data:
            raise HTTPException(status_code=401, detail="会话不存在或已过期")
        try:
            payload = json.loads(session_data)
            userid = payload.get("userid")
        except Exception:
            raise HTTPException(status_code=401, detail="会话损坏")
        if not userid:
            raise HTTPException(status_code=401, detail="会话缺少用户信息")

        oauth_service = OAuthService(agent_id)
        user_info = await oauth_service.get_user_info_by_userid(userid)
        print("user_info_me", user_info)
        logger.info(f"获取本人信息成功，agent_id: {agent_id}, userid: {userid}")
        return OAuthUserInfo(**user_info)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取本人信息异常，agent_id: {agent_id}, 错误: {str(e)}")
        raise HTTPException(status_code=500, detail="获取用户信息失败")


# 收紧原接口：要求会话，且仅允许查询本人
@oauth_router.get("/{agent_id}/userinfo/{userid}", summary="获取用户详细信息（仅限本人）")
async def get_user_info(
    agent_id: str,
    userid: str,
    request: Request
):
    """
    获取已授权用户的详细信息
    
    - **agent_id**: 应用ID
    - **userid**: 企业微信用户ID
    
    Returns:
        用户详细信息
    """
    try:
        cookie_val = request.cookies.get(SESSION_COOKIE_NAME)
        if not cookie_val:
            raise HTTPException(status_code=401, detail="未登录或会话已失效")
        try:
            cookie_agent_id, session_id = cookie_val.split(":", 1)
        except ValueError:
            raise HTTPException(status_code=401, detail="无效会话")
        if cookie_agent_id != agent_id:
            raise HTTPException(status_code=401, detail="会话与应用不匹配")

        session_data = redis_client.get(f"oauth:{agent_id}:session:{session_id}")
        if not session_data:
            raise HTTPException(status_code=401, detail="会话不存在或已过期")
        try:
            payload = json.loads(session_data)
            session_userid = payload.get("userid")
        except Exception:
            raise HTTPException(status_code=401, detail="会话损坏")

        if not session_userid or session_userid != userid:
            raise HTTPException(status_code=403, detail="无权限查看他人信息")

        oauth_service = OAuthService(agent_id)
        user_info = await oauth_service.get_user_info_by_userid(userid)
        print("user_info3", user_info)
        logger.info(f"获取用户信息成功，agent_id: {agent_id}, userid: {userid}")
        
        return OAuthUserInfo(**user_info)
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取用户信息异常，agent_id: {agent_id}, userid: {userid}, 错误: {str(e)}")
        raise HTTPException(status_code=500, detail="获取用户信息失败")

@oauth_router.get("/{agent_id}/demo", summary="OAuth演示页面")
async def oauth_demo_page(agent_id: str):
    """
    提供OAuth演示页面，包含授权链接和说明
    
    - **agent_id**: 应用ID
    
    Returns:
        HTML演示页面
    """
    print("agent_id",agent_id)
    try:
        oauth_service = OAuthService(agent_id)
        print("oauth_service",oauth_service)
        # 生成不同scope的授权链接
        base_auth_url = oauth_service.generate_authorization_url("snsapi_base")
        # print("base_auth_url",base_auth_url)
        userinfo_auth_url = oauth_service.generate_authorization_url("snsapi_userinfo")
        # print("userinfo_auth_url",userinfo_auth_url)
        
        html_content = _generate_demo_html(agent_id, base_auth_url, userinfo_auth_url)
        # print("html_content",html_content)
        return HTMLResponse(content=html_content)
        
    except Exception as e:
        logger.error(f"生成演示页面异常，agent_id: {agent_id}, 错误: {str(e)}")
        raise HTTPException(status_code=500, detail="生成演示页面失败")

def _generate_success_html(user_info: dict, agent_id: str) -> str:
    """生成授权成功的HTML页面"""
    return f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>企业微信授权成功</title>
        <style>
            body {{
                font-family: 'Microsoft YaHei', Arial, sans-serif;
                max-width: 800px;
                margin: 0 auto;
                padding: 20px;
                background-color: #f5f5f5;
            }}
            .container {{
                background: white;
                padding: 30px;
                border-radius: 10px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }}
            .success-icon {{
                color: #52c41a;
                font-size: 48px;
                text-align: center;
                margin-bottom: 20px;
            }}
            h1 {{
                color: #1890ff;
                text-align: center;
                margin-bottom: 30px;
            }}
            .user-info {{
                background: #f8f9fa;
                padding: 20px;
                border-radius: 8px;
                margin: 20px 0;
            }}
            .info-item {{
                display: flex;
                margin: 10px 0;
                border-bottom: 1px solid #e8e8e8;
                padding-bottom: 10px;
            }}
            .info-label {{
                font-weight: bold;
                width: 120px;
                color: #666;
            }}
            .info-value {{
                flex: 1;
                color: #333;
            }}
            .back-btn {{
                display: inline-block;
                background: #1890ff;
                color: white;
                padding: 12px 24px;
                text-decoration: none;
                border-radius: 6px;
                margin-top: 20px;
                text-align: center;
            }}
            .back-btn:hover {{
                background: #40a9ff;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="success-icon">✅</div>
            <h1>企业微信授权成功</h1>
            <p style="text-align: center; color: #666; margin-bottom: 30px;">
                应用ID: {agent_id} | 用户已成功授权
            </p>
            {user_info}
            <div class="user-info">
                <h3>用户信息</h3>
                <div class="info-item">
                    <span class="info-label">用户ID:</span>
                    <span class="info-value">{user_info.get('userid', 'N/A')}</span>
                </div>
                <div class="info-item">
                    <span class="info-label">姓名:</span>
                    <span class="info-value">{user_info.get('name', 'N/A')}</span>
                </div>
                <div class="info-item">
                    <span class="info-label">部门:</span>
                    <span class="info-value">{', '.join(map(str, user_info.get('department', []))) if user_info.get('department') else 'N/A'}</span>
                </div>
                <div class="info-item">
                    <span class="info-label">职位:</span>
                    <span class="info-value">{user_info.get('position', 'N/A')}</span>
                </div>
                <div class="info-item">
                    <span class="info-label">手机:</span>
                    <span class="info-value">{user_info.get('mobile', 'N/A')}</span>
                </div>
                <div class="info-item">
                    <span class="info-label">邮箱:</span>
                    <span class="info-value">{user_info.get('email', 'N/A')}</span>
                </div>
                <div class="info-item">
                    <span class="info-label">状态:</span>
                    <span class="info-value">{'启用' if user_info.get('enable') == 1 else '禁用'}</span>
                </div>
            </div>
            
            <div style="text-align: center;">
                <a href="/oauth/{agent_id}/demo" class="back-btn">返回演示页面</a>
            </div>
        </div>
    </body>
    </html>
    """

def _generate_error_html(error_msg: str, agent_id: str) -> str:
    """生成授权失败的HTML页面"""
    return f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>企业微信授权失败</title>
        <style>
            body {{
                font-family: 'Microsoft YaHei', Arial, sans-serif;
                max-width: 800px;
                margin: 0 auto;
                padding: 20px;
                background-color: #f5f5f5;
            }}
            .container {{
                background: white;
                padding: 30px;
                border-radius: 10px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }}
            .error-icon {{
                color: #ff4d4f;
                font-size: 48px;
                text-align: center;
                margin-bottom: 20px;
            }}
            h1 {{
                color: #ff4d4f;
                text-align: center;
                margin-bottom: 30px;
            }}
            .error-info {{
                background: #fff2f0;
                border: 1px solid #ffccc7;
                padding: 20px;
                border-radius: 8px;
                margin: 20px 0;
            }}
            .back-btn {{
                display: inline-block;
                background: #1890ff;
                color: white;
                padding: 12px 24px;
                text-decoration: none;
                border-radius: 6px;
                margin-top: 20px;
                text-align: center;
            }}
            .back-btn:hover {{
                background: #40a9ff;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="error-icon">❌</div>
            <h1>企业微信授权失败</h1>
            <p style="text-align: center; color: #666; margin-bottom: 30px;">
                应用ID: {agent_id} | 授权过程中发生错误
            </p>
            
            <div class="error-info">
                <h3>错误信息</h3>
                <p style="color: #ff4d4f; word-break: break-all;">{error_msg}</p>
            </div>
            
            <div style="text-align: center;">
                <a href="/oauth/{agent_id}/demo" class="back-btn">返回演示页面</a>
            </div>
        </div>
    </body>
    </html>
    """

def _generate_demo_html(agent_id: str, base_auth_url: str, userinfo_auth_url: str) -> str:
    """生成OAuth演示页面"""
    return f"""
    <!DOCTYPE html>
    <html lang="zh-CN">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>企业微信OAuth演示 - {agent_id}</title>
        <style>
            body {{
                font-family: 'Microsoft YaHei', Arial, sans-serif;
                max-width: 1000px;
                margin: 0 auto;
                padding: 20px;
                background-color: #f5f5f5;
            }}
            .container {{
                background: white;
                padding: 30px;
                border-radius: 10px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
            }}
            h1 {{
                color: #1890ff;
                text-align: center;
                margin-bottom: 30px;
            }}
            .auth-section {{
                background: #f8f9fa;
                padding: 20px;
                border-radius: 8px;
                margin: 20px 0;
                border-left: 4px solid #1890ff;
            }}
            .auth-title {{
                color: #1890ff;
                font-size: 18px;
                font-weight: bold;
                margin-bottom: 15px;
            }}
            .auth-desc {{
                color: #666;
                margin-bottom: 15px;
                line-height: 1.6;
            }}
            .auth-btn {{
                display: inline-block;
                background: #52c41a;
                color: white;
                padding: 12px 24px;
                text-decoration: none;
                border-radius: 6px;
                margin: 10px 10px 10px 0;
                font-weight: bold;
            }}
            .auth-btn:hover {{
                background: #73d13d;
            }}
            .auth-btn.secondary {{
                background: #1890ff;
            }}
            .auth-btn.secondary:hover {{
                background: #40a9ff;
            }}
            .info-box {{
                background: #e6f7ff;
                border: 1px solid #91d5ff;
                padding: 15px;
                border-radius: 6px;
                margin: 20px 0;
            }}
            .code-block {{
                background: #f6f8fa;
                border: 1px solid #e1e4e8;
                padding: 15px;
                border-radius: 6px;
                font-family: 'Courier New', monospace;
                overflow-x: auto;
                margin: 10px 0;
            }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>企业微信OAuth网页授权演示</h1>
            <p style="text-align: center; color: #666; margin-bottom: 30px;">
                应用ID: {agent_id} | 测试企业微信网页授权功能
            </p>
            
            <div class="info-box">
                <h3>📋 使用说明</h3>
                <p>企业微信网页授权分为两种模式：</p>
                <ul>
                    <li><strong>snsapi_base</strong>: 静默授权，用户无感知，只能获取用户的openid</li>
                    <li><strong>snsapi_userinfo</strong>: 弹出授权页面，用户确认后可以获取用户基本信息</li>
                </ul>
            </div>
            
            <div class="auth-section">
                <div class="auth-title">🔐 静默授权 (snsapi_base)</div>
                <div class="auth-desc">
                    适用于只需要验证用户身份的场景，用户无感知，授权过程在后台完成。
                    只能获取用户的openid，无法获取用户详细信息。
                </div>
                <a href="{base_auth_url}" class="auth-btn">开始静默授权</a>
                <div class="code-block">
                    授权链接: {base_auth_url}
                </div>
            </div>
            
            <div class="auth-section">
                <div class="auth-title">👤 用户信息授权 (snsapi_userinfo)</div>
                <div class="auth-desc">
                    适用于需要获取用户基本信息的场景，会弹出授权确认页面。
                    可以获取用户的姓名、部门、职位等详细信息。
                </div>
                <a href="{userinfo_auth_url}" class="auth-btn secondary">开始用户信息授权</a>
                <div class="code-block">
                    授权链接: {userinfo_auth_url}
                </div>
            </div>
            
            <div class="info-box">
                <h3>🔗 API接口</h3>
                <p>本演示页面还提供了以下API接口：</p>
                <ul>
                    <li><code>GET /oauth/{agent_id}/authorize</code> - 生成授权链接</li>
                    <li><code>GET /oauth/{agent_id}/callback</code> - 处理授权回调</li>
                    <li><code>POST /oauth/{agent_id}/callback</code> - 处理授权回调（POST方式）</li>
                    <li><code>GET /oauth/{agent_id}/userinfo/{{userid}}</code> - 获取用户详细信息</li>
                </ul>
            </div>
            
            <div class="info-box">
                <h3>⚠️ 注意事项</h3>
                <ul>
                    <li>请确保在企业微信管理后台配置了正确的回调域名</li>
                    <li>回调域名必须与您的实际部署域名一致</li>
                    <li>state参数用于防止CSRF攻击，会自动生成</li>
                    <li>授权码(code)只能使用一次，使用后立即失效</li>
                </ul>
            </div>
        </div>
    </body>
    </html>
    """
