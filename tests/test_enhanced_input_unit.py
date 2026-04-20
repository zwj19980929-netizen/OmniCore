from utils import enhanced_input as enhanced_input_module


def test_windows_disables_pyreadline3_by_default(monkeypatch):
    monkeypatch.setattr(enhanced_input_module.sys, "platform", "win32")
    monkeypatch.delenv("OMNICORE_ENABLE_PYREADLINE3", raising=False)

    instance = enhanced_input_module.EnhancedInput(history_file="data/test-runtime/history.txt")

    assert instance.has_readline is False
    assert instance.readline_disabled_reason == "windows_pyreadline3_disabled"
