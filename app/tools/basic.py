from __future__ import annotations

import datetime
from typing import Callable, List

from langchain_core.tools import tool


def build_basic_tools(describe_agents: Callable[[], str]) -> List:
    @tool
    def time_query() -> str:
        """查询当前本地时间。"""
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @tool
    def help_text() -> str:
        """查看当前企业微信应用启用的 agent 和工具能力。"""
        return describe_agents()

    return [time_query, help_text]
