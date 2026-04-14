from typing import Optional, Dict, Any, List
import datetime
from core.plugin import Plugin

class DemoPlugin(Plugin):
    """基础对话插件，提供问候、时间查询和帮助功能"""
    
    def __init__(self):
        super().__init__(
            name="基础对话",
            description="提供问候、时间查询和帮助功能"
        )
    
    async def handle(self, message: str, from_user: str, context: Dict[str, Any]) -> Optional[str]:
        message_lower = message.lower()
        
        if "你好" in message_lower:
            return f"你好！已收到你的消息：{message}"
        elif "时间" in message_lower:
            now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            return f"当前时间：{now}"
        elif "帮助" in message_lower:
            return self._get_help_message(context)
        
        return None
    
    def get_commands(self) -> List[str]:
        return ["你好 - 打招呼", "时间 - 获取当前时间", "帮助 - 查看帮助"]
    
    def _get_help_message(self, context: Dict[str, Any]) -> str:
        """生成帮助信息，汇总所有插件的命令"""
        help_msg = "支持的命令：\n"
        plugin_manager = context.get("plugin_manager")
        
        if plugin_manager:
            for plugin in plugin_manager.plugins:
                commands = plugin.get_commands()
                if commands:
                    help_msg += f"\n{plugin.name}:\n"
                    help_msg += "\n".join([f"- {cmd}" for cmd in commands])
        
        return help_msg
    