import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

# 将项目根目录添加到Python路径
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

# 导入核心模块
from core.config import CONFIG, AGENT_CONFIGS
from core.plugin_manager import PluginManager
from core.wecom.enterprise_wechat import register_routes
from core.wecom.oauth_routes import oauth_router
from core.scheduler import start_scheduler, shutdown_scheduler, is_scheduler_leader
from core.logger import get_logger
from core.redis_client import redis_client  # 导入全局Redis单例

# 进程标识
PROCESS_ID = os.getpid()

# 插件管理器实例池，按 agent_id 缓存
plugin_managers = {}

def get_plugin_manager(agent_id: str):
    """获取指定应用的插件管理器（单例）"""
    if agent_id not in plugin_managers:
        pm = PluginManager()
        pm.load_plugins_for_agent(agent_id)
        plugin_managers[agent_id] = pm
    return plugin_managers[agent_id]

# 启动/关闭事件
@asynccontextmanager
async def lifespan(app: FastAPI):
    # ====================== startup 逻辑（启动时执行）======================
    logger = get_logger("main")
    logger.info(f"=== 企业微信智能对话系统启动 ===")
    logger.info(f"进程ID: {PROCESS_ID} (scheduler_leader: {is_scheduler_leader()})")
    logger.info(f"企业ID: {CONFIG['CORP_ID']}")
    logger.info(f"端口: {CONFIG['PORT']}")
    logger.info(f"支持应用: {list(AGENT_CONFIGS.keys())}")

    # 验证Redis连接
    try:
        redis_client.ping()
        logger.info("Redis连接验证成功")
    except Exception as e:
        logger.error(f"Redis连接失败: {str(e)}", exc_info=True)
        sys.exit(1)

    # 启动定时任务：多进程下通过 Redis 锁选主（只有 leader 会真正启动）
    try:
        start_scheduler()
        if is_scheduler_leader():
            logger.info("定时任务已启动（leader进程）")
            # 恢复用户自建任务（仅 leader 执行，避免重复恢复）
            try:
                for agent_id in AGENT_CONFIGS.keys():
                    pm = get_plugin_manager(agent_id)
                    for p in pm.plugins:
                        if hasattr(p, 'restore_tasks_from_store'):
                            restored = p.restore_tasks_from_store()
                            logger.info(f"应用 {agent_id} 的插件 {p.__class__.__name__} 已从存储恢复定时任务数量: {restored}")
            except Exception as e:
                logger.error(f"恢复定时任务失败: {str(e)}")
        else:
            logger.info("当前进程非调度器leader，不启动定时任务")
    except Exception as e:
        logger.error(f"定时任务启动失败: {str(e)}")

    yield

    # ======================  shutdown 逻辑（关闭时执行）======================
    shutdown_scheduler()

    # 关闭Redis连接池
    redis_client.close()

    logger = get_logger("main")
    logger.info(f"=== 企业微信智能对话系统已关闭 (进程ID: {PROCESS_ID}) ===")

# 初始化FastAPI应用（必须在模块级创建并挂载 lifespan，gunicorn 才会生效）
app = FastAPI(
    title="企业微信智能对话系统",
    description="支持插件自动载入的企业微信消息处理服务，支持多应用",
    version="2.0.0",
    lifespan=lifespan,
)
os.makedirs("temp_media", exist_ok=True)
# 挂载静态文件目录
app.mount("/temp_media", StaticFiles(directory="temp_media"), name="temp_media")

# 注册路由，所有接口加 /{agent_id} 前缀
register_routes(app, get_plugin_manager)

# 注册OAuth路由
app.include_router(oauth_router)

# 健康检查接口（新增）
@app.get("/health")
async def health_check():
    """服务健康检查接口"""
    try:
        # 检查Redis连接
        redis_healthy = redis_client.ping()
        # 检查调度器状态（leader 才会 running）
        from core.scheduler import scheduler
        scheduler_healthy = scheduler.running
            
        return {
            "status": "healthy",
            "process_id": PROCESS_ID,
            "scheduler_leader": is_scheduler_leader(),
            "dependencies": {
                "redis": "healthy" if redis_healthy else "unhealthy",
                "scheduler": "healthy" if scheduler_healthy else "unhealthy"
            }
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e)
        }


# 启动服务
if __name__ == '__main__':
    import uvicorn

    # 多进程模式需要使用导入字符串而不是 app 对象
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=CONFIG["PORT"],
        log_level="info",
        workers=1
    )
    