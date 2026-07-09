from __future__ import annotations

from typing import List

from langchain_core.tools import tool


def build_wecom_tools(agent_id: str, user_id: str) -> List:
    @tool
    async def send_wecom_text(message: str) -> str:
        """主动给当前用户发送一条企业微信文本消息。普通回复无需调用此工具。"""
        from app.wecom.enterprise_wechat import enqueue_active_message

        await enqueue_active_message(agent_id=agent_id, msg=message, user=user_id)
        return "已发送"

    return [send_wecom_text]
