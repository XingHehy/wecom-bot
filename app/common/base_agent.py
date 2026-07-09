from __future__ import annotations

from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class AgentProfile:
    key: str
    name: str
    display_name: str
    description: str
    system_prompt: str
    status: str = "enable"
    category: str = "chat"
    model_profile: str = "dashscope"
    model: Optional[str] = None
    temperature: float = 0.7
    max_tokens: Optional[int] = None
    custom_tools: List[str] = field(default_factory=list)
    global_tools: List[str] = field(default_factory=list)
    sub_agents: List[str] = field(default_factory=list)
    route_keywords: List[str] = field(default_factory=list)
    plugin_package: str = ""

    @property
    def enabled(self) -> bool:
        return str(self.status).lower() in {"enable", "enabled", "true", "1", "on"}


@dataclass
class AgentPlugin:
    profile: AgentProfile
    tools_module: Optional[ModuleType] = None
    logic_module: Optional[ModuleType] = None

    def route_score(self, message_text: str) -> int:
        if self.logic_module and hasattr(self.logic_module, "route_score"):
            try:
                return int(self.logic_module.route_score(message_text, self.profile))
            except Exception:
                return 0
        lowered = message_text.lower()
        return sum(1 for keyword in self.profile.route_keywords if str(keyword).lower() in lowered)

    def build_custom_tools(self, *, agent_id: str, user_id: str) -> List[Any]:
        if not self.tools_module or not hasattr(self.tools_module, "build_tools"):
            return []
        return list(
            self.tools_module.build_tools(
                agent_id=agent_id,
                user_id=user_id,
                enabled_tools=self.profile.custom_tools,
            )
        )


def profile_from_config(data: Dict[str, Any], plugin_package: str) -> AgentProfile:
    return AgentProfile(
        key=str(data["key"]),
        name=str(data.get("name") or data["key"]),
        display_name=str(data.get("display_name") or data.get("name") or data["key"]),
        description=str(data.get("description") or ""),
        status=str(data.get("status") or "enable"),
        category=str(data.get("category") or "chat"),
        model_profile=str(data.get("model_profile") or "dashscope"),
        model=data.get("model"),
        temperature=float(data.get("temperature", 0.7)),
        max_tokens=data.get("max_tokens"),
        system_prompt=str(data.get("system_prompt") or ""),
        custom_tools=list(data.get("custom_tools") or []),
        global_tools=list(data.get("global_tools") or []),
        sub_agents=list(data.get("sub_agents") or []),
        route_keywords=list(data.get("route_keywords") or []),
        plugin_package=plugin_package,
    )
