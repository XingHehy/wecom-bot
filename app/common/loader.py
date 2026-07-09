from __future__ import annotations

import importlib
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List

import yaml

from app.common.base_agent import AgentPlugin, AgentProfile, profile_from_config
from app.logger import get_logger

logger = get_logger("agent_plugin_loader")

PLUGIN_ROOT = Path(__file__).resolve().parents[1] / "agent_plugins"
PLUGIN_PACKAGE_PREFIX = "app.agent_plugins"


class AgentPluginRegistry:
    def __init__(self, plugins: Dict[str, AgentPlugin]):
        self.plugins = plugins

    def all_profiles(self, *, include_disabled: bool = False) -> Dict[str, AgentProfile]:
        return {
            key: plugin.profile
            for key, plugin in self.plugins.items()
            if include_disabled or plugin.profile.enabled
        }

    def enabled_keys(self) -> List[str]:
        return list(self.all_profiles().keys())

    def get(self, key: str) -> AgentPlugin:
        return self.plugins[key]

    def normalize_keys(self, values: Iterable[str]) -> List[str]:
        profiles = self.all_profiles()
        normalized: List[str] = []
        for raw in values:
            key = str(raw)
            if key in profiles and key not in normalized:
                normalized.append(key)
            elif key not in profiles:
                logger.warning(f"忽略未启用或不存在的 agent plugin: {key}")
        return normalized

    def describe(self, keys: Iterable[str]) -> str:
        profiles = self.all_profiles(include_disabled=True)
        lines = ["当前应用启用的 agent plugins："]
        for key in keys:
            profile = profiles.get(key)
            if profile:
                lines.append(f"- {profile.display_name}（{key}）：{profile.description}")
        return "\n".join(lines)


def _load_optional_module(package: str, name: str):
    module_name = f"{package}.{name}"
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name == module_name:
            return None
        raise


def _load_plugin(plugin_dir: Path) -> AgentPlugin | None:
    config_path = plugin_dir / "agent_config.yaml"
    if not config_path.exists():
        return None
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    package = f"{PLUGIN_PACKAGE_PREFIX}.{plugin_dir.name}"
    profile = profile_from_config(data, package)
    tools_module = _load_optional_module(package, "tools")
    logic_module = _load_optional_module(package, "logic")
    return AgentPlugin(profile=profile, tools_module=tools_module, logic_module=logic_module)


@lru_cache(maxsize=1)
def load_agent_plugins() -> AgentPluginRegistry:
    plugins: Dict[str, AgentPlugin] = {}
    if not PLUGIN_ROOT.exists():
        logger.warning(f"agent plugin 目录不存在: {PLUGIN_ROOT}")
        return AgentPluginRegistry(plugins)
    for plugin_dir in sorted(PLUGIN_ROOT.iterdir()):
        if not plugin_dir.is_dir() or plugin_dir.name.startswith("_"):
            continue
        try:
            plugin = _load_plugin(plugin_dir)
        except Exception as exc:
            logger.error(f"加载 agent plugin 失败: {plugin_dir.name}, error={exc}")
            continue
        if not plugin:
            continue
        if plugin.profile.key in plugins:
            logger.warning(f"重复 agent key={plugin.profile.key}，忽略 {plugin_dir}")
            continue
        plugins[plugin.profile.key] = plugin
    logger.info(f"agent plugins 加载完成，数量={len(plugins)}")
    return AgentPluginRegistry(plugins)


def reload_agent_plugins() -> AgentPluginRegistry:
    load_agent_plugins.cache_clear()
    return load_agent_plugins()
