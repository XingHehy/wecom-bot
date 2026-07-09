from __future__ import annotations

import asyncio
import json
import random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.logger import get_logger
from app.redis_client import redis_client
from app.scheduler.runtime import is_scheduler_leader, scheduler

logger = get_logger("schedule_service")


async def send_scheduled_message(user_id: str, message: str, agent_id: str, task_id: Optional[str] = None):
    """APScheduler entrypoint for user-created reminders."""
    from app.wecom.enterprise_wechat import enqueue_active_message

    try:
        await enqueue_active_message(agent_id=agent_id, msg=message, user=user_id)
    finally:
        if task_id:
            ScheduleService().cleanup_once_task(task_id, user_id)


def send_scheduled_message_sync(user_id: str, message: str, agent_id: str, task_id: Optional[str] = None):
    asyncio.run(send_scheduled_message(user_id, message, agent_id, task_id))


class ScheduleService:
    namespace = "schedule_manager"

    def _user_tasks_key(self, user_id: str) -> str:
        return f"{self.namespace}:user_tasks:{user_id}"

    def _task_key(self, task_id: str) -> str:
        return f"{self.namespace}:task:{task_id}"

    def _load_user_task_ids(self, user_id: str) -> List[str]:
        raw = redis_client.get(self._user_tasks_key(user_id))
        if not raw:
            return []
        try:
            ids = json.loads(raw)
        except Exception:
            return []
        cleaned = []
        for task_id in ids if isinstance(ids, list) else []:
            if task_id and redis_client.exists(self._task_key(str(task_id))):
                cleaned.append(str(task_id))
        if cleaned != ids:
            redis_client.set(self._user_tasks_key(user_id), json.dumps(cleaned, ensure_ascii=False))
        return cleaned

    def _save_user_task_ids(self, user_id: str, ids: List[str]) -> None:
        redis_client.set(self._user_tasks_key(user_id), json.dumps(ids, ensure_ascii=False))

    def _load_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        raw = redis_client.get(self._task_key(task_id))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def _save_task(self, task: Dict[str, Any]) -> None:
        ttl = None
        if task.get("trigger_type") == "date" and task.get("run_date"):
            try:
                run_date = datetime.fromisoformat(str(task["run_date"]).rstrip("Z"))
                ttl = max(60, int((run_date - datetime.now()).total_seconds()) + 60)
            except Exception:
                ttl = None
        payload = json.dumps(task, ensure_ascii=False)
        if ttl:
            redis_client.set(self._task_key(task["id"]), payload, ex=ttl)
        else:
            redis_client.set(self._task_key(task["id"]), payload)

    def _generate_task_id(self) -> str:
        for _ in range(20):
            task_id = str(random.randint(10000, 99999))
            if not redis_client.exists(self._task_key(task_id)):
                return task_id
        return str(10000 + (int(redis_client.incr(f"{self.namespace}:seq")) % 90000))

    def create_task(
        self,
        *,
        user_id: str,
        agent_id: str,
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
        task_id = self._generate_task_id()
        task = {
            "id": task_id,
            "name": name[:20] or "提醒",
            "user_id": user_id,
            "agent_id": agent_id,
            "message": message or name or "提醒",
            "trigger_type": trigger_type,
            "created_at": datetime.now().isoformat(),
        }
        if trigger_type == "date":
            if not run_date:
                return "创建失败：一次性提醒必须提供 run_date"
            task["run_date"] = run_date
        elif trigger_type == "interval":
            task.update({"seconds": seconds, "minutes": minutes, "hours": hours, "days": days})
        elif trigger_type == "cron":
            task.update({"minute": str(minute), "hour": str(hour), "day": str(day), "month": str(month), "day_of_week": str(day_of_week)})
        else:
            return "创建失败：trigger_type 只能是 date、cron 或 interval"

        self._save_task(task)
        ids = self._load_user_task_ids(user_id)
        ids.append(task_id)
        self._save_user_task_ids(user_id, ids)

        if not self.add_task_to_scheduler(task):
            return "提醒已保存，等待调度器 leader 同步。"

        return (
            f"创建成功\n任务：{task['name']}\n时间：{self.format_time(task)}\n"
            f"消息：{task['message']}\nID：{task_id}"
        )

    def add_task_to_scheduler(self, task: Dict[str, Any]) -> bool:
        if not is_scheduler_leader():
            return True
        try:
            trigger_type = task.get("trigger_type")
            if trigger_type == "cron":
                trigger = CronTrigger(
                    minute=task.get("minute", "0"),
                    hour=task.get("hour", "*"),
                    day=task.get("day", "*"),
                    month=task.get("month", "*"),
                    day_of_week=task.get("day_of_week", "*"),
                )
            elif trigger_type == "interval":
                trigger = IntervalTrigger(
                    seconds=int(task.get("seconds", 0) or 0),
                    minutes=int(task.get("minutes", 0) or 0),
                    hours=int(task.get("hours", 0) or 0),
                    days=int(task.get("days", 0) or 0),
                )
            elif trigger_type == "date":
                run_date = datetime.fromisoformat(str(task.get("run_date", "")).rstrip("Z"))
                if run_date < datetime.now():
                    return False
                trigger = DateTrigger(run_date=run_date)
            else:
                return False

            scheduler.add_job(
                func="app.services.schedule:send_scheduled_message_sync",
                trigger=trigger,
                args=[task["user_id"], task["message"], task["agent_id"], task["id"]],
                id=task["id"],
                replace_existing=True,
                misfire_grace_time=30,
            )
            return True
        except Exception as exc:
            logger.error(f"添加提醒到调度器失败: {exc}")
            return False

    def restore_tasks_from_store(self) -> int:
        restored = 0
        for key in redis_client.scan_iter(f"{self.namespace}:task:*"):
            task = self._load_task(str(key).split(":")[-1])
            if task and self.add_task_to_scheduler(task):
                restored += 1
        return restored

    def list_tasks(self, user_id: str) -> str:
        ids = self._load_user_task_ids(user_id)
        if not ids:
            return "你还没有任何定时任务"
        lines = ["你的定时任务列表："]
        for index, task_id in enumerate(ids, 1):
            task = self._load_task(task_id)
            if not task:
                continue
            try:
                status = "运行中" if redis_client.hexists("apscheduler.jobs", task_id) else "待同步"
            except Exception:
                status = "状态未知"
            lines.append(f"{index}. [{task_id}] {task['name']} - {self.format_time(task)} - {status}")
            lines.append(f"   消息：{task['message']}")
        return "\n".join(lines)

    def delete_task(self, user_id: str, task_id: str) -> str:
        task = self._load_task(task_id)
        if not task or task.get("user_id") != user_id:
            return "任务不存在"
        try:
            scheduler.remove_job(task_id)
        except Exception:
            pass
        redis_client.delete(self._task_key(task_id))
        ids = [tid for tid in self._load_user_task_ids(user_id) if tid != task_id]
        self._save_user_task_ids(user_id, ids)
        return f"任务删除成功：{task.get('name', task_id)}"

    def modify_task(self, user_id: str, task_id: str, new_message: str) -> str:
        task = self._load_task(task_id)
        if not task or task.get("user_id") != user_id:
            return "任务不存在"
        task["message"] = new_message
        task["updated_at"] = datetime.now().isoformat()
        self._save_task(task)
        try:
            scheduler.remove_job(task_id)
        except Exception:
            pass
        self.add_task_to_scheduler(task)
        return f"任务修改成功：{task['name']}\n新消息：{new_message}"

    def cleanup_once_task(self, task_id: str, user_id: str) -> None:
        task = self._load_task(task_id)
        if not task or task.get("trigger_type") != "date":
            return
        redis_client.delete(self._task_key(task_id))
        self._save_user_task_ids(user_id, [tid for tid in self._load_user_task_ids(user_id) if tid != task_id])

    def format_time(self, task: Dict[str, Any]) -> str:
        trigger_type = task.get("trigger_type")
        if trigger_type == "date":
            return f"仅一次 {task.get('run_date')}"
        if trigger_type == "interval":
            parts = []
            for key, label in (("days", "天"), ("hours", "小时"), ("minutes", "分钟"), ("seconds", "秒")):
                value = int(task.get(key, 0) or 0)
                if value:
                    parts.append(f"{value}{label}")
            return "每隔" + "".join(parts) if parts else "间隔时间未设置"
        return (
            f"cron minute={task.get('minute', '0')} hour={task.get('hour', '*')} "
            f"day={task.get('day', '*')} month={task.get('month', '*')} dow={task.get('day_of_week', '*')}"
        )


schedule_service = ScheduleService()
