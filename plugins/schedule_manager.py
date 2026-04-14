from datetime import datetime
from typing import Any, Dict, Optional, Union, List
import json
from core.logger import get_logger
from core.plugin import Plugin
from core.redis_client import redis_client
from .schedule_base import ScheduleBase


def send_scheduled_message(user_id: str, message: str, task_id: Optional[str] = None):
    """发送定时消息的独立函数"""
    def _cleanup_once_task():
        if not task_id:
            return
        try:
            task_key = f"schedule_manager:task:{task_id}"
            task_raw = redis_client.get(task_key)
            if task_raw:
                try:
                    task_data = json.loads(task_raw)
                    # 仅一次性(date)任务在发送后清理；周期任务保留
                    if task_data.get("trigger_type") != "date":
                        return
                except Exception:
                    # 解析失败时保守处理：不删除，避免误删周期任务
                    return

            # 删除任务详情
            redis_client.delete(task_key)
            # 从用户任务列表移除
            user_key = f"schedule_manager:user_tasks:{user_id}"
            data = redis_client.get(user_key)
            if data:
                try:
                    task_ids = json.loads(data)
                    if isinstance(task_ids, list) and task_id in task_ids:
                        task_ids.remove(task_id)
                        redis_client.set(user_key, json.dumps(task_ids))
                except Exception:
                    pass
        except Exception as e:
            logger = get_logger("schedule_manager")
            logger.error(f"清理一次性任务失败: {str(e)}")

    try:
        from core.wecom.enterprise_wechat import send_wechat_message
        import asyncio
        try:
            # 尝试获取当前事件循环
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 如果当前循环正在运行，创建新线程来运行
                import threading
                def run_async():
                    new_loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(new_loop)
                    try:
                        new_loop.run_until_complete(send_wechat_message(msg=message, agent_id="1000003", user=user_id))
                    finally:
                        new_loop.close()
                
                thread = threading.Thread(target=run_async)
                thread.start()
                thread.join()
            else:
                # 如果当前循环没有运行，直接运行
                loop.run_until_complete(send_wechat_message(msg=message, agent_id="1000003", user=user_id))
        except RuntimeError:
            # 如果没有事件循环，创建新的
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                new_loop.run_until_complete(send_wechat_message(msg=message, agent_id="1000003", user=user_id))
            finally:
                new_loop.close()
        finally:
            # 一次性(date)任务发送后清理：不依赖 TTL，确保不永久堆积
            _cleanup_once_task()
    except Exception as e:
        logger = get_logger("schedule_manager")
        logger.error(f"发送定时消息失败: {str(e)}")
        _cleanup_once_task()


class ScheduleManagerPlugin(ScheduleBase, Plugin):
    """基础定时任务管理器

    提供基础的定时任务管理功能：
    - 创建/查看/删除/修改定时任务
    - 支持cron、date、interval三种触发器类型
    - 任务持久化存储
    - 与调度器集成
    """

    def __init__(self, name: str = "定时任务管理器", description: str = "管理定时任务"):
        ScheduleBase.__init__(self, namespace="schedule_manager")
        Plugin.__init__(self, name=name, description=description)

    def get_commands(self) -> List[str]:
        return [
            "创建任务 <任务名> <时间表达式> <消息内容>",
            "查看任务",
            "删除任务 <任务ID>",
            "修改任务 <任务ID> <新消息内容>",
        ]

    async def handle(self, message: Union[str, Dict[str, Any]], from_user: str, context: Dict[str, Any]) -> Optional[str]:
        if not isinstance(message, str):
            return None
        text = message.strip()
        if not text:
            return None

        # 基础命令解析
        if text.startswith("创建任务") or text.startswith("添加任务"):
            return self._handle_create_task(text, from_user)
        elif text.startswith("查看任务"):
            return self._list_tasks(from_user)
        elif text.startswith("删除任务"):
            return self._delete_task_command(text, from_user)
        elif text.startswith("修改任务"):
            return self._modify_task(text, from_user)
        
        return None

    def _handle_create_task(self, message: str, from_user: str) -> str:
        """处理创建任务命令"""
        try:
            parts = message.split(' ', 2)
            if len(parts) < 3:
                return "❌ 格式错误！正确格式：创建任务 <任务名> <时间表达式> <消息内容>\n\n💡 时间表达式示例：\n- 每天9点：cron 0 9 * * *\n- 工作日9点：cron 0 9 * * 1-5\n- 30分钟后：date +30m\n- 每5分钟：interval 5m"
            
            task_name = parts[1].strip()
            time_expr = parts[2].strip()
            
            # 解析时间表达式
            time_config = self._parse_time_expression(time_expr)
            if not time_config:
                return "❌ 时间表达式解析失败！\n\n💡 支持格式：\n- cron表达式：cron 0 9 * * *\n- 相对时间：date +30m\n- 间隔时间：interval 5m"
            
            # 提取消息内容（从时间表达式后面开始）
            message_parts = message.split(' ', 3)
            if len(message_parts) >= 4:
                task_message = message_parts[3].strip()
            else:
                task_message = task_name

            task_id = self._generate_task_id(from_user, task_name)
            task_data = {
                'id': task_id,
                'name': task_name,
                'user_id': from_user,
                'message': task_message,
                'created_at': datetime.now().isoformat(),
                **time_config
            }

            self._save_task(task_id, task_data)
            user_tasks = self._load_user_tasks(from_user)
            user_tasks.append(task_id)
            self._save_user_tasks(from_user, user_tasks)

            if self._add_task_to_scheduler(task_id, task_data):
                time_desc = self._format_time_description(task_data)
                return f"✅ 创建成功\n📝 任务：{task_name}\n⏰ 时间：{time_desc}\n💬 消息：{task_message}\n🆔 ID：{task_id}"
            else:
                # 回滚
                self._delete_task(task_id)
                user_tasks.remove(task_id)
                self._save_user_tasks(from_user, user_tasks)
                return "❌ 创建失败，请稍后重试"
        except Exception as e:
            self.logger.error(f"创建任务失败: {str(e)}")
            return f"❌ 创建失败：{str(e)}"