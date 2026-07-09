"""Compatibility shim for dynamic agent plugin profiles.

Profiles are loaded from ``app/agent_plugins/*/agent_config.yaml``. Do not add
hard-coded agent personas here.
"""

from app.common.loader import load_agent_plugins


def all_profiles():
    return load_agent_plugins().all_profiles()


def describe_enabled(enabled):
    return load_agent_plugins().describe(enabled)
