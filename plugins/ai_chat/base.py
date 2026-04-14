import json
import os
import datetime
from typing import Optional, Dict, Any, List, Union
from openai import OpenAI

from core.config import CONFIG
from core.plugin import Plugin
from core.redis_client import redis_client


class BaseAIChatPlugin(Plugin):
    """AI对话插件基础类，支持多轮对话和长回复分条发送（对接大模型API，历史存储于Redis）"""

    def __init__(self, name: str, description: str, default_system_prompt: str,
                 max_history: int = 20, max_reply_length: int = 500,
                 model: str = "qwen-plus-2025-07-28", 
                 base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1",
                 api_key_config: str = "DASHSCOPE_API_KEY"):
        super().__init__(name=name, description=description)
        self.name = name
        self.max_history = max_history
        self.max_reply_length = max_reply_length
        self.default_system_prompt = default_system_prompt
        self.model = model
        self.api_key_config = api_key_config
        
        # 使用全局Redis客户端
        self.redis = redis_client

        # 初始化OpenAI兼容客户端
        self.client = OpenAI(
            api_key=CONFIG.get(api_key_config, os.getenv(api_key_config)),
            base_url=base_url
        )

    def _key_namespace(self) -> str:
        """生成Redis键的命名空间，避免键冲突"""
        module_name = self.__class__.__module__.split('.')[-1]
        class_name = self.__class__.__name__
        return f"wxbot:{module_name}:{class_name}"

    def _get_history_key(self, user_id: str) -> str:
        """获取用户对话历史的Redis键名"""
        return f"{self._key_namespace()}:history:{user_id}"

    def _load_history(self, user_id: str) -> List[Dict[str, Any]]:
        """从Redis加载用户对话历史"""
        key = self._get_history_key(user_id)
        data = self.redis.get(key)
        if data:
            try:
                return json.loads(data)
            except Exception:
                return []
        return []

    def _save_history(self, user_id: str, history: List[Dict[str, Any]]):
        """将用户对话历史保存到Redis"""
        key = self._get_history_key(user_id)
        self.redis.set(key, json.dumps(history))

    def _clear_history(self, user_id: str):
        """清除用户对话历史"""
        key = self._get_history_key(user_id)
        self.redis.delete(key)

    def _get_system_prompt_key(self, user_id: str) -> str:
        """获取用户自定义system prompt的Redis键名"""
        return f"{self._key_namespace()}:system_prompt:{user_id}"

    def _get_system_prompt(self, user_id: str) -> str:
        """获取用户的system prompt，优先使用自定义，否则用默认"""
        prompt = self.redis.get(self._get_system_prompt_key(user_id))
        return prompt or self.default_system_prompt

    def _set_system_prompt(self, user_id: str, prompt: str):
        """设置用户自定义的system prompt"""
        self.redis.set(self._get_system_prompt_key(user_id), prompt)

    def _reset_system_prompt(self, user_id: str):
        """重置用户的system prompt为默认值"""
        key = self._get_system_prompt_key(user_id)
        self.redis.delete(key)  # 删除用户自定义的prompt，将使用默认值

    def _add_user_info_to_message(self, message: Union[str, Dict[str, Any]], user_info: Dict[str, Any]) -> Union[str, Dict[str, Any]]:
        """在用户消息前添加用户基本信息"""
        if not user_info:
            return message
        
        # 构建用户信息摘要
        user_summary = []
        if user_info.get('name') and user_info['name'] != '未知用户':
            user_summary.append(f"姓名：{user_info['name']}")
        
        # if user_info.get('department_names'):
        #     dept_str = "、".join(user_info['department_names'])
        #     user_summary.append(f"部门：{dept_str}")
        #
        # if user_info.get('position'):
        #     user_summary.append(f"职位：{user_info['position']}")
        #
        # if user_info.get('mobile'):
        #     user_summary.append(f"手机：{user_info['mobile']}")
        #
        # if user_info.get('email'):
        #     user_summary.append(f"邮箱：{user_info['email']}")
        user_info_text = f"[用户信息：{', '.join(user_summary)} - 此信息仅供你了解用户身份，请勿在回复中提及]"
        # 判断message类型Dict
        if isinstance(message, str):
            return f"{user_info_text}\n{message}"

        # 如果没有有效信息，返回原消息
        if not user_summary:
            return message

        # 对于字典类型的消息，保留原消息不变（在后续流程中会单独附加时间信息等）
        return message

    def _add_time_to_message(self, message: Union[str, Dict[str, Any]]) -> Union[str, Dict[str, Any]]:
        """在用户消息前添加当前详细时间信息"""
        # 获取当前时间
        now = datetime.datetime.now()

        # 格式化详细时间信息
        current_time = now.strftime("%Y年%m月%d日 %H:%M:%S")  # 年月日时分秒
        weekday = now.strftime("%A")  # 星期几（英文）
        weekday_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][now.weekday()]  # 星期几（中文）
        # week_number = now.isocalendar()[1]  # 一年中的第几周
        # day_of_year = now.timetuple().tm_yday  # 一年中的第几天
        # is_leap_year = "是" if (now.year % 4 == 0 and now.year % 100 != 0) or (now.year % 400 == 0) else "否"  # 是否闰年

        # 组合详细时间信息
        time_info = (
            f"[当前时间：{current_time} - {weekday_cn} - "
            # f"本年度第{week_number}周 - 本年度第{day_of_year}天 - "
            # f"{now.year}年{'是' if is_leap_year else '不是'}闰年 - "
            f"此信息仅供你了解对话时间，请勿在回复中提及]"
        )

        if isinstance(message, str):
            return f"{time_info}\n{message}"
        # 如果是字典类型，这里可能需要根据实际需求调整处理方式
        # 例如可以考虑添加到字典的某个字段中，而不是直接返回字符串
        return {**message, "time_info": time_info}  # 更合理的字典处理方式

    def _limit_history_length(self, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """限制对话历史长度，保留最新的对话"""
        if len(history) <= self.max_history:
            return history

        # 始终保留第一个system提示词
        try:
            has_leading_system = bool(history) and isinstance(history[0], dict) and history[0].get("role") == "system"
        except Exception:
            has_leading_system = False

        if has_leading_system:
            # 可用于非system消息的最大条数
            max_non_system_messages = max(self.max_history - 1, 0)
            # 仅从除system外的历史中截取最新的部分
            remaining_messages = history[1:]
            trimmed_remaining = remaining_messages[-max_non_system_messages:] if max_non_system_messages > 0 else []
            return [history[0]] + trimmed_remaining

        # 如果没有明确的system在首位，退化为简单尾部截断
        return history[-self.max_history:]

    def supports_message_type(self, message_type: str) -> bool:
        """
        检查插件是否支持指定类型的消息
        
        Args:
            message_type: 消息类型，如 'text', 'image', 'voice', 'video', 'file', 'location'
             ['text', 'image', 'voice', 'video', 'file', 'location']
            
        Returns:
            是否支持该消息类型
        """
        return message_type in ['text', 'image']

 
    def _build_media_url(self, file_path: str) -> str:
        """构建媒体文件的HTTP访问URL"""
        if not file_path:
            return ""
        
        # 获取配置中的媒体URL基础域名
        from core.config import CONFIG
        base_domain = CONFIG.get("MEDIA_URL_BASE", "http://127.0.0.1:4455")
        
        # 将Windows路径分隔符转换为URL路径分隔符
        url_path = file_path.replace('\\', '/')
        
        # 如果路径以temp_media开头，构建完整的HTTP URL
        if url_path.startswith('temp_media/'):
            return f"{base_domain}/{url_path}"
        elif url_path.startswith('./temp_media/'):
            return f"{base_domain}/{url_path[2:]}"
        else:
            # 如果不是temp_media目录，返回原始路径
            return file_path

    def _process_multimedia_message(self, message: Dict[str, Any], message_type: str) -> Union[str, Dict[str, Any]]:
        """处理多媒体消息，转换为AI可理解的格式"""
        if message_type == 'image':
            pic_url = message.get('PicUrl', '')
            media_id = message.get('MediaId', '')
            
            # 返回OpenAI多模态API支持的图片消息格式
            if pic_url:
                return {
                    'type': 'image_url',
                    'image_url': {
                        'url': pic_url
                    }
                }
            else:
                # 如果没有URL，返回文本描述
                return f"[图片消息] 用户发送了一张图片，媒体ID: {media_id}"
                
        elif message_type == 'voice':
            media_id = message.get('MediaId', '')
            format_type = message.get('Format', 'amr')  # 企业微信语音通常是amr格式
            voice_path = message.get('VoicePath', '')
            
            # 如果有本地文件路径，构建音频输入格式
            if voice_path and os.path.exists(voice_path):
                # 将本地文件路径转换为可访问的URL
                voice_url = self._build_media_url(voice_path)
                return {
                    'type': 'input_audio',
                    'input_audio': {
                        'data': voice_url,  # 使用HTTP URL而不是本地文件路径
                        'format': format_type if format_type else 'amr',
                    }
                }
            else:
                # 如果没有本地文件，返回文本描述
                return f"[语音消息] 用户发送了一条语音消息，格式: {format_type}, 媒体ID: {media_id}"
            
        elif message_type == 'video':
            media_id = message.get('MediaId', '')
            thumb_media_id = message.get('ThumbMediaId', '')
            media_url = message.get('MediaUrl', '')
            video_size = message.get('VideoSize', 0)
            
            # 如果有媒体URL，直接使用URL
            if media_url:
                return {
                    'type': 'video_url',
                    'video_url': {
                        'url': media_url  # 直接使用企业微信媒体URL
                    }
                }
            else:
                # 如果没有媒体URL，返回文本描述
                return f"[视频消息] 用户发送了一条视频消息，媒体ID: {media_id}, 缩略图ID: {thumb_media_id}, 大小: {video_size} 字节"
            
        elif message_type == 'file':
            media_id = message.get('MediaId', '')
            file_path = message.get('FilePath', '')
            file_size = message.get('FileSize', 0)
            # 如果没有本地文件，返回文本描述
            return f"[文件消息] 用户发送了一个文件，媒体ID: {media_id}, 大小: {file_size} 字节"
            
        elif message_type == 'location':
            location_x = message.get('Location_X', '')
            location_y = message.get('Location_Y', '')
            scale = message.get('Scale', '')
            label = message.get('Label', '')
            # 位置消息转换为文本描述
            return f"[位置消息] 用户分享了位置信息，坐标: ({location_x}, {location_y}), 缩放: {scale}, 标签: {label}"
        else:
            return str(message)

    async def handle(self, message: Union[str, Dict[str, Any]], from_user: str, context: Dict[str, Any]) -> Optional[str]:
        """处理用户消息，返回AI回复"""
        try:
            # 获取消息类型
            message_type = context.get('message_type', 'text')
            
            # 处理不同类型的消息
            if isinstance(message, dict):
                # 多媒体消息
                processed_message = self._process_multimedia_message(message, message_type)
            else:
                # 文本消息
                processed_message = message
                # 处理新对话指令
                if processed_message.strip() == "/开启新对话":
                    self._clear_history(from_user)
                    return "已开启新对话，历史上下文已清空。"

                # 处理设定角色指令
                if processed_message.startswith("/设定角色："):
                    prompt = processed_message.replace("/设定角色：", "", 1).strip()
                    self._set_system_prompt(from_user, prompt)
                    self._clear_history(from_user)
                    return f"已为你设定AI角色：{prompt}\n历史上下文已清空，可以开始新对话啦~"

                # 处理重置角色指令
                if processed_message.strip() == "/重置角色":
                    self._clear_history(from_user)
                    self._reset_system_prompt(from_user)
                    return f"你好，我是{self.name}，历史上下文已清空，可以开始新对话啦~"
            # 获取用户信息和配置
            user_info = context.get('user_info', {})
            agent_id = context.get('agent_id', '')
            
            # 检查是否配置了包含时间信息
            include_time_info = True  # 默认包含时间信息
            if agent_id:
                from core.config import AGENT_CONFIGS
                agent_conf = AGENT_CONFIGS.get(agent_id)
                if agent_conf:
                    include_time_info = agent_conf.get('include_time_info', True)
            
            # 在用户消息前添加用户信息和当前时间信息
            message_with_user_info = self._add_user_info_to_message(processed_message, user_info)
            if include_time_info:
                message_with_time = self._add_time_to_message(message_with_user_info)
            else:
                message_with_time = message_with_user_info
            
            # 加载对话历史
            history = self._load_history(from_user)
            
            # 获取system prompt
            system_prompt = self._get_system_prompt(from_user)
            
            # 检查历史记录是否已经有system prompt，如果没有则添加
            if not history or history[0]["role"] != "system":
                history = [{"role": "system", "content": system_prompt}] + history
            else:
                # 如果已有system prompt，更新其内容（以防用户修改了prompt）
                history[0]["content"] = system_prompt
            
            # 构建用户消息，支持多模态内容
            if isinstance(processed_message, dict) and processed_message.get('type') in ['image_url', 'input_audio', 'video_url']:
                # 多模态消息：构建包含用户信息、时间信息和媒体的content数组
                content = []
                
                # 添加用户信息（如果有的话）
                if isinstance(message_with_user_info, str):
                    content.append({"type": "text", "text": message_with_user_info})
                elif isinstance(message_with_user_info, dict):
                    content.append(message_with_user_info)
                
                # 添加时间信息（如果配置了包含时间信息）
                if include_time_info:
                    if isinstance(message_with_time, str):
                        content.append({"type": "text", "text": message_with_time})
                    elif isinstance(message_with_time, dict):
                        content.append(message_with_time)
                
                # 添加媒体内容
                content.append(processed_message)
                
                history.append({"role": "user", "content": content})
            else:
                # 文本消息或其他类型，直接添加
                history.append({"role": "user", "content": message_with_time})
            
            # 限制历史长度
            history = self._limit_history_length(history)
            
            # 调用AI API
            response = await self._call_ai_api(history)
            
            if response:
                # 保存对话历史
                history.append({"role": "assistant", "content": response})
                self._save_history(from_user, history)
                
                # 分条发送长回复，现在返回的是列表
                split_replies = self._split_long_reply(response)
                
                # 如果只有一条回复，直接返回；如果有多条，返回列表
                if len(split_replies) == 1:
                    return split_replies[0]
                else:
                    return split_replies
            else:
                print("AI API调用失败，没有返回响应")
            
        except Exception as e:
            print(f"AI对话插件处理消息时出错: {str(e)}")
            import traceback
            traceback.print_exc()
            return f"抱歉，我遇到了一些问题：{str(e)}"
        
        return None

    async def _call_ai_api(self, history: List[Dict[str, Any]]) -> Optional[str]:
        """调用AI API获取回复"""
        try:
            # 检查API Key
            api_key = CONFIG.get(self.api_key_config, os.getenv(self.api_key_config))
            if not api_key:
                error_msg = f"未配置API Key: {self.api_key_config}"
                print(f"错误: {error_msg}")
                raise Exception(error_msg)
            
            # 调用OpenAI兼容API
            response = self.client.chat.completions.create(
                model=self.model,
                messages=history,
                # max_tokens=self.ai_model_config.get('max_tokens', 1000),
                # temperature=self.ai_model_config.get('temperature', 0.7)
            )
            
            if response.choices and len(response.choices) > 0:
                content = response.choices[0].message.content
                return content
            else:
                print("AI API返回的choices为空")
                return None
            
        except Exception as e:
            print(f"调用AI API失败: {str(e)}")
            import traceback
            traceback.print_exc()
            raise e

    def _split_long_reply(self, reply: str) -> List[str]:
        """将长回复分割成多条消息，返回消息列表"""
        if len(reply) <= self.max_reply_length:
            return [reply]
        
        # 按句号分割，保持语义完整性
        sentences = reply.split('。')
        result = []
        current_chunk = ""
        
        for i, sentence in enumerate(sentences):
            if not sentence.strip():
                continue
                
            # 加上句号
            sentence = sentence.strip() + "。"
            
            # 如果当前块加上新句子超过限制，先保存当前块
            if len(current_chunk + sentence) > self.max_reply_length and current_chunk:
                result.append(current_chunk.strip())
                current_chunk = sentence
            else:
                current_chunk += sentence
        
        # 添加最后一块
        if current_chunk.strip():
            result.append(current_chunk.strip())
        
        # 如果分割后仍然有超长的块，强制按字符分割
        final_result = []
        for i, chunk in enumerate(result):
            if len(chunk) <= self.max_reply_length:
                final_result.append(chunk)
            else:
                # 强制按字符分割
                for j in range(0, len(chunk), self.max_reply_length):
                    sub_chunk = chunk[j:j + self.max_reply_length]
                    final_result.append(sub_chunk)
        
        return final_result

    def get_commands(self) -> List[str]:
        """获取插件支持的命令列表"""
        return [
            "/开启新对话 - 清除对话历史",
            "/重置角色 - 重置为默认system prompt",
            "/设定角色 <内容> - 设置自定义system prompt"
        ]