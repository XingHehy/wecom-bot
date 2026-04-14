import os
import yaml
from typing import Dict, Any, Optional
from pathlib import Path

class YAMLConfig:
    """YAML 配置管理器"""
    
    def __init__(self, config_path: str = "config.yaml"):
        self.config_path = Path(config_path)
        self._config = None
        self._load_config()
    
    def _load_config(self):
        """加载 YAML 配置文件"""
        try:
            if self.config_path.exists():
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    self._config = yaml.safe_load(f)
                print(f"成功加载配置文件: {self.config_path}")
            else:
                print(f"配置文件不存在: {self.config_path}")
                self._config = {}
        except Exception as e:
            print(f"加载配置文件失败: {e}")
            self._config = {}
    
    def reload(self):
        """重新加载配置文件"""
        self._load_config()
    
    def get(self, key_path: str, default: Any = None) -> Any:
        """获取配置值，支持点号分隔的路径"""
        if not self._config:
            return default
        
        keys = key_path.split('.')
        value = self._config
        
        try:
            for key in keys:
                if isinstance(value, dict) and key in value:
                    value = value[key]
                else:
                    return default
            return value
        except (KeyError, TypeError):
            return default
    
    def get_wechat_config(self) -> Dict[str, Any]:
        """获取企业微信配置"""
        return {
            'corp_id': self.get('wechat.corp_id'),
            'agents': self.get('wechat.agents', {})
        }
    
    def get_redis_config(self) -> Dict[str, Any]:
        """获取 Redis 配置"""
        redis_config = self.get('redis', {})
        # 处理 null 值
        if redis_config.get('password') is None:
            redis_config['password'] = None
        return redis_config
    
    def get_logging_config(self) -> Dict[str, Any]:
        """获取日志配置"""
        return self.get('logging', {})
    
    def get_scheduler_config(self) -> Dict[str, Any]:
        """获取定时任务配置"""
        return self.get('scheduler', {})
    
    def get_api_keys(self) -> Dict[str, str]:
        """获取 API 密钥配置"""
        return self.get('api_keys', {})
    
    def get_deployment_config(self) -> Dict[str, Any]:
        """获取部署配置"""
        return self.get('deployment', {})
    
    def get_agent_config(self, agent_id: str) -> Optional[Dict[str, Any]]:
        """获取指定 agent 的配置"""
        agents = self.get('wechat.agents', {})
        return agents.get(str(agent_id))
    
    def list_agent_ids(self) -> list:
        """列出所有可用的 agent ID"""
        agents = self.get('wechat.agents', {})
        return list(agents.keys())
    
    def validate_config(self) -> bool:
        """验证配置的完整性"""
        required_fields = [
            'wechat.corp_id',
            'wechat.agents'
        ]
        
        for field in required_fields:
            if not self.get(field):
                print(f"缺少必需配置: {field}")
                return False
        
        # 验证至少有一个 agent 配置
        agents = self.get('wechat.agents', {})
        if not agents:
            print("至少需要配置一个 agent")
            return False
        
        # 验证每个 agent 的必需字段
        for agent_id, agent_config in agents.items():
            if not isinstance(agent_config, dict):
                print(f"Agent {agent_id} 配置格式错误")
                return False
            
            required_agent_fields = ['corp_secret', 'agent_id']
            for field in required_agent_fields:
                if not agent_config.get(field):
                    print(f"Agent {agent_id} 缺少必需字段: {field}")
                    return False
        
        print("配置验证通过")
        return True

# 全局配置实例
yaml_config = YAMLConfig()

# 便捷访问函数
def get_config() -> YAMLConfig:
    """获取全局配置实例"""
    return yaml_config

def reload_config():
    """重新加载配置"""
    yaml_config.reload()
