from agents.system_worker import SystemWorker


def test_system_worker_blocks_shell_control_tokens():
    worker = SystemWorker()

    result = worker.execute_command("echo hello && whoami", require_confirm=False)

    assert result["success"] is False
    assert "不允许的命令控制符" in result["error"]
