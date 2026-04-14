from typing import Dict, Any, Optional, List, Union


class Plugin:
    """插件基类，所有对话处理插件需继承此类"""

    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description

    async def handle(self, message: Union[str, Dict[str, Any]], from_user: str, context: Dict[str, Any]) -> Optional[str]:
        """
        处理消息的方法

        Args:
            message: 收到的消息内容，可以是文本字符串或包含多媒体信息的字典
            from_user: 发送者标识
            context: 对话上下文

        Returns:
            回复内容，如果不处理该消息则返回None
        """
        return None

    def get_commands(self) -> List[str]:
        """返回该插件支持的命令列表"""
        return []

    def supports_message_type(self, message_type: str) -> bool:
        """
        检查插件是否支持指定类型的消息
        
        Args:
            message_type: 消息类型，如 'text', 'image', 'voice', 'video', 'file', 'location'
            
        Returns:
            是否支持该消息类型
        """
        return message_type == 'text'  # 默认只支持文本消息
