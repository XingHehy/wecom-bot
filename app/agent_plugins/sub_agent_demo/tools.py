def build_tools(*, agent_id: str, user_id: str, enabled_tools: list[str]):
    from langchain_core.tools import tool

    @tool
    def sub_agent_demo_reply(task: str) -> str:
        """返回 sub-agent demo 的处理结果，用于验证委托调用链路。"""
        return (
            "Sub Agent Demo 已处理任务\n"
            f"agent_id={agent_id}\n"
            f"user_id={user_id}\n"
            f"task={task}"
        )

    all_tools = [sub_agent_demo_reply]
    return [item for item in all_tools if item.name in enabled_tools]
