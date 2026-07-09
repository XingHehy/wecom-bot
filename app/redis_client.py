import redis

from app.config.settings import CONFIG
from app.logger import get_logger


class RedisClient:
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        self.logger = get_logger("redis")
        self.client = redis.Redis(
            host=CONFIG["REDIS_HOST"],
            port=CONFIG["REDIS_PORT"],
            password=CONFIG["REDIS_PASSWORD"],
            db=CONFIG["REDIS_DB"],
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5,
        )
        self.logger.info(
            f"Redis客户端已创建 (host: {CONFIG['REDIS_HOST']}:{CONFIG['REDIS_PORT']} db: {CONFIG['REDIS_DB']})"
        )

    def get_client(self):
        return self.client


redis_client = RedisClient().get_client()
