import asyncio
from typing import List

from core.logger import get_logger
from core.task_registry import scheduled_task
from core.wecom.enterprise_wechat import enqueue_active_message


logger = get_logger("demo_task")
AGENT_ID = "1000003"


@scheduled_task(key="demo", mode="batch", default_time="09:00")
def send_demo_message_batch(user_ids: List[str]):
    """演示定时任务：按配置用户批量发送一条固定文本。"""
    if not user_ids:
        logger.warning("demo 任务用户列表为空，跳过")
        return

    msg = "这是一条定时任务 Demo 消息。"
    touser = "|".join([u for u in user_ids if u])
    if not touser:
        logger.warning("demo 任务有效用户为空，跳过")
        return

    try:
        asyncio.run(enqueue_active_message(agent_id=AGENT_ID, msg=msg, user=touser))
        logger.info(f"demo 任务已入队，接收人: {touser}")
    except Exception as e:
        logger.error(f"demo 任务执行失败: {str(e)}")
