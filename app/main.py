import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.agents.runtime import runtime_registry
from app.config.settings import AGENT_CONFIGS, CONFIG
from app.logger import get_logger
from app.memory.checkpointer import checkpointer_provider
from app.redis_client import redis_client
from app.scheduler.runtime import is_scheduler_leader, shutdown_scheduler, start_scheduler, scheduler
from app.services.schedule import schedule_service
from app.wecom.enterprise_wechat import register_routes
from app.wecom.oauth_routes import oauth_router


PROCESS_ID = os.getpid()


def get_agent_runtime(agent_id: str):
    return runtime_registry.get(agent_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger = get_logger("main")
    logger.info("=== 企业微信 LangChain 智能对话系统启动 ===")
    logger.info(f"进程ID: {PROCESS_ID} (scheduler_leader: {is_scheduler_leader()})")
    logger.info(f"企业ID: {CONFIG['CORP_ID']}")
    logger.info(f"端口: {CONFIG['PORT']}")
    logger.info(f"支持应用: {list(AGENT_CONFIGS.keys())}")

    try:
        redis_client.ping()
        logger.info("Redis连接验证成功")
    except Exception as exc:
        logger.error(f"Redis连接失败: {exc}", exc_info=True)
        sys.exit(1)

    try:
        await checkpointer_provider.get()
        logger.info(f"LangGraph memory backend: {checkpointer_provider.backend}")
    except Exception as exc:
        logger.warning(f"LangGraph memory 初始化失败，将按降级策略运行: {exc}")

    try:
        start_scheduler()
        if is_scheduler_leader():
            restored = schedule_service.restore_tasks_from_store()
            logger.info(f"用户创建提醒任务恢复数量: {restored}")
    except Exception as exc:
        logger.error(f"定时任务启动失败: {exc}")

    yield

    shutdown_scheduler()
    await checkpointer_provider.close()
    redis_client.close()
    logger.info(f"=== 企业微信 LangChain 智能对话系统已关闭 (进程ID: {PROCESS_ID}) ===")


app = FastAPI(
    title="企业微信 LangChain 智能对话系统",
    description="支持 LangChain 1.x agents、tool calling、Redis memory、定时任务和多企业微信应用",
    version="3.0.0",
    lifespan=lifespan,
)

os.makedirs("temp_media", exist_ok=True)
app.mount("/temp_media", StaticFiles(directory="temp_media"), name="temp_media")

register_routes(app, get_agent_runtime)
app.include_router(oauth_router)


@app.get("/health")
async def health_check():
    try:
        redis_healthy = redis_client.ping()
        return {
            "status": "healthy",
            "process_id": PROCESS_ID,
            "scheduler_leader": is_scheduler_leader(),
            "dependencies": {
                "redis": "healthy" if redis_healthy else "unhealthy",
                "scheduler": "healthy" if scheduler.running else "inactive",
                "langgraph_memory": checkpointer_provider.backend,
            },
            "agents": runtime_registry.status(),
        }
    except Exception as exc:
        return {"status": "unhealthy", "error": str(exc)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=CONFIG["PORT"], log_level="info", workers=1)
