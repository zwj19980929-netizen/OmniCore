from pathlib import Path

from utils.cleanup_test_artifacts import discover_cleanup_targets, main, remove_targets


def test_discover_cleanup_targets_only_matches_transient_test_paths(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "test-runtime").mkdir(parents=True)
    (data_dir / "test_automation_deadbeef").mkdir(parents=True)
    (data_dir / "browser_state_smoketest").mkdir(parents=True)
    (data_dir / "runtime_state").mkdir(parents=True)
    (data_dir / "logs").mkdir(parents=True)
    (data_dir / "debug").mkdir(parents=True)

    targets = discover_cleanup_targets(tmp_path)

    names = [item.name for item in targets]
    assert names == [
        "browser_state_smoketest",
        "test-runtime",
        "test_automation_deadbeef",
    ]


def test_discover_cleanup_targets_can_include_debug_and_logs(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "test-runtime").mkdir(parents=True)
    (data_dir / "debug").mkdir(parents=True)
    (data_dir / "logs").mkdir(parents=True)

    targets = discover_cleanup_targets(
        tmp_path,
        include_debug=True,
        include_logs=True,
    )

    names = [item.name for item in targets]
    assert names == ["debug", "logs", "test-runtime"]


def test_remove_targets_honors_dry_run(tmp_path):
    target = tmp_path / "data" / "test-runtime"
    target.mkdir(parents=True)

    results = remove_targets([target], dry_run=True)

    assert target.exists()
    assert results[0].reason == "dry-run"
    assert results[0].removed is False


def test_main_removes_targets_and_preserves_non_matching_paths(tmp_path, capsys):
    data_dir = tmp_path / "data"
    test_dir = data_dir / "test_file_worker_1234abcd"
    keep_dir = data_dir / "runtime_state"
    test_dir.mkdir(parents=True)
    keep_dir.mkdir(parents=True)

    exit_code = main(["--project-root", str(tmp_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Removed 1 target(s)." in captured.out
    assert not test_dir.exists()
    assert keep_dir.exists()
