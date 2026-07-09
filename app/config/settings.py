import os

from app.config.yaml_config import get_config


yaml_config = get_config()

CONFIG = {
    "CORP_ID": yaml_config.get("wechat.corp_id"),
    "PORT": yaml_config.get("deployment.port", 4455),
    "DASHSCOPE_API_KEY": yaml_config.get("api_keys.dashscope"),
    "REDIS_HOST": yaml_config.get("redis.host", "localhost"),
    "REDIS_PORT": yaml_config.get("redis.port", 6379),
    "REDIS_PASSWORD": yaml_config.get("redis.password"),
    "REDIS_DB": yaml_config.get("redis.db", 0),
    "MEDIA_URL_BASE": yaml_config.get("deployment.media_url_base", "http://127.0.0.1:4455"),
}

AGENT_CONFIGS = yaml_config.get("wechat.agents", {})


def get_model_config(profile: str = "dashscope") -> dict:
    """Return an OpenAI-compatible chat model config."""
    models = yaml_config.get("models", {}) or {}
    configured = models.get(profile, {}) if isinstance(models, dict) else {}
    defaults = {
        "model": "qwen3.6-flash",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": CONFIG.get("DASHSCOPE_API_KEY") or os.getenv("DASHSCOPE_API_KEY"),
    }
    merged = {**defaults, **configured}
    api_key_config = merged.get("api_key_config")
    api_key_env = merged.get("api_key_env")
    configured_key = merged.get("api_key")
    if api_key_config:
        configured_key = configured_key or yaml_config.get(f"api_keys.{api_key_config}")
    if api_key_env:
        configured_key = configured_key or os.getenv(str(api_key_env))
    merged["api_key"] = configured_key or defaults.get("api_key")
    return merged


def redis_url() -> str:
    password = CONFIG.get("REDIS_PASSWORD")
    auth = f":{password}@" if password else ""
    return f"redis://{auth}{CONFIG['REDIS_HOST']}:{CONFIG['REDIS_PORT']}/{CONFIG['REDIS_DB']}"
