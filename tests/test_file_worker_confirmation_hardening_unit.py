from pathlib import Path
import uuid

from agents.file_worker import FileWorker


def _test_output_path() -> str:
    base = Path("data/test-runtime/manual-file-worker")
    base.mkdir(parents=True, exist_ok=True)
    return str(base / f"{uuid.uuid4().hex}.txt")


def test_write_file_does_not_bypass_confirmation_with_require_confirm_false(monkeypatch):
    worker = FileWorker()

    monkeypatch.setattr("agents.file_worker.settings.REQUIRE_HUMAN_CONFIRM", True)
    monkeypatch.setattr(
        "agents.file_worker.HumanConfirm.request_file_write_confirmation",
        lambda **kwargs: False,
    )

    result = worker.write_file(
        _test_output_path(),
        "hello",
        require_confirm=False,
    )

    assert result["success"] is False
    assert "取消" in result["error"]


def test_write_file_accepts_internal_preconfirmed_override(monkeypatch):
    worker = FileWorker()

    monkeypatch.setattr("agents.file_worker.settings.REQUIRE_HUMAN_CONFIRM", True)

    def _unexpected_confirmation(**kwargs):
        raise AssertionError("confirmation should be skipped for preconfirmed writes")

    monkeypatch.setattr(
        "agents.file_worker.HumanConfirm.request_file_write_confirmation",
        _unexpected_confirmation,
    )

    result = worker.write_file(
        _test_output_path(),
        "hello",
        require_confirm=False,
        policy_preconfirmed=True,
    )

    assert result["success"] is True
    assert result["file_path"].endswith(".txt")
