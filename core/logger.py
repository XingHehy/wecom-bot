import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler

# 全局日志只配置一次，避免每个 logger 各自创建文件/handler
_GLOBAL_CONFIGURED = False
_GLOBAL_LOG_FILE: str | None = None


class DailyPathRotatingFileHandler(RotatingFileHandler):
    """
    同时支持：
    - 按天切换输出路径：logs/YYYY-MM-DD/<filename>
    - 按大小滚动：继承 RotatingFileHandler 的 maxBytes/backupCount
    """

    def __init__(
        self,
        base_log_dir: str,
        filename: str,
        maxBytes: int,
        backupCount: int,
        encoding: str = "utf-8",
    ):
        self._base_log_dir = base_log_dir
        self._filename = filename
        self._current_day = datetime.now().strftime("%Y-%m-%d")
        full_path = self._build_path_for_day(self._current_day)
        super().__init__(
            full_path,
            maxBytes=maxBytes,
            backupCount=backupCount,
            encoding=encoding,
        )

    def _build_path_for_day(self, day: str) -> str:
        daily_dir = os.path.join(self._base_log_dir, day)
        os.makedirs(daily_dir, exist_ok=True)
        return os.path.join(daily_dir, self._filename)

    def _maybe_switch_day(self):
        day = datetime.now().strftime("%Y-%m-%d")
        if day == self._current_day:
            return
        self._current_day = day
        new_path = self._build_path_for_day(day)
        # 切换 baseFilename 并重开文件流
        self.acquire()
        try:
            if self.stream:
                self.stream.close()
                self.stream = None
            self.baseFilename = os.path.abspath(new_path)
            self.stream = self._open()
        finally:
            self.release()

    def emit(self, record: logging.LogRecord) -> None:
        self._maybe_switch_day()
        super().emit(record)

class Logger:
    """日志系统工具类"""
    
    def __init__(self, name: str = "wxbot", log_dir: str = None):
        # 如果没有指定日志目录，尝试多个位置
        if log_dir is None:
            import os
            import tempfile
            
            # 尝试顺序：当前目录logs -> 用户主目录 -> 临时目录
            possible_dirs = [
                "logs",  # 当前目录下的logs
                os.path.join(os.path.expanduser("~"), "wxbot_logs"),  # 用户主目录
                os.path.join(tempfile.gettempdir(), "wxbot_logs")  # 系统临时目录
            ]
            
            for dir_path in possible_dirs:
                try:
                    if not os.path.exists(dir_path):
                        os.makedirs(dir_path, exist_ok=True)
                    # 测试写入权限
                    test_file = os.path.join(dir_path, "test_write.tmp")
                    with open(test_file, 'w') as f:
                        f.write("test")
                    os.remove(test_file)
                    log_dir = dir_path
                    break
                except (PermissionError, OSError):
                    continue
            
            # 如果所有位置都失败，使用当前目录（可能会失败，但至少不会阻止程序启动）
            if log_dir is None:
                log_dir = "logs"
        self.name = name
        self.log_dir = log_dir
        self.logger = None
        self._setup_logger()
    
    def _setup_logger(self):
        """设置日志器（全局统一到一个文件）。"""
        global _GLOBAL_CONFIGURED, _GLOBAL_LOG_FILE

        # 创建日志器（命名 logger 仅用于区分 %(name)s）
        self.logger = logging.getLogger(self.name)

        # 只做一次全局配置：root handler -> wxbot.log
        if not _GLOBAL_CONFIGURED:
            # 从YAML配置获取日志级别与开关
            try:
                from .yaml_config import get_config
                yaml_config = get_config()
                log_level_str = yaml_config.get("logging.level", "INFO")
                log_level = getattr(logging, log_level_str.upper(), logging.INFO)
                console_enabled = yaml_config.get("logging.console", True)
                file_enabled = yaml_config.get("logging.file", True)
                max_size = yaml_config.get("logging.max_size", "10MB")
                backup_count = yaml_config.get("logging.backup_count", 5)
                log_filename = yaml_config.get("logging.filename", "wxbot.log")
            except Exception:
                log_level = logging.INFO
                console_enabled = True
                file_enabled = True
                max_size = "10MB"
                backup_count = 5
                log_filename = "wxbot.log"

            # 转换max_size为字节数
            if isinstance(max_size, str):
                s = max_size.strip().upper()
                try:
                    if s.endswith("MB"):
                        max_size = int(s[:-2]) * 1024 * 1024
                    elif s.endswith("KB"):
                        max_size = int(s[:-2]) * 1024
                    else:
                        max_size = int(s)
                except Exception:
                    max_size = 10 * 1024 * 1024

            # 创建格式化器
            formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )

            root = logging.getLogger()
            root.setLevel(log_level)

            if console_enabled:
                ch = logging.StreamHandler()
                ch.setLevel(log_level)
                ch.setFormatter(formatter)
                root.addHandler(ch)

            if file_enabled:
                try:
                    if self.log_dir is not None and not os.path.exists(self.log_dir):
                        os.makedirs(self.log_dir, exist_ok=True)
                    log_dir = self.log_dir if self.log_dir is not None else "."
                    fh = DailyPathRotatingFileHandler(
                        base_log_dir=log_dir,
                        filename=log_filename,
                        maxBytes=int(max_size),
                        backupCount=int(backup_count),
                        encoding="utf-8",
                    )
                    fh.setLevel(log_level)
                    fh.setFormatter(formatter)
                    root.addHandler(fh)
                    _GLOBAL_LOG_FILE = fh.baseFilename
                except Exception as e:
                    print(f"警告：无法创建统一文件日志处理器，错误：{e!r}")
                    print("将只使用控制台输出日志")

            _GLOBAL_CONFIGURED = True

        # 命名 logger 不再单独挂 handler，全部向 root 汇聚
        self.logger.propagate = True
        self.logger.setLevel(logging.NOTSET)
    
    def info(self, message: str):
        """信息日志"""
        self.logger.info(message)
    
    def warning(self, message: str):
        """警告日志"""
        self.logger.warning(message)
    
    def error(self, message: str):
        """错误日志"""
        self.logger.error(message)
    
    def debug(self, message: str):
        """调试日志"""
        self.logger.debug(message)
    
    def critical(self, message: str):
        """严重错误日志"""
        self.logger.critical(message)

# 创建默认日志器实例
default_logger = Logger()

def get_logger(name: str = None) -> Logger:
    """获取日志器实例"""
    if name:
        return Logger(name)
    return default_logger
