from __future__ import annotations

import asyncio
import datetime
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.config.settings import AGENT_CONFIGS, get_model_config, yaml_config
from app.common.base_agent import AgentProfile
from app.common.loader import AgentPluginRegistry, load_agent_plugins
from app.logger import get_logger
from app.memory.checkpointer import checkpointer_provider, thread_id_for

logger = get_logger("agent_runtime")


@dataclass
class RuntimeResult:
    replies: List[str]
    selected_agent: str


class AgentRuntime:
    def __init__(self, agent_id: str):
        self.agent_id = str(agent_id)
        self.agent_config = AGENT_CONFIGS.get(self.agent_id, {})
        self.registry: AgentPluginRegistry = load_agent_plugins()
        configured_plugins = self.agent_config.get("plugins") or []
        self.enabled_agents = self.registry.normalize_keys(configured_plugins)
        if not self.enabled_agents:
            self.enabled_agents = ["demo_agent"]
        self._compiled: Dict[str, Any] = {}

    def status(self) -> Dict[str, Any]:
        profiles = self.registry.all_profiles(include_disabled=True)
        return {
            "agent_id": self.agent_id,
            "enabled_agents": [
                {
                    "key": key,
                    "name": profiles[key].name if key in profiles else key,
                    "description": profiles[key].description if key in profiles else "unknown",
                    "sub_agents": profiles[key].sub_agents if key in profiles else [],
                }
                for key in self.enabled_agents
            ],
            "memory_backend": checkpointer_provider.backend,
            "memory_error": checkpointer_provider.error,
        }

    async def ainvoke(self, message: Any, from_user: str, context: Optional[Dict[str, Any]] = None) -> RuntimeResult:
        context = context or {}
        if isinstance(message, str):
            command = message.strip()
            if command == "/开启新对话":
                return RuntimeResult(["已开启新对话。LangGraph checkpoint 会在下一轮以新的消息继续推进。"], "system")

        selected = self._route(message)
        agent = await self._get_agent(selected, from_user)
        content = self._message_content(message, context)
        user_info = context.get("user_info") or {}
        include_time = self.agent_config.get("include_time_info", True)
        prefix_lines = []
        if include_time:
            now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            prefix_lines.append(f"[当前时间：{now}，时区 Asia/Shanghai。不要在回复中主动复述本条系统上下文。]")
        if user_info.get("name"):
            prefix_lines.append(f"[用户信息：姓名 {user_info.get('name')}。不要在回复中主动复述本条系统上下文。]")

        if isinstance(content, str) and prefix_lines:
            content = "\n".join(prefix_lines + [content])
        elif isinstance(content, list) and prefix_lines:
            content = [{"type": "text", "text": "\n".join(prefix_lines)}] + content

        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": content}]},
            config={"configurable": {"thread_id": thread_id_for(self.agent_id, from_user)}},
        )
        reply = self._extract_reply(result)
        return RuntimeResult(self._split_reply(reply), selected)

    def _normalize_enabled(self, values: List[str]) -> List[str]:
        return self.registry.normalize_keys(values)

    def _route(self, message: Any) -> str:
        profiles = self.registry.all_profiles()
        for key in self.enabled_agents:
            profile = profiles.get(key)
            if profile and profile.category == "chat" and profile.sub_agents:
                return key

        text = message if isinstance(message, str) else " ".join(str(v) for v in message.values()) if isinstance(message, dict) else str(message)
        scored: List[tuple[int, str]] = []
        for key in self.enabled_agents:
            try:
                score = self.registry.get(key).route_score(text)
            except Exception:
                score = 0
            if score > 0:
                scored.append((score, key))
        if scored:
            scored.sort(key=lambda item: item[0], reverse=True)
            return scored[0][1]

        for key in self.enabled_agents:
            profile = profiles.get(key)
            if profile and profile.category == "chat":
                return key
        return self.enabled_agents[0]

    async def _get_agent(self, key: str, user_id: str):
        try:
            from langchain.agents import create_agent
        except Exception as exc:
            raise RuntimeError("LangChain 1.x 依赖未安装，请先安装 requirements.txt") from exc

        cache_key = f"{key}:{user_id}"
        if cache_key in self._compiled:
            return self._compiled[cache_key]
        plugin = self.registry.get(key)
        profile = plugin.profile
        checkpointer = await checkpointer_provider.get()
        tools = self._tools_for(key, user_id)
        model = self._model_for(profile)
        system_prompt = self._system_prompt_for(profile)
        create_kwargs = {
            "model": model,
            "tools": tools,
            "system_prompt": system_prompt,
            "checkpointer": checkpointer,
        }
        middleware = self._middleware_for(profile)
        if middleware:
            create_kwargs["middleware"] = middleware
        self._compiled[cache_key] = create_agent(**create_kwargs)
        return self._compiled[cache_key]

    def _model_for(self, profile: AgentProfile):
        try:
            from langchain_openai import ChatOpenAI
        except Exception as exc:
            raise RuntimeError("langchain-openai 依赖未安装，请先安装 requirements.txt") from exc

        cfg = get_model_config(profile.model_profile)
        return ChatOpenAI(
            model=profile.model or cfg["model"],
            api_key=cfg.get("api_key"),
            base_url=cfg.get("base_url"),
            temperature=profile.temperature,
            max_tokens=profile.max_tokens,
        )

    def _tools_for(self, key: str, user_id: str) -> List[Any]:
        plugin = self.registry.get(key)
        profile = plugin.profile
        tools: List[Any] = plugin.build_custom_tools(agent_id=self.agent_id, user_id=user_id)
        tools.extend(self._sub_agent_tools_for(profile, user_id))

        if profile.global_tools:
            from app.tools.basic import build_basic_tools

            global_tools = build_basic_tools(lambda: self.registry.describe(self.enabled_agents))
            tools.extend(tool for tool in global_tools if tool.name in profile.global_tools)
        return tools

    def _sub_agent_tools_for(self, profile: AgentProfile, user_id: str) -> List[Any]:
        if not profile.sub_agents:
            return []
        try:
            from langchain_core.tools import StructuredTool
        except Exception as exc:
            logger.warning(f"无法创建 sub-agent tools，langchain_core 不可用: {exc}")
            return []

        tools: List[Any] = []
        for sub_key in self._allowed_sub_agents(profile):
            sub_profile = self.registry.get(sub_key).profile
            tool_name = f"call_agent_{sub_key}"

            def _make_call_sub_agent(target_key: str):
                async def _call_sub_agent(message: str) -> str:
                    return await self._invoke_sub_agent(target_key, message, user_id)

                return _call_sub_agent

            tools.append(
                StructuredTool.from_function(
                    coroutine=_make_call_sub_agent(sub_key),
                    name=tool_name,
                    description=(
                        f"把任务委托给 sub-agent「{sub_profile.display_name}」。"
                        f"适用场景：{sub_profile.description}。"
                        "输入应是需要该 sub-agent 处理的完整中文任务描述。"
                    ),
                )
            )
        return tools

    def _allowed_sub_agents(self, profile: AgentProfile) -> List[str]:
        allowed: List[str] = []
        enabled = set(self.enabled_agents)
        profiles = self.registry.all_profiles()
        for raw in profile.sub_agents:
            sub_key = str(raw)
            if sub_key == profile.key:
                logger.warning(f"忽略自身 sub_agent 引用: {profile.key}")
                continue
            if sub_key not in profiles:
                logger.warning(f"忽略不存在或未启用的 sub_agent: {sub_key}")
                continue
            if sub_key not in enabled:
                logger.warning(f"sub_agent {sub_key} 未在当前应用 plugins 中启用，已忽略")
                continue
            if sub_key not in allowed:
                allowed.append(sub_key)
        return allowed

    async def _invoke_sub_agent(self, key: str, message: str, user_id: str) -> str:
        profile = self.registry.get(key).profile
        notify_cfg = yaml_config.get("agent.sub_agent_notify", {}) or {}
        notify_enabled = bool(notify_cfg.get("enabled", True))
        delay_seconds = float(notify_cfg.get("delay_seconds", 1.2))
        template = str(notify_cfg.get("template") or "我让{agent_name}处理一下，稍等。")

        task = asyncio.create_task(self._invoke_agent_once(key, message, user_id, thread_suffix=f"sub:{key}"))
        if not notify_enabled:
            return await task

        try:
            return await asyncio.wait_for(asyncio.shield(task), timeout=delay_seconds)
        except asyncio.TimeoutError:
            try:
                from app.wecom.enterprise_wechat import enqueue_active_message

                await enqueue_active_message(
                    agent_id=self.agent_id,
                    msg=template.format(agent_name=profile.display_name, agent_key=key),
                    user=user_id,
                )
            except Exception as exc:
                logger.warning(f"发送 sub-agent 处理中提示失败: {exc}")
            return await task

    async def _invoke_agent_once(self, key: str, message: str, user_id: str, *, thread_suffix: str = "") -> str:
        agent = await self._get_agent(key, user_id)
        thread_id = thread_id_for(self.agent_id, user_id)
        if thread_suffix:
            thread_id = f"{thread_id}:{thread_suffix}"
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": message}]},
            config={"configurable": {"thread_id": thread_id}},
        )
        return self._extract_reply(result)

    def _middleware_for(self, profile: AgentProfile) -> List[Any]:
        cfg = yaml_config.get("agent.middleware.summarization", {}) or {}
        if not cfg.get("enabled", False):
            return []
        try:
            from langchain.agents.middleware import SummarizationMiddleware
        except Exception as exc:
            logger.warning(f"SummarizationMiddleware 不可用，跳过: {exc}")
            return []

        model_profile = str(cfg.get("model_profile") or profile.model_profile)
        model = self._model_for(
            AgentProfile(
                key=f"{profile.key}_summary_model",
                name="summary_model",
                display_name="summary_model",
                description="",
                system_prompt="",
                model_profile=model_profile,
                model=cfg.get("model") or profile.model,
                temperature=0,
                max_tokens=int(cfg.get("max_tokens", 2048)),
            )
        )
        candidate_kwargs = [
            {
                "model": model,
                "max_tokens_before_summary": int(cfg.get("max_tokens_before_summary", 800000)),
                "messages_to_keep": int(cfg.get("messages_to_keep", 20)),
            },
            {
                "model": model,
                "trigger": {"tokens": int(cfg.get("max_tokens_before_summary", 800000))},
                "keep": {"messages": int(cfg.get("messages_to_keep", 20))},
            },
        ]
        for kwargs in candidate_kwargs:
            try:
                return [SummarizationMiddleware(**kwargs)]
            except TypeError:
                continue
            except Exception as exc:
                logger.warning(f"SummarizationMiddleware 初始化失败，跳过: {exc}")
                return []
        logger.warning("SummarizationMiddleware 参数签名不匹配，已跳过")
        return []

    def _system_prompt_for(self, profile: AgentProfile) -> str:
        sub_agent_text = ""
        if profile.sub_agents:
            allowed_sub_agents = self._allowed_sub_agents(profile)
            lines = ["\n\n可委托的 sub-agents："]
            for sub_key in allowed_sub_agents:
                sub_profile = self.registry.get(sub_key).profile
                lines.append(f"- {sub_profile.display_name}（{sub_key}）：{sub_profile.description}")
            if allowed_sub_agents:
                lines.append("需要专业能力或复合任务时，优先调用合适的 call_agent_* 工具委托给 sub-agent。")
                sub_agent_text = "\n".join(lines)
        return (
            f"{profile.system_prompt}{sub_agent_text}\n\n"
            "你运行在企业微信机器人 wxbot 中。需要调用工具完成的事情必须调用工具；普通回复直接给最终文本。"
            "回复要适合企业微信消息，简洁、明确，不输出 markdown 代码块。"
        )

    def _message_content(self, message: Any, context: Dict[str, Any]) -> Any:
        message_type = context.get("message_type", "text")
        if isinstance(message, dict) and message_type == "image":
            image_url = message.get("PicUrl") or message.get("pic_url") or message.get("image_url")
            text = message.get("text") or "用户发送了一张图片，请结合图片内容处理。"
            if image_url:
                return [
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ]
        return message if isinstance(message, str) else str(message)

    def _extract_reply(self, result: Any) -> str:
        messages = result.get("messages", []) if isinstance(result, dict) else []
        for msg in reversed(messages):
            if getattr(msg, "type", None) == "ai":
                return self._stringify_content(getattr(msg, "content", ""))
            if isinstance(msg, dict) and msg.get("role") in {"assistant", "ai"}:
                return self._stringify_content(msg.get("content", ""))
        return "处理完成。"

    def _stringify_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
                else:
                    parts.append(str(item))
            return "\n".join(part for part in parts if part).strip()
        return str(content)

    def _split_reply(self, reply: str, limit: int = 500) -> List[str]:
        if not reply:
            return []
        if len(reply) <= limit:
            return [reply]
        return [reply[i:i + limit] for i in range(0, len(reply), limit)]


class AgentRuntimeRegistry:
    def __init__(self):
        self._runtimes: Dict[str, AgentRuntime] = {}

    def get(self, agent_id: str) -> AgentRuntime:
        agent_id = str(agent_id)
        if agent_id not in self._runtimes:
            self._runtimes[agent_id] = AgentRuntime(agent_id)
        return self._runtimes[agent_id]

    def status(self) -> Dict[str, Any]:
        return {agent_id: runtime.status() for agent_id, runtime in self._runtimes.items()}


runtime_registry = AgentRuntimeRegistry()
