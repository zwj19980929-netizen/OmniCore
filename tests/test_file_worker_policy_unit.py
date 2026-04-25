import shutil
import uuid
import os
from pathlib import Path

from agents.file_worker import FileWorker


def test_file_worker_write_honors_policy_confirmation(monkeypatch):
    worker = FileWorker()

    monkeypatch.setattr(
        "agents.file_worker.HumanConfirm.request_file_write_confirmation",
        lambda **kwargs: False,
    )

    result = worker.execute(
        {
            "task_id": "task_1",
            "task_type": "file_worker",
            "description": "save report",
            "params": {"action": "write", "file_path": "report.txt", "content": "hello"},
            "status": "pending",
            "result": None,
            "priority": 5,
            "execution_trace": [],
            "failure_type": None,
            "requires_confirmation": True,
        },
        {},
    )

    assert result["success"] is False
    assert "取消" in result["error"]


def test_file_worker_uses_preferred_output_directory_when_path_missing(monkeypatch):
    worker = FileWorker()
    captured = {}
    output_dir = Path.cwd() / "data" / f"test_file_worker_{uuid.uuid4().hex[:8]}" / "exports"
    output_dir.mkdir(parents=True, exist_ok=True)

    def fake_write_file(file_path, content, **kwargs):
        captured["file_path"] = file_path
        target = output_dir / "output.txt"
        target.write_text(content, encoding="utf-8")
        return {
            "success": True,
            "file_path": str(target),
        }

    monkeypatch.setattr(worker, "write_file", fake_write_file)

    try:
        result = worker.execute(
            {
                "task_id": "task_2",
                "task_type": "file_worker",
                "description": "save report",
                "params": {"action": "write", "content": "hello"},
                "status": "pending",
                "result": None,
                "priority": 5,
                "execution_trace": [],
                "failure_type": None,
                "requires_confirmation": False,
            },
            {
                "user_preferences": {
                    "default_output_directory": str(output_dir),
                }
            },
        )

        assert result["success"] is True
        assert captured["file_path"] == str(output_dir / "output.txt")
    finally:
        shutil.rmtree(output_dir.parent, ignore_errors=True)


def test_file_worker_maps_windows_desktop_path_to_current_desktop_on_posix(monkeypatch):
    if os.name == "nt":
        return

    worker = FileWorker()
    monkeypatch.setattr("agents.file_worker.settings.USER_DESKTOP_PATH", Path("C:/Users/11015/Desktop"))

    resolved = worker._resolve_path("C:/Users/11015/Desktop/generated.md")

    assert resolved == Path.home() / "Desktop" / "generated.md"
    assert "OmniCore/C:" not in str(resolved)


def test_file_worker_generate_uses_data_source_and_writes_html_with_sources(monkeypatch, tmp_path):
    captured = {}

    class FakeLLM:
        def chat(self, messages, temperature=0.7, max_tokens=None):
            captured["messages"] = messages
            return type("Response", (), {"content": "<h1>OpenAI 最新模型</h1><p>GPT-5.2 摘要</p>"})()

    monkeypatch.setattr("core.llm.LLMClient", lambda: FakeLLM())
    monkeypatch.setattr("agents.file_worker.settings.REQUIRE_HUMAN_CONFIRM", False)
    monkeypatch.setattr("agents.file_worker.settings.USER_DESKTOP_PATH", tmp_path)

    worker = FileWorker()
    task = {
        "task_id": "task_2",
        "task_type": "file_worker",
        "description": "生成中文HTML报告，总结OpenAI最新模型，并给出信息源",
        "params": {
            "action": "generate",
            "format": "html",
            "data_source": "task_1",
        },
        "execution_trace": [],
    }
    shared_memory = {
        "current_time_context": {"local_date": "2026-04-24", "current_year": 2026},
        "task_1": [
            {
                "title": "Introducing GPT-5.2",
                "summary": "GPT-5.2 is OpenAI's latest frontier model.",
                "url": "https://openai.com/index/introducing-gpt-5-2/",
            }
        ]
    }

    result = worker.execute(task, shared_memory)

    assert result["success"] is True
    assert result["file_path"].endswith("generated.html")
    content = Path(result["file_path"]).read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in content
    assert "2026-04-24" in content
    assert "https://openai.com/index/introducing-gpt-5-2/" in content
    assert "Introducing GPT-5.2" in captured["messages"][0]["content"]
    assert "2026-04-24" in captured["messages"][0]["content"]
