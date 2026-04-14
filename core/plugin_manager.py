from typing import List, Dict, Any, Optional, Union
import os
import importlib
import inspect
from .plugin import Plugin
from .config import AGENT_CONFIGS, CONFIG

class PluginManager:
    """插件管理器，负责加载和管理所有插件"""

    def __init__(self, plugins_dir: str = None):
        self.plugins_dir = plugins_dir or CONFIG["PLUGINS_DIR"]
        self._all_plugins: Dict[str, Plugin] = {}
        self.plugins: List[Plugin] = []
        self._load_all_plugins()

    def _load_all_plugins(self) -> None:
        """加载插件目录下所有插件（不区分agent）"""
        print(f"开始加载插件，插件目录: {self.plugins_dir}")
        if not os.path.exists(self.plugins_dir):
            os.makedirs(self.plugins_dir)
        
        try:
            files = os.listdir(self.plugins_dir)
        except Exception as e:
            print(f"无法读取插件目录: {str(e)}")
            return
            
        for filename in files:
            if filename.endswith(".py") and not filename.startswith("_"):
                module_name = filename[:-3]
                try:
                    # 统一使用包导入，确保模块路径为 plugins.<module_name>
                    module = importlib.import_module(f"plugins.{module_name}")
                    for name, cls in inspect.getmembers(module, inspect.isclass):
                        if issubclass(cls, Plugin) and cls != Plugin and cls.__name__ != 'BaseAIChatPlugin':
                            try:
                                plugin_instance = cls()
                                self._all_plugins[module_name] = plugin_instance
                            except Exception as init_e:
                                print(f"初始化插件类 {name} 失败: {str(init_e)}")
                                import traceback
                                traceback.print_exc()
                except Exception as e:
                    print(f"加载插件 {filename} 失败: {str(e)}")
                    import traceback
                    traceback.print_exc()
        
        print(f"插件加载完成，总共加载了 {len(self._all_plugins)} 个插件")

    def load_plugins_for_agent(self, agent_id: str):
        """根据 agent_id 加载允许的插件"""
        self.plugins = []
        agent_conf = AGENT_CONFIGS.get(agent_id)
        if not agent_conf:
            print(f"未找到 agent_id={agent_id} 的配置")
            return
        allowed = agent_conf.get("plugins", [])
        print(f"应用 {agent_id} 允许的插件: {allowed}")
        for plugin_key in allowed:
            plugin = self._all_plugins.get(plugin_key)
            if plugin:
                self.plugins.append(plugin)
            else:
                print(f"插件 {plugin_key} 未找到，跳过")
        print(f"应用 {agent_id} 最终加载的插件数量: {len(self.plugins)}")

    async def process_message(self, message: Union[str, Dict[str, Any]], from_user: str, context: Dict[str, Any]) -> List[str]:
        try:
            print(f"process_message")
            replies: List[str] = []
            message_type = context.get("message_type", "text")
            print(f"插件管理器开始处理消息，插件数量: {len(self.plugins)}")
            print(f"消息内容: {message}, 用户: {from_user}, 消息类型: {message_type}")
            
            for i, plugin in enumerate(self.plugins):
                print(f"处理插件 {i+1}/{len(self.plugins)}: {plugin.name}")
                # 检查插件是否支持该消息类型
                if hasattr(plugin, 'supports_message_type') and not plugin.supports_message_type(message_type):
                    print(f"插件 {plugin.name} 不支持消息类型 {message_type}，跳过")
                    continue
                print(f"正在处理消息，调用插件 {plugin.name} 的 handle 方法")
                try:
                    response = await plugin.handle(message, from_user, context)
                    if response is not None:
                        print(f"插件 {plugin.name} 返回响应: {response}")
                        # 如果插件返回的是列表，将每个元素都添加到replies中
                        if isinstance(response, list):
                            replies.extend(response)
                        else:
                            replies.append(response)
                    else:
                        print(f"插件 {plugin.name} 没有返回响应")
                except Exception as e:
                    print(f"插件 {plugin.name} 处理消息时出错: {str(e)}")
                    import traceback
                    print(f"插件 {plugin.name} 异常堆栈: {traceback.format_exc()}")
            print(f"插件管理器处理完成，总回复数: {len(replies)}")
            return replies
        except Exception as e:
            print(f"插件管理器处理消息时出错: {str(e)}")
            import traceback
            print(f"插件管理器异常堆栈: {traceback.format_exc()}")
            return []
