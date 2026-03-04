"""
Persistence helpers for plugin installation and governance overrides.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import settings


def _normalize_token(value: str) -> str:
    return str(value or "").strip()


def _normalize_path(value: str) -> str:
    token = _normalize_token(value)
    if not token:
        return ""
    return str(Path(token).expanduser())


class ToolPluginStore:
    def __init__(self, config_path: Optional[Path] = None):
        self.config_path = Path(config_path) if config_path else settings.DATA_DIR / "tool_plugins.json"
        self._lock = threading.Lock()

    def _default_config(self) -> Dict[str, List[str]]:
        return {
            "module_sources": [],
            "directory_sources": [],
            "disabled_plugin_ids": [],
            "blocked_modules": [],
            "blocked_files": [],
        }

    def _ensure_parent_dir(self) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)

    def _read_locked(self) -> Dict[str, List[str]]:
        if not self.config_path.exists():
            return self._default_config()
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return self._default_config()

        if not isinstance(payload, dict):
            return self._default_config()

        config = self._default_config()
        for key in config:
            values = payload.get(key, [])
            if isinstance(values, list):
                config[key] = [_normalize_token(item) for item in values if _normalize_token(item)]
        return config

    def _write_locked(self, config: Dict[str, List[str]]) -> Dict[str, List[str]]:
        normalized = self._default_config()
        for key in normalized:
            values = config.get(key, [])
            if isinstance(values, list):
                deduped = []
                seen = set()
                for item in values:
                    token = _normalize_token(item)
                    if not token or token in seen:
                        continue
                    seen.add(token)
                    deduped.append(token)
                normalized[key] = deduped

        self._ensure_parent_dir()
        self.config_path.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return normalized

    def get_config(self) -> Dict[str, List[str]]:
        with self._lock:
            return dict(self._read_locked())

    def install_module(self, module_name: str) -> Dict[str, List[str]]:
        token = _normalize_token(module_name)
        if not token:
            return self.get_config()
        with self._lock:
            config = self._read_locked()
            if token not in config["module_sources"]:
                config["module_sources"].append(token)
            config["blocked_modules"] = [item for item in config["blocked_modules"] if item != token]
            return self._write_locked(config)

    def install_directory(self, directory: str) -> Dict[str, List[str]]:
        token = _normalize_path(directory)
        if not token:
            return self.get_config()
        with self._lock:
            config = self._read_locked()
            if token not in config["directory_sources"]:
                config["directory_sources"].append(token)
            prefix = token.rstrip("\\/")
            config["blocked_files"] = [
                item for item in config["blocked_files"]
                if not _normalize_path(item).startswith(prefix)
            ]
            return self._write_locked(config)

    def disable_plugin(self, plugin_id: str) -> Dict[str, List[str]]:
        token = _normalize_token(plugin_id)
        if not token:
            return self.get_config()
        with self._lock:
            config = self._read_locked()
            if token not in config["disabled_plugin_ids"]:
                config["disabled_plugin_ids"].append(token)
            return self._write_locked(config)

    def enable_plugin(self, plugin_id: str) -> Dict[str, List[str]]:
        token = _normalize_token(plugin_id)
        if not token:
            return self.get_config()
        with self._lock:
            config = self._read_locked()
            config["disabled_plugin_ids"] = [item for item in config["disabled_plugin_ids"] if item != token]
            return self._write_locked(config)

    def uninstall_plugin(self, plugin_id: str, *, source: str = "") -> Dict[str, List[str]]:
        token = _normalize_token(plugin_id)
        src = _normalize_token(source)
        with self._lock:
            config = self._read_locked()
            if token and token not in config["disabled_plugin_ids"]:
                config["disabled_plugin_ids"].append(token)

            if src:
                if src.endswith(".py") or "\\" in src or "/" in src:
                    normalized_src = _normalize_path(src)
                    if normalized_src and normalized_src not in config["blocked_files"]:
                        config["blocked_files"].append(normalized_src)
                    config["module_sources"] = [item for item in config["module_sources"] if item != src]
                else:
                    if src not in config["blocked_modules"]:
                        config["blocked_modules"].append(src)
                    config["module_sources"] = [item for item in config["module_sources"] if item != src]

            return self._write_locked(config)


_tool_plugin_store: Optional[ToolPluginStore] = None


def get_tool_plugin_store() -> ToolPluginStore:
    global _tool_plugin_store
    if _tool_plugin_store is None:
        _tool_plugin_store = ToolPluginStore()
    return _tool_plugin_store
