import redis
from core.config import CONFIG
from core.logger import get_logger

class RedisClient:
    """Redis客户端单例类"""
    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
            cls._instance._initialize()
        return cls._instance

    def _initialize(self):
        """初始化Redis连接"""
        self.logger = get_logger("redis")
        try:
            # 从配置文件读取Redis连接参数
            self.client = redis.Redis(
                host=CONFIG["REDIS_HOST"],
                port=CONFIG["REDIS_PORT"],
                password=CONFIG["REDIS_PASSWORD"],
                db=CONFIG["REDIS_DB"],
                decode_responses=True,  # 自动将bytes转换为字符串
                socket_timeout=5,
                socket_connect_timeout=5
            )
            # 测试连接
            self.client.ping()
            self.logger.info(f"Redis客户端初始化成功 (host: {CONFIG['REDIS_HOST']}:{CONFIG['REDIS_PORT']})")
        except Exception as e:
            self.logger.error(f"Redis客户端初始化失败: {str(e)}", exc_info=True)
            raise  # 初始化失败时终止应用

    def get_client(self):
        """获取Redis客户端实例"""
        return self.client

# 全局Redis客户端实例
redis_client = RedisClient().get_client()
    