from typing import Callable, Dict, List, Optional, Literal
from dataclasses import dataclass


TaskMode = Literal["batch", "per_user"]


@dataclass
class TaskDef:
    key: str
    mode: TaskMode
    default_time: str
    job: Callable[..., None]


_registry: Dict[str, TaskDef] = {}


def scheduled_task(key: str, mode: TaskMode = "batch", default_time: str = "00:00"):
    """装饰器：注册一个可自动加载的定时任务。

    - key: 在 config.yaml 的 scheduler.tasks.<key> 下读取配置
    - mode: "batch" 表示按用户列表拼接一次执行；"per_user" 表示为每个用户单独建一个任务
    - default_time: 未配置时的默认时间（HH:MM）
    """

    def _decorator(func: Callable[..., None]) -> Callable[..., None]:
        if key in _registry:
            raise ValueError(f"重复注册任务 key={key}")
        _registry[key] = TaskDef(key=key, mode=mode, default_time=default_time, job=func)
        return func

    return _decorator


def list_tasks() -> List[TaskDef]:
    return list(_registry.values())


def get_task(key: str) -> Optional[TaskDef]:
    return _registry.get(key)



