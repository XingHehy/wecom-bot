"""定时任务调度器，支持多进程安全"""
import importlib
import os
import pkgutil
import threading
import time
import uuid
from typing import List

from apscheduler.executors.pool import ThreadPoolExecutor as APSThreadPoolExecutor
from apscheduler.jobstores.redis import RedisJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from core.config import CONFIG
from core.logger import get_logger
from core.redis_client import redis_client
from core.yaml_config import get_config

# 进程ID
PROCESS_ID = os.getpid()

# 选主锁：确保多进程下只有一个调度器实例在运行
SCHEDULER_LEADER_LOCK_KEY = os.getenv("SCHEDULER_LEADER_LOCK_KEY", "scheduler:leader_lock")
SCHEDULER_LEADER_LOCK_TTL_SEC = int(os.getenv("SCHEDULER_LEADER_LOCK_TTL_SEC", "60"))
SCHEDULER_LEADER_FORCE_TAKEOVER = os.getenv("SCHEDULER_LEADER_FORCE_TAKEOVER", "true").lower() in ("1", "true", "yes", "on")
_leader_token: str | None = None
_leader_renew_thread: threading.Thread | None = None

# 获取YAML配置
yaml_config = get_config()

# 配置Redis任务存储
jobstores = {
    'default': RedisJobStore(
        host=CONFIG["REDIS_HOST"],
        port=CONFIG["REDIS_PORT"],
        password=CONFIG["REDIS_PASSWORD"],
        db=CONFIG["REDIS_DB"],
    )
}

# 配置线程池（2个线程）
executors = {
    'default': APSThreadPoolExecutor(10)
}

# 全局单例调度器
scheduler = AsyncIOScheduler(
    jobstores=jobstores,
    executors=executors,
    job_defaults={'coalesce': False, 'max_instances': 1, 'misfire_grace_time': 1}
)
logger = get_logger("scheduler")

_RENEW_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('expire', KEYS[1], ARGV[2])
else
  return 0
end
"""

_RELEASE_LUA = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
else
  return 0
end
"""


def is_scheduler_leader() -> bool:
    """当前进程是否为调度器 leader（拥有启动权）"""
    return _leader_token is not None


def _acquire_leader_lock() -> bool:
    global _leader_token
    if _leader_token:
        return True
    token = f"{PROCESS_ID}:{uuid.uuid4().hex}"
    try:
        # 可选：强制接管（仅当你确认旧实例已停止但锁残留时使用）
        # 风险：如果旧 leader 仍在运行，会导致双调度器并发，可能重复发送。
        if SCHEDULER_LEADER_FORCE_TAKEOVER:
            try:
                redis_client.delete(SCHEDULER_LEADER_LOCK_KEY)
                logger.warning(f"已强制清除调度器 leader 锁并尝试接管，key={SCHEDULER_LEADER_LOCK_KEY}")
            except Exception as e:
                logger.error(f"强制清除 leader 锁失败: {str(e)}")

        ok = redis_client.set(SCHEDULER_LEADER_LOCK_KEY, token, nx=True, ex=SCHEDULER_LEADER_LOCK_TTL_SEC)
        if ok:
            _leader_token = token
            logger.info(
                f"已获得调度器 leader 锁，key={SCHEDULER_LEADER_LOCK_KEY}, ttl={SCHEDULER_LEADER_LOCK_TTL_SEC}s, token={token}"
            )
            return True
        # 抢锁失败：仅 debug 输出（避免常驻刷屏）
        try:
            current = redis_client.get(SCHEDULER_LEADER_LOCK_KEY)
            ttl = redis_client.ttl(SCHEDULER_LEADER_LOCK_KEY)
            logger.debug(
                f"未获得调度器 leader 锁，key={SCHEDULER_LEADER_LOCK_KEY}，当前持有者={current}，ttl={ttl}"
            )
            # 若锁存在但没有过期时间（ttl=-1），自动补上 TTL，避免永久死锁
            if ttl == -1:
                redis_client.expire(SCHEDULER_LEADER_LOCK_KEY, SCHEDULER_LEADER_LOCK_TTL_SEC)
                logger.warning(
                    f"检测到 leader 锁无TTL，已补充 expire={SCHEDULER_LEADER_LOCK_TTL_SEC}s，key={SCHEDULER_LEADER_LOCK_KEY}"
                )
        except Exception as e:
            logger.error(f"读取 leader 锁状态失败: {str(e)}")
        return False
    except Exception as e:
        logger.error(f"获取调度器 leader 锁失败: {str(e)}")
        return False


