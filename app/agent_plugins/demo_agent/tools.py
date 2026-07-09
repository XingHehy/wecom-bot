def build_tools(*, agent_id: str, user_id: str, enabled_tools: list[str]):
    from langchain_core.tools import tool

    from app.common.loader import load_agent_plugins
    from app.config.settings import AGENT_CONFIGS
    from app.memory.checkpointer import checkpointer_provider

    @tool
    def demo_system_status() -> str:
        """演示系统状态：当前应用启用的 plugins、memory backend、插件数量。"""
        registry = load_agent_plugins()
        agent_conf = AGENT_CONFIGS.get(str(agent_id), {})
        enabled = agent_conf.get("plugins") or []
        return (
            "wxbot 系统演示状态\n"
            f"当前企业微信应用: {agent_id}\n"
            f"当前用户: {user_id}\n"
            f"启用 agent plugins: {enabled}\n"
            f"已发现 agent 插件数: {len(registry.plugins)}\n"
            f"LangGraph memory backend: {checkpointer_provider.backend}"
        )

    @tool
    def demo_list_agent_plugins() -> str:
        """列出通过 app/agent_plugins/*/agent_config.yaml 动态加载的 agent 插件。"""
        registry = load_agent_plugins()
        lines = ["动态加载的 agent 插件："]
        for key, plugin in sorted(registry.plugins.items()):
            profile = plugin.profile
            status = "enabled" if profile.enabled else "disabled"
            lines.append(f"- {key}: {profile.display_name} | {profile.category} | {status} | {profile.description}")
        return "\n".join(lines)

    @tool
    def demo_current_context() -> str:
        """展示当前请求上下文中的应用、用户和配置字段，不输出任何 secret。"""
        agent_conf = AGENT_CONFIGS.get(str(agent_id), {})
        safe_keys = [
            "name",
            "agent_id",
            "plugins",
            "fetch_user_info",
            "include_time_info",
            "oauth",
        ]
        safe_conf = {key: agent_conf.get(key) for key in safe_keys if key in agent_conf}
        return f"当前上下文\nagent_id={agent_id}\nuser_id={user_id}\n安全配置摘要={safe_conf}"

    @tool
    def demo_echo(text: str) -> str:
        """回显用户输入，用于验证工具调用链路。"""
        return f"echo: {text}"

    all_tools = [demo_system_status, demo_list_agent_plugins, demo_current_context, demo_echo]
    return [item for item in all_tools if item.name in enabled_tools]
