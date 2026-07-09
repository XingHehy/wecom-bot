import asyncio
import random
from typing import List
from app.wecom.enterprise_wechat import enqueue_active_message
from app.scheduler.task_registry import scheduled_task
from app.logger import get_logger

logger = get_logger("morning_task")
agent_id = "1000003"
message_pool = [
    "早安，祝你今天愉快！",
    "新的一天开始了，愿你充满活力！",
    "早上好，今天也要加油哦！",
    "清晨的阳光很美，就像你的心情一样灿烂～",
    "早安！希望今天的你一切顺利，收获满满！",
    "早上好，送你一份好心情，开启美好的一天！",
    "新的一天，新的希望，早安！",
    "愿你今天有甜甜的心情和满满的好运，早安～",
    "清晨的问候，愿你今天被温柔以待！",
    "早安！今天也是努力发光的一天呀！"
    ]

def send_good_morning(user_id: str):
    """定时发送早安消息"""
    logger.info(f"开始执行早安消息发送任务，接收人: {user_id}")

    try:
        msg = random.choice(message_pool)
        logger.info(f"选择的早安消息: {msg}")

        # 到时间仅入队，由后台发送队列按序发送
        asyncio.run(enqueue_active_message(agent_id=agent_id, msg=msg, user=user_id))
        logger.info(f"早安消息已加入后台队列，接收人: {user_id}")

    except Exception as e:
        logger.error(f"早安消息发送失败，接收人: {user_id}, 错误: {str(e)}")


@scheduled_task(key="morning", mode="batch", default_time="08:30")
def send_good_morning_batch(user_ids: List[str]):
    """批量入队早安消息（由调度器单任务触发）"""
    
    try:
        if not user_ids:
            logger.warning("早安批量任务用户列表为空，跳过")
            return
        logger.info(f"开始批量入队早安消息，人数: {len(user_ids)}，接收人: {user_ids}")
        # 统一选择一条消息，使用“|”拼接用户，一次发送给所有人
        msg = random.choice(message_pool)
        logger.info(f"选择的早安消息: {msg}")
        touser = "|".join(user_ids)
        asyncio.run(enqueue_active_message(agent_id=agent_id, msg=msg, user=touser))
        logger.info("早安消息已加入后台队列（批量），接收人: %s" % touser)
    except Exception as e:
        logger.error(f"批量入队早安消息失败，错误: {str(e)}")