def _renew_leader_lock_loop():
    # 续租频率：TTL 的 1/3，避免抖动导致过期
    interval = max(5, SCHEDULER_LEADER_LOCK_TTL_SEC // 3)
    while True:
        time.sleep(interval)
        if not _leader_token:
            return
        try:
            renewed = redis_client.eval(_RENEW_LUA, 1, SCHEDULER_LEADER_LOCK_KEY, _leader_token, SCHEDULER_LEADER_LOCK_TTL_SEC)
            if int(renewed) != 1:
                # 锁丢失：为避免多实例并发，主动停掉本进程 scheduler
                logger.warning("调度器 leader 锁续租失败或已丢失，将停止本进程调度器以避免多实例运行")
                try:
                    if scheduler.running:
                        scheduler.shutdown()
                finally:
                    # 清空 token，结束线程
                    globals()["_leader_token"] = None
                return
        except Exception as e:
            logger.error(f"调度器 leader 锁续租异常: {str(e)}")


def _start_leader_renew_thread():
    global _leader_renew_thread
    if _leader_renew_thread and _leader_renew_thread.is_alive():
        return
    t = threading.Thread(target=_renew_leader_lock_loop, name="scheduler_leader_renew", daemon=True)
    _leader_renew_thread = t
    t.start()


def _release_leader_lock():
    global _leader_token
    if not _leader_token:
        return
    try:
        redis_client.eval(_RELEASE_LUA, 1, SCHEDULER_LEADER_LOCK_KEY, _leader_token)
    except Exception as e:
        logger.error(f"释放调度器 leader 锁失败: {str(e)}")
    finally:
        _leader_token = None


def _parse_day_of_week(day_of_week_config) -> str:
    """解析星期几配置，支持多种格式：
    - "*" 或 None: 每天
    - "1-7": 1到7（周一到周日）
    - [1,2,3,4,5,6,7]: 列表格式
    - [1,3,5]: 指定星期几
    """
    if not day_of_week_config or day_of_week_config == "*":
        return "*"
    
    if isinstance(day_of_week_config, str):
        # 处理字符串格式
        if day_of_week_config == "1-7":
            return "mon-sun"
        elif day_of_week_config == "1-5":
            return "mon-fri"
        elif day_of_week_config == "6-7":
            return "sat-sun"
        else:
            # 直接返回，可能是cron格式
            return day_of_week_config
    
    elif isinstance(day_of_week_config, list):
        # 处理列表格式
        if not day_of_week_config:
            return "*"
        
        # 将数字转换为cron格式的星期几
        day_map = {
            1: "mon", 2: "tue", 3: "wed", 4: "thu", 
            5: "fri", 6: "sat", 7: "sun"
        }
        
        cron_days = []
        for day in day_of_week_config:
            if isinstance(day, int) and 1 <= day <= 7:
                cron_days.append(day_map[day])
        
        if not cron_days:
            return "*"
        
        # 如果包含所有天，返回通配符
        if len(cron_days) == 7:
            return "*"
        
        return ",".join(cron_days)
    
    return "*"


def _is_job_matching_task(job, task_def) -> bool:
    """判断某个已存在的 job 是否属于指定 task。

    兼容旧版本可能使用过不同的 job.id 命名或未显式设置 id 的情况：
    - 现版本：id 为 f"{task_def.key}_batch" 或 f"{task_def.key}_<user>"
    - 旧版本：可能依赖函数名/模块路径，导致 id 不匹配，但 func_ref/name 可识别
    """
    try:
        if job.id.startswith(f"{task_def.key}_") or job.id == f"{task_def.key}_batch":
            return True
    except Exception:
        pass

    # 兼容：通过函数名或函数引用匹配
    func_name = getattr(task_def.job, "__name__", None)
    job_name = getattr(job, "name", None)
    if func_name and job_name and job_name == func_name:
        return True

    func_module = getattr(task_def.job, "__module__", None)
    func_ref = getattr(job, "func_ref", None)
    if func_module and func_name and func_ref:
        # 典型为 "tasks.morning_job:send_good_morning_batch"
        expected_ref_suffix = f"{func_module}:{func_name}"
        if str(func_ref).endswith(expected_ref_suffix):
            return True

    return False


def _force_cleanup_task_jobs(task_key: str) -> int:
    """兜底清理：直接从 Redis APScheduler 存储删除指定任务相关 job。"""
    removed_count = 0
    try:
        job_ids = redis_client.hkeys("apscheduler.jobs")
        for raw_job_id in job_ids:
            job_id = str(raw_job_id)
            if job_id == f"{task_key}_batch" or job_id.startswith(f"{task_key}_"):
                redis_client.hdel("apscheduler.jobs", raw_job_id)
                redis_client.zrem("apscheduler.run_times", raw_job_id)
                removed_count += 1
        if removed_count:
            logger.info(f"兜底清理 Redis 中 {task_key} 相关任务数量: {removed_count}")
    except Exception as e:
        logger.warning(f"兜底清理 Redis 中 {task_key} 任务失败: {str(e)}")
    return removed_count

def test_scheduler():
    """测试任务：验证调度器是否正常工作"""
    logger.info(f"=== 定时任务调度器测试成功 (进程ID: {PROCESS_ID}) ===")

def register_tasks():
    """自动注册所有定时任务"""
    try:
        enabled_task_keys: List[str] = []
        skipped_task_keys: List[str] = []
        registered_job_ids: List[str] = []

        # 扫描并导入 tasks 包下的所有 .py 模块，使其触发注册装饰器
        import tasks as tasks_pkg
        for m in pkgutil.iter_modules(tasks_pkg.__path__):
            if not m.ispkg:
                importlib.import_module(f"tasks.{m.name}")

        # 从YAML配置获取定时任务配置
        scheduler_config = yaml_config.get_scheduler_config()
        tasks_config = scheduler_config.get("tasks", {})
        
        # 动态读取注册的任务定义
        from core.task_registry import list_tasks
        for task_def in list_tasks():
            # 先按 key 清理旧任务（同时兼容旧版本命名/存量任务）
            try:
                removed_count = 0
                for job in scheduler.get_jobs():
                    if _is_job_matching_task(job, task_def):
                        logger.info(f"清理旧任务: {job}")
                        scheduler.remove_job(job.id)
                        removed_count += 1
                logger.info(f"已清理旧的 {task_def.key} 相关任务，数量: {removed_count}")
            except Exception as e:
                logger.warning(f"清理旧 {task_def.key} 任务时出错: {str(e)}")

            conf = tasks_config.get(task_def.key, {})
            if not conf:
                logger.info(f"任务 {task_def.key} 未在配置中启用，已跳过注册")
                _force_cleanup_task_jobs(task_def.key)
                skipped_task_keys.append(task_def.key)
                continue

            enabled_task_keys.append(task_def.key)
            users: List[str] = conf.get("users", [])
            time_str: str = conf.get("time", task_def.default_time)
            hour, minute = map(int, time_str.split(":"))

            # 解析星期几配置
            day_of_week = conf.get("day_of_week", "*")
            day_of_week_str = _parse_day_of_week(day_of_week)

            if task_def.mode == "batch":
                scheduler.add_job(
                    task_def.job,
                    trigger=CronTrigger(hour=hour, minute=minute, day_of_week=day_of_week_str),
                    args=[users],
                    id=f"{task_def.key}_batch",
                    replace_existing=True
                )
                registered_job_ids.append(f"{task_def.key}_batch")
                logger.info(f"主进程 (ID: {PROCESS_ID}) 已注册 {task_def.key} 批量任务，人数: {len(users)}，接收人: {users}，星期几: {day_of_week_str}")
            else:
                for user_id in users:
                    job_id = f"{task_def.key}_{user_id}"
                    scheduler.add_job(
                        task_def.job,
                        trigger=CronTrigger(hour=hour, minute=minute, day_of_week=day_of_week_str),
                        args=[user_id],
                        id=job_id,
                        replace_existing=True
                    )
                    registered_job_ids.append(job_id)
                logger.info(f"主进程 (ID: {PROCESS_ID}) 已注册 {task_def.key} 逐人任务，接收人: {users}，星期几: {day_of_week_str}")

        # 测试任务（每分钟执行一次，仅调试用）
        if scheduler_config.get("test", False):
            scheduler.add_job(
                test_scheduler,
                trigger=CronTrigger(minute="*"),
                id="test_task",
                replace_existing=True
            )
            logger.info(f"主进程 (ID: {PROCESS_ID}) 已注册测试任务")
            registered_job_ids.append("test_task")

        logger.info(
            f"任务同步完成：启用任务={enabled_task_keys or []}；"
            f"跳过任务={skipped_task_keys or []}；"
            f"已注册jobs={registered_job_ids or []}"
        )

    except Exception as e:
        logger.error(f"任务注册失败: {str(e)}")

def start_scheduler():
    """启动调度器（多进程下通过 Redis 锁选主）"""
    if not _acquire_leader_lock():
        return
    if not scheduler.running:
        register_tasks()
        scheduler.start()
        _start_leader_renew_thread()
        logger.info(f"调度器启动成功 (PID: {PROCESS_ID})")
    else:
        _start_leader_renew_thread()
        logger.warning(f"调度器已在运行中 (PID: {PROCESS_ID})")

def shutdown_scheduler():
    """关闭调度器"""
    if scheduler.running:
        scheduler.shutdown()
        logger.info(f"调度器已关闭 (PID: {PROCESS_ID})")
    _release_leader_lock()
    