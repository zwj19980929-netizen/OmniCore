import main


def test_interactive_mode_shows_windows_native_input_notice(monkeypatch):
    printed = []

    class DummyMemory:
        _collection = object()

    class DummyEnhancedInput:
        has_readline = False

        def input(self, _prompt):
            raise EOFError

        def save_history(self):
            return None

    monkeypatch.setattr(main, "print_banner", lambda: None)
    monkeypatch.setattr(main, "ChromaMemory", lambda *args, **kwargs: DummyMemory())
    monkeypatch.setattr(main, "EnhancedInput", lambda: DummyEnhancedInput())
    monkeypatch.setattr(main.sys, "platform", "win32")
    monkeypatch.setattr(main.console, "print", lambda *args, **kwargs: printed.append(args[0] if args else ""))

    main.interactive_mode()

    assert any("pyreadline3" in str(message) for message in printed)
