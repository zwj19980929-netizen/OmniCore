import shutil
import uuid
from pathlib import Path

from utils.tool_plugin_store import ToolPluginStore


def _make_store():
    root = Path.cwd() / "data" / f"test_tool_plugin_store_file_{uuid.uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=True)
    return root, ToolPluginStore(config_path=root / "tool_plugins.json")


def test_tool_plugin_store_tracks_install_disable_and_uninstall():
    root, store = _make_store()

    try:
        store.install_module("plugin.module")
        store.install_directory("D:/plugins")
        store.disable_plugin("plugin.id")
        config = store.uninstall_plugin("plugin.id", source="plugin.module")

        assert "plugin.module" not in config["module_sources"]
        assert str(Path("D:/plugins")) in config["directory_sources"]
        assert "plugin.id" in config["disabled_plugin_ids"]
        assert "plugin.module" in config["blocked_modules"]
    finally:
        shutil.rmtree(root, ignore_errors=True)
