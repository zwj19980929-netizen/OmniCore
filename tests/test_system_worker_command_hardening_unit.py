from agents.system_worker import SystemWorker


def test_system_worker_blocks_shell_launchers():
    worker = SystemWorker()

    result = worker.execute_command('powershell -Command "Get-Date"', require_confirm=False)

    assert result["success"] is False
    assert "shell/interpreter" in result["error"]


def test_system_worker_executes_without_shell(monkeypatch):
    worker = SystemWorker()
    captured = {}

    class _Completed:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def _fake_run(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _Completed()

    monkeypatch.setattr("agents.system_worker.subprocess.run", _fake_run)

    result = worker.execute_command("python --version", require_confirm=False)

    assert result["success"] is True
    assert captured["args"][0] == ["python", "--version"]
    assert captured["kwargs"]["shell"] is False
