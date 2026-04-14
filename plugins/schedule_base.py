import re
import json
import random
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Union, List

from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from core.logger import get_logger
from core.redis_client import redis_client
from core.scheduler import scheduler
from core.scheduler import is_scheduler_leader

import threading
import time


class ScheduleBase:
    """定时任务管理基础类
    
    提供通用的定时任务管理功能：
    - 任务存储和检索
    - 时间表达式解析
    - 调度器集成
    - 任务生命周期管理
    """

    def __init__(self, namespace: str = "schedule_manager"):
        """
        初始化基础类
        
        Args:
            namespace: Redis键的命名空间，固定为 schedule_manager 以共享任务数据
        """
        self.namespace = namespace
        self.logger = get_logger(namespace)
        self.redis = redis_client

        # 多进程下：只有 leader 负责跑 APScheduler。为确保“任务在任意 worker 创建后也能被 leader 执行”，
        # leader 进程周期性从 Redis 同步任务到调度器（幂等，replace_existing=True）。
        self._ensure_leader_sync_loop()

    _sync_threads_started: set = set()

    def _ensure_leader_sync_loop(self):
        if not is_scheduler_leader():
            return
        key = self.namespace
        if key in ScheduleBase._sync_threads_started:
            return
        ScheduleBase._sync_threads_started.add(key)

        def _loop():
            # 轻量同步：避免频繁 scan，默认每 5 秒同步一次
            interval = 5
            while True:
                try:
                    restored = self.restore_tasks_from_store()
                    if restored:
                        # self.logger.debug(f"leader同步任务完成，新增/修复调度数量: {restored}")
                        pass
                except Exception as e:
                    self.logger.error(f"leader同步任务异常: {str(e)}")
                time.sleep(interval)

        t = threading.Thread(target=_loop, name=f"schedule_sync_{self.namespace}", daemon=True)
        t.start()

    # =============== 基础功能 ===============
    def _key_namespace(self) -> str:
        """生成Redis键的命名空间"""
        return self.namespace

    def _get_user_tasks_key(self, user_id: str) -> str:
        """获取用户任务的Redis键名"""
        return f"{self._key_namespace()}:user_tasks:{user_id}"

    def _get_task_key(self, task_id: str) -> str:
        """获取任务详情的Redis键名"""
        return f"{self._key_namespace()}:task:{task_id}"

    def _load_user_tasks(self, user_id: str) -> List[str]:
        """加载用户的任务列表"""
        key = self._get_user_tasks_key(user_id)
        data = self.redis.get(key)
        if data:
            try:
                task_ids = json.loads(data)
                if not isinstance(task_ids, list):
                    return []
                # 清理脏数据：去重 + 剔除已不存在的任务ID，避免 user_tasks 累积悬空引用
                cleaned: List[str] = []
                seen = set()
                for tid in task_ids:
                    tid = str(tid).strip()
                    if not tid or tid in seen:
                        continue
                    if self.redis.exists(self._get_task_key(tid)):
                        cleaned.append(tid)
                        seen.add(tid)
                if cleaned != task_ids:
                    self.redis.set(key, json.dumps(cleaned))
                return cleaned
            except Exception:
                return []
        return []

    def _save_user_tasks(self, user_id: str, task_ids: List[str]):
        """保存用户的任务列表"""
        key = self._get_user_tasks_key(user_id)
        self.redis.set(key, json.dumps(task_ids))

    def _load_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        """加载任务详情"""
        key = self._get_task_key(task_id)
        data = self.redis.get(key)
        if data:
            try:
                return json.loads(data)
            except Exception:
                return None
        return None

    def _save_task(self, task_id: str, task_data: Dict[str, Any]):
        """保存任务详情"""
        key = self._get_task_key(task_id)
        # 一次性(date)任务不应永久保存：按 run_date 到期后再保留 60s 作为缓冲，然后自动过期
        ex = None
        if task_data.get("trigger_type") == "date":
            run_date_str = task_data.get("run_date")
            if isinstance(run_date_str, str) and run_date_str:
                try:
                    if run_date_str.endswith("Z"):
                        run_date_str = run_date_str[:-1]
                    run_date = datetime.fromisoformat(run_date_str)
                    ttl = int((run_date - datetime.now()).total_seconds()) + 60
                    ex = max(60, ttl)
                except Exception:
                    # 解析失败则不设置 TTL，避免误删；由发送后清理兜底
                    ex = None
        if ex is not None:
            self.redis.set(key, json.dumps(task_data), ex=ex)
        else:
            self.redis.set(key, json.dumps(task_data))

    def _delete_task(self, task_id: str):
        """删除任务详情"""
        key = self._get_task_key(task_id)
        self.redis.delete(key)

    def _generate_task_id(self, user_id: str, task_name: str) -> str:
        """生成任务ID（随机5位纯数字，带冲突规避）"""
        for _ in range(20):
            candidate = str(random.randint(10000, 99999))
            if not self.redis.exists(self._get_task_key(candidate)):
                return candidate
        try:
            seq_key = f"{self._key_namespace()}:seq"
            seq_val = int(self.redis.incr(seq_key))
            mapped = 10000 + (seq_val % 90000)
            return str(mapped)
        except Exception:
            fallback = 10000 + (int(datetime.now().timestamp()) % 90000)
            return str(fallback)

    # =============== 时间解析功能 ===============
    def _parse_time_expression(self, time_expr: str) -> Optional[Dict[str, Any]]:
        """解析时间表达式"""
        time_expr = time_expr.strip().lower()
        
        # 解析cron表达式
        if time_expr.startswith("cron "):
            cron_parts = time_expr[5:].strip().split()
            if len(cron_parts) == 5:
                return {
                    'trigger_type': 'cron',
                    'minute': cron_parts[0],
                    'hour': cron_parts[1],
                    'day': cron_parts[2],
                    'month': cron_parts[3],
                    'day_of_week': cron_parts[4]
                }
        
        # 解析相对时间
        elif time_expr.startswith("date "):
            date_expr = time_expr[5:].strip()
            if date_expr.startswith("+"):
                # 相对时间
                relative_time = self._parse_relative_time(date_expr[1:])
                if relative_time:
                    return {
                        'trigger_type': 'date',
                        'run_date': relative_time
                    }
            else:
                # 绝对时间
                try:
                    run_date = datetime.fromisoformat(date_expr)
                    return {
                        'trigger_type': 'date',
                        'run_date': run_date.isoformat()
                    }
                except ValueError:
                    pass
        
        # 解析间隔时间
        elif time_expr.startswith("interval "):
            interval_expr = time_expr[9:].strip()
            interval_config = self._parse_interval_time(interval_expr)
            if interval_config:
                return {
                    'trigger_type': 'interval',
                    **interval_config
                }
        
        return None

    def _parse_relative_time(self, time_str: str) -> Optional[str]:
        """解析相对时间表达式"""
        time_str = time_str.strip()
        
        # 匹配 "X分钟后" 或 "X分钟"
        minute_pattern = r'(\d+)\s*m(?:in(?:ute)?s?)?'
        match = re.match(minute_pattern, time_str)
        if match:
            minutes = int(match.group(1))
            future_time = datetime.now() + timedelta(minutes=minutes)
            return future_time.replace(second=0, microsecond=0).isoformat()
        
        # 匹配 "X小时后" 或 "X小时"
        hour_pattern = r'(\d+)\s*h(?:our)?s?'
        match = re.match(hour_pattern, time_str)
        if match:
            hours = int(match.group(1))
            future_time = datetime.now() + timedelta(hours=hours)
            return future_time.replace(second=0, microsecond=0).isoformat()
        
        # 匹配 "X天后" 或 "X天"
        day_pattern = r'(\d+)\s*d(?:ay)?s?'
        match = re.match(day_pattern, time_str)
        if match:
            days = int(match.group(1))
            future_time = datetime.now() + timedelta(days=days)
            return future_time.replace(second=0, microsecond=0).isoformat()
        
        return None

    def _parse_interval_time(self, time_str: str) -> Optional[Dict[str, int]]:
        """解析间隔时间表达式"""
        time_str = time_str.strip()
        
        # 匹配 "X分钟"
        minute_pattern = r'(\d+)\s*m(?:in(?:ute)?s?)?'
        match = re.match(minute_pattern, time_str)
        if match:
            return {'minutes': int(match.group(1))}
        
        # 匹配 "X小时"
        hour_pattern = r'(\d+)\s*h(?:our)?s?'
        match = re.match(hour_pattern, time_str)
        if match:
            return {'hours': int(match.group(1))}
        
        # 匹配 "X天"
        day_pattern = r'(\d+)\s*d(?:ay)?s?'
        match = re.match(day_pattern, time_str)
        if match:
            return {'days': int(match.group(1))}
        
        return None

    # =============== 调度器集成 ===============
    def _add_task_to_scheduler(self, task_id: str, task_data: Dict[str, Any]) -> bool:
        """将任务添加到调度器"""
        # 多进程：只有 leader 进程运行 scheduler。非 leader 只负责持久化任务，
        # 由 leader 的同步线程 restore_tasks_from_store() 拉起调度，避免跨进程 add_job 的不可见/竞态问题。
        if not is_scheduler_leader():
            return True
        try:
            trigger_type = task_data.get('trigger_type', 'cron')
            
            if trigger_type == 'cron':
                trigger = CronTrigger(
                    minute=task_data.get('minute', '*'),
                    hour=task_data.get('hour', '*'),
                    day=task_data.get('day', '*'),
                    month=task_data.get('month', '*'),
                    day_of_week=task_data.get('day_of_week', '*')
                )
            elif trigger_type == 'interval':
                trigger = IntervalTrigger(
                    seconds=task_data.get('seconds', 0),
                    minutes=task_data.get('minutes', 0),
                    hours=task_data.get('hours', 0),
                    days=task_data.get('days', 0)
                )
            elif trigger_type == 'date':
                run_date_str = task_data.get('run_date')
                if not run_date_str:
                    return False
                
                try:
                    if run_date_str.endswith('Z'):
                        run_date_str = run_date_str[:-1]
                    run_date = datetime.fromisoformat(run_date_str)
                    trigger = DateTrigger(run_date=run_date)
                except Exception as e:
                    self.logger.error(f"解析日期失败: {run_date_str}, 错误: {str(e)}")
                    return False
            else:
                return False
            
            scheduler.add_job(
                func='plugins.schedule_manager:send_scheduled_message',
                trigger=trigger,
                args=[task_data['user_id'], task_data['message'], task_id],
                id=task_id,
                replace_existing=True,
                misfire_grace_time=1
            )
            
            return True
        except Exception as e:
            self.logger.error(f"添加任务到调度器失败: {str(e)}")
            return False

    def _remove_task_from_scheduler(self, task_id: str) -> bool:
        """从调度器中移除任务"""
        try:
            scheduler.remove_job(task_id)
            return True
        except Exception as e:
            self.logger.error(f"从调度器移除任务失败: {str(e)}")
            return False

    # =============== 任务恢复 ===============
    def restore_tasks_from_store(self) -> int:
        """从存储中恢复定时任务"""
        restored = 0
        try:
            # 遍历本插件命名空间下的任务定义并恢复到调度器
            for key in self.redis.scan_iter(f"{self._key_namespace()}:task:*"):
                data = self.redis.get(key)
                if not data:
                    continue
                try:
                    task = json.loads(data)
                except Exception:
                    continue
                task_id = task.get("id")
                if not task_id:
                    continue
                # 若是一次性(date)且已过期则跳过
                if task.get('trigger_type') == 'date':
                    try:
                        from datetime import datetime
                        run_date_str = task.get('run_date')
                        if run_date_str:
                            if run_date_str.endswith('Z'):
                                run_date_str = run_date_str[:-1]
                            run_date = datetime.fromisoformat(run_date_str)
                            if run_date < datetime.now():
                                continue
                    except Exception:
                        pass
                if self._add_task_to_scheduler(task_id, task):
                    restored += 1
        except Exception:
            # 保持宁静失败，调用方可根据日志排查
            pass
        return restored

    # =============== 时间描述格式化 ===============
    def _format_time_description(self, task_data: Dict[str, Any]) -> str:
        """格式化时间描述"""
        trigger_type = task_data.get('trigger_type', 'cron')
        
        if trigger_type == 'cron':
            if task_data.get('day_of_week') and task_data.get('day_of_week') != '*':
                day_of_week = task_data['day_of_week']
                if day_of_week == 'mon-fri':
                    return f"工作日 {task_data.get('hour', '*')}:{task_data.get('minute', '0')}:{task_data.get('seconds', '0')}"
                elif day_of_week == 'sat-sun':
                    return f"周六日 {task_data.get('hour', '*')}:{task_data.get('minute', '0')}:{task_data.get('seconds', '0')}"
                elif ',' in day_of_week:
                    # 处理多个星期几的情况
                    day_map = {'mon': '周一', 'tue': '周二', 'wed': '周三', 'thu': '周四', 'fri': '周五', 'sat': '周六', 'sun': '周日'}
                    days = [day_map.get(d.strip(), d.strip()) for d in day_of_week.split(',')]
                    return f"每周{','.join(days)} {task_data.get('hour', '*')}:{task_data.get('minute', '0')}:{task_data.get('seconds', '0')}"
                else:
                    day_map = {'mon': '周一', 'tue': '周二', 'wed': '周三', 'thu': '周四', 'fri': '周五', 'sat': '周六', 'sun': '周日'}
                    day = day_map.get(day_of_week, day_of_week)
                    return f"每周{day} {task_data.get('hour', '*')}:{task_data.get('minute', '0')}:{task_data.get('seconds', '0')}"
            elif task_data.get('day') and task_data.get('day') != '*':
                return f"每月{task_data['day']}号 {task_data.get('hour', '*')}:{task_data.get('minute', '0')}:{task_data.get('seconds', '0')}"
            else:
                return f"每天 {task_data.get('hour', '*')}:{task_data.get('minute', '0')}:{task_data.get('seconds', '0')}"
        elif trigger_type == 'date':
            try:
                run_date_str = task_data.get('run_date', '')
                if run_date_str.endswith('Z'):
                    run_date_str = run_date_str[:-1]
                run_date = datetime.fromisoformat(run_date_str)
                now = datetime.now()
                
                time_diff = run_date - now
                if time_diff.total_seconds() < 0:
                    return f"仅一次 {run_date.strftime('%Y-%m-%d %H:%M:%S')} (已过期)"
                
                if run_date.date() == now.date():
                    return f"今天 {run_date.strftime('%H:%M:%S')}"
                
                tomorrow = now + timedelta(days=1)
                if run_date.date() == tomorrow.date():
                    return f"明天 {run_date.strftime('%H:%M:%S')}"
                
                if time_diff.total_seconds() < 86400:
                    hours = int(time_diff.total_seconds() // 3600)
                    minutes = int((time_diff.total_seconds() % 3600) // 60)
                    if hours > 0:
                        if minutes > 0:
                            return f"{hours}小时{minutes}分钟后"
                        else:
                            return f"{hours}小时后"
                    else:
                        return f"{minutes}分钟后"
                
                return f"仅一次 {run_date.strftime('%Y-%m-%d %H:%M:%S')}"
            except Exception:
                return "仅一次（时间解析失败）"
        
        return "自定义时间"

    # =============== 任务列表管理 ===============
    def _list_tasks(self, user_id: str) -> str:
        """列出用户的所有任务"""
        task_ids = self._load_user_tasks(user_id)
        if not task_ids:
            return "📝 你还没有任何定时任务"
        
        result = ["📅 你的定时任务列表："]
        for i, task_id in enumerate(task_ids, 1):
            task_data = self._load_task(task_id)
            if task_data:
                # 多进程下只有 leader 的 scheduler 在运行；其它进程本地 get_job 会误判为空。
                # 这里直接查 APScheduler RedisJobStore（apscheduler.jobs）确保所有 worker 结果一致。
                try:
                    status = "✅ 运行中" if self.redis.hexists("apscheduler.jobs", task_id) else "❌ 已停止"
                except Exception:
                    status = "❓ 状态未知"
                time_desc = self._format_time_description(task_data)
                result.append(f"{i}. [{task_id}] {task_data['name']} - {time_desc} - {status}")
                result.append(f"   消息：{task_data['message']}")
        
        return "\n".join(result)

    def _delete_task_command(self, message: str, user_id: str) -> str:
        """删除任务命令"""
        try:
            parts = message.split(' ', 1)
            if len(parts) < 2:
                return "❌ 格式错误！正确格式：删除任务 <任务ID>\n\n💡 使用 查看任务 获取任务ID"
            
            task_id = parts[1].strip()
            
            # 检查任务是否存在且属于该用户
            task_data = self._load_task(task_id)
            if not task_data:
                return "❌ 任务不存在"
            
            if task_data['user_id'] != user_id:
                return "❌ 你只能删除自己的任务"
            
            # 从调度器移除
            self._remove_task_from_scheduler(task_id)
            
            # 从用户任务列表移除
            user_tasks = self._load_user_tasks(user_id)
            if task_id in user_tasks:
                user_tasks.remove(task_id)
                self._save_user_tasks(user_id, user_tasks)
            
            # 删除任务数据
            self._delete_task(task_id)
            
            return f"✅ 任务删除成功：{task_data['name']}"
            
        except Exception as e:
            self.logger.error(f"删除任务失败: {str(e)}")
            return f"❌ 删除任务失败：{str(e)}"

    def _modify_task(self, message: str, user_id: str) -> str:
        """修改任务"""
        try:
            parts = message.split(' ', 2)
            if len(parts) < 3:
                return "❌ 格式错误！正确格式：修改任务 <任务ID> <新消息内容>\n\n💡 使用 查看任务 获取任务ID"
            
            task_id = parts[1].strip()
            new_message = parts[2].strip()
            
            # 检查任务是否存在且属于该用户
            task_data = self._load_task(task_id)
            if not task_data:
                return "❌ 任务不存在"
            
            if task_data['user_id'] != user_id:
                return "❌ 你只能修改自己的任务"
            
            # 更新任务数据
            task_data['message'] = new_message
            task_data['updated_at'] = datetime.now().isoformat()
            
            # 保存更新后的任务
            self._save_task(task_id, task_data)
            
            # 重新添加到调度器
            self._remove_task_from_scheduler(task_id)
            self._add_task_to_scheduler(task_id, task_data)
            
            return f"✅ 任务修改成功！\n\n📝 任务名称：{task_data['name']}\n💬 新消息内容：{new_message}"
            
        except Exception as e:
            self.logger.error(f"修改任务失败: {str(e)}")
            return f"❌ 修改任务失败：{str(e)}"
