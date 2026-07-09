def build_tools(*, agent_id: str, user_id: str, enabled_tools: list[str]):
    from app.tools.schedule import build_schedule_tools

    tools = build_schedule_tools(agent_id, user_id)
    return [tool for tool in tools if tool.name in enabled_tools]
