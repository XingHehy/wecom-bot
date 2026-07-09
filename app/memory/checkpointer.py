from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import Any, Optional

from app.config.settings import redis_url
from app.logger import get_logger

logger = get_logger("memory")


class CheckpointerProvider:
    """Lazy LangGraph checkpointer provider.

    RedisSaver requires Redis Stack modules. If the server is plain Redis or the
    package is unavailable, we fall back to in-memory checkpoints so the bot can
    still boot while surfacing the degraded mode in logs/health.
    """

    def __init__(self):
        self._checkpointer: Optional[Any] = None
        self._ctx: Optional[AbstractAsyncContextManager] = None
        self.backend = "uninitialized"
        self.error: Optional[str] = None

    async def get(self) -> Any:
        if self._checkpointer is not None:
            return self._checkpointer

        try:
            from langgraph.checkpoint.redis.aio import AsyncRedisSaver

            self._ctx = AsyncRedisSaver.from_conn_string(redis_url())
            self._checkpointer = await self._ctx.__aenter__()
            setup = getattr(self._checkpointer, "asetup", None)
            if setup:
                await setup()
            self.backend = "redis"
            self.error = None
            return self._checkpointer
        except Exception as exc:
            self.error = str(exc)
            logger.warning(f"Redis checkpointer不可用，降级为内存checkpoint: {exc}")

        from langgraph.checkpoint.memory import InMemorySaver

        self._checkpointer = InMemorySaver()
        self.backend = "memory"
        return self._checkpointer

    async def close(self) -> None:
        if self._ctx is not None:
            try:
                await self._ctx.__aexit__(None, None, None)
            finally:
                self._ctx = None
                self._checkpointer = None
                self.backend = "uninitialized"


checkpointer_provider = CheckpointerProvider()


def thread_id_for(agent_id: str, user_id: str) -> str:
    return f"wecom:{agent_id}:{user_id}"
