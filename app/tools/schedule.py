from __future__ import annotations

from typing import List, Optional

from langchain_core.tools import tool

from app.services.schedule import schedule_service


def build_schedule_tools(agent_id: str, user_id: str) -> List:
    @tool
    def create_reminder(
        name: str,
        message: str,
        trigger_type: str,
        run_date: Optional[str] = None,
        minute: str = "0",
        hour: str = "*",
        day: str = "*",
        month: str = "*",
        day_of_week: str = "*",
        seconds: int = 0,
        minutes: int = 0,
        hours: int = 0,
        days: int = 0,
    ) -> str:
        """创建提醒任务。trigger_type 为 date、cron 或 interval；date 使用 run_date ISO 时间。"""
        return schedule_service.create_task(
            user_id=user_id,
            agent_id=agent_id,
            name=name,
            message=message,
            trigger_type=trigger_type,
            run_date=run_date,
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            seconds=seconds,
            minutes=minutes,
            hours=hours,
            days=days,
        )

    @tool
    def list_reminders() -> str:
        """查看当前用户的所有提醒任务。"""
        return schedule_service.list_tasks(user_id)

    @tool
    def delete_reminder(task_id: str) -> str:
        """删除当前用户的提醒任务。"""
        return schedule_service.delete_task(user_id, task_id)

    @tool
    def modify_reminder(task_id: str, new_message: str) -> str:
        """修改当前用户提醒任务的消息内容。"""
        return schedule_service.modify_task(user_id, task_id, new_message)

    return [create_reminder, list_reminders, delete_reminder, modify_reminder]
