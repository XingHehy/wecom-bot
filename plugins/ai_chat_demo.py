from typing import List

from plugins.ai_chat.base import BaseAIChatPlugin


DEFAULT_SYSTEM_PROMPT = """你是企业微信机器人中的演示 AI 助手。
请遵循以下规则：
1. 优先给出直接可执行的答案，避免空话。
2. 涉及代码时，尽量给出简洁示例并解释关键点。
3. 回答风格专业、友好、简明。
4. 不编造事实，不确定时明确说明。"""


class AIChatDemoPlugin(BaseAIChatPlugin):
    """通用 AI 对话演示插件"""

    def __init__(self):
        super().__init__(
            name="AI 对话 Demo",
            description="通用 AI 对话演示插件",
            default_system_prompt=DEFAULT_SYSTEM_PROMPT,
            api_key_config="DASHSCOPE_API_KEY",
        )

    def get_commands(self) -> List[str]:
        return [
            "/开启新对话 - 清除对话历史",
            "/重置角色 - 重置为默认角色",
            "/设定角色：<内容> - 设置自定义角色",
        ]
