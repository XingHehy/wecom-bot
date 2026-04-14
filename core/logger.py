import logging
import os
from datetime import datetime
from logging.handlers import RotatingFileHandler

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
        """设置日志器"""
        try:
            # 按日期创建日志目录
            today = datetime.now().strftime('%Y-%m-%d')
            daily_log_dir = os.path.join(self.log_dir, today)
            if not os.path.exists(daily_log_dir):
                os.makedirs(daily_log_dir)
        except Exception as e:
            # 如果创建目录失败，只使用控制台输出
            print(f"警告：无法创建日志目录 {self.log_dir}，错误：{str(e)}")
            print("将只使用控制台输出日志")
            self.log_dir = None
        
        # 创建日志器
        self.logger = logging.getLogger(self.name)
        
        # 从YAML配置获取日志级别
        try:
            from .yaml_config import get_config
            yaml_config = get_config()
            log_level_str = yaml_config.get("logging.level", "INFO")
            log_level = getattr(logging, log_level_str.upper(), logging.INFO)
        except Exception:
            log_level = logging.INFO
        
        self.logger.setLevel(log_level)
        
        # 避免重复添加处理器
        if self.logger.handlers:
            return
        
        # 创建格式化器
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        # 控制台处理器
        try:
            from .yaml_config import get_config
            yaml_config = get_config()
            console_enabled = yaml_config.get("logging.console", True)
        except Exception:
            console_enabled = True
            
        if console_enabled:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(log_level)
            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)
        
        # 文件处理器 - 按模块名分割（只有在目录可用时才添加）
        if self.log_dir is not None:
            try:
                from .yaml_config import get_config
                yaml_config = get_config()
                file_enabled = yaml_config.get("logging.file", True)
                max_size = yaml_config.get("logging.max_size", "10MB")
                backup_count = yaml_config.get("logging.backup_count", 5)
                
                # 转换max_size为字节数
                if isinstance(max_size, str):
                    if max_size.endswith("MB"):
                        max_size = int(max_size[:-2]) * 1024 * 1024
                    elif max_size.endswith("KB"):
                        max_size = int(max_size[:-2]) * 1024
                    else:
                        max_size = int(max_size)
                
                if file_enabled:
                    file_handler = RotatingFileHandler(
                        os.path.join(daily_log_dir, f'{self.name}.log'),
                        maxBytes=max_size,
                        backupCount=backup_count,
                        encoding='utf-8'
                    )
                    file_handler.setLevel(log_level)
                    file_handler.setFormatter(formatter)
                    self.logger.addHandler(file_handler)
            except Exception as e:
                print(f"警告：无法创建文件日志处理器，错误：{str(e)}")
                print("将只使用控制台输出日志")
    
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
