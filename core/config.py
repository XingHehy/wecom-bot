import os
from .yaml_config import get_config

"""系统配置参数 - 从YAML配置文件读取"""
yaml_config = get_config()

CONFIG = {
    "CORP_ID": yaml_config.get("wechat.corp_id"),  # 企业ID - 从YAML配置读取
    "PORT": yaml_config.get("deployment.port", 4455),  # 服务端口
    "PLUGINS_DIR": os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "plugins")),  # 插件目录
    "DASHSCOPE_API_KEY": yaml_config.get("api_keys.dashscope"),  # 阿里云千问API Key
    "ARK_API_KEY": yaml_config.get("api_keys.ark"),   # 火山引擎API Key
    "REDIS_HOST": yaml_config.get("redis.host", "localhost"),
    "REDIS_PORT": yaml_config.get("redis.port", 6379),
    "REDIS_PASSWORD": yaml_config.get("redis.password"),
    "REDIS_DB": yaml_config.get("redis.db", 0),
}

# Agent配置 - 从YAML配置读取
AGENT_CONFIGS = yaml_config.get("wechat.agents", {})

# 确保插件目录存在
if not os.path.exists(CONFIG["PLUGINS_DIR"]):
    os.makedirs(CONFIG["PLUGINS_DIR"])
