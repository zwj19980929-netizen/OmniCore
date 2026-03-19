import shutil
import uuid
from pathlib import Path

from utils.artifact_store import ArtifactStore
from utils.work_context_store import WorkContextStore


def _make_test_dir() -> Path:
    target = Path.cwd() / "data" / f"test_work_context_{uuid.uuid4().hex[:8]}"
    target.mkdir(parents=True, exist_ok=True)
    return target


def test_work_context_store_tracks_goals_todos_and_experiences():
    state_dir = _make_test_dir()
    store = WorkContextStore(state_dir=state_dir)

    try:
        goal = store.create_goal(session_id="session_a", title="Track project")
        project = store.create_project(
            session_id="session_a",
            title="Weekly updates",
            goal_id=goal["goal_id"],
        )
        todo = store.create_todo(
            session_id="session_a",
            title="Collect status notes",
            goal_id=goal["goal_id"],
            project_id=project["project_id"],
        )
        store.record_experience(
            session_id="session_a",
            job_id="job_1",
            user_input="collect weekly status notes",
            intent="information_query",
            tool_sequence=["web.fetch_and_extract", "file.read_write"],
            success=True,
            goal_id=goal["goal_id"],
            project_id=project["project_id"],
            todo_id=todo["todo_id"],
            summary="Fetched notes and saved a report",
        )
        suggestions = store.suggest_success_paths(
            query="weekly status notes",
            session_id="session_a",
            goal_id=goal["goal_id"],
        )
        snapshot = store.get_context_snapshot(
            session_id="session_a",
            goal_id=goal["goal_id"],
            project_id=project["project_id"],
            todo_id=todo["todo_id"],
        )

        assert suggestions
        assert suggestions[0]["tool_sequence"][0] == "web.fetch_and_extract"
        assert snapshot["goal"]["goal_id"] == goal["goal_id"]
        assert snapshot["project"]["project_id"] == project["project_id"]
        assert snapshot["todo"]["todo_id"] == todo["todo_id"]
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


def test_artifact_store_catalogs_cross_job_artifacts():
    state_dir = _make_test_dir()
    store = ArtifactStore(catalog_path=state_dir / "artifact_catalog.jsonl")

    try:
        recorded = store.record_artifacts(
            session_id="session_b",
            job_id="job_2",
            goal_id="goal_1",
            artifacts=[
                {
                    "artifact_id": "artifact_1",
                    "artifact_type": "file",
                    "name": "report.txt",
                    "path": "D:/tmp/report.txt",
                }
            ],
        )
        search = store.search_artifacts(
            session_id="session_b",
            goal_id="goal_1",
            query="report",
        )

        assert recorded
        assert search[0]["name"] == "report.txt"
        assert search[0]["goal_id"] == "goal_1"
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


def test_work_context_store_recalls_chinese_queries_with_mixed_wording():
    state_dir = _make_test_dir()
    store = WorkContextStore(state_dir=state_dir)

    try:
        goal = store.create_goal(session_id="session_cn", title="周报整理")
        store.record_experience(
            session_id="session_cn",
            job_id="job_cn_1",
            user_input="整理本周项目周报并输出摘要",
            intent="information_query",
            tool_sequence=["web.fetch_and_extract", "file.read_write"],
            success=True,
            goal_id=goal["goal_id"],
            summary="已抓取周报素材并生成本周摘要",
        )

        suggestions = store.suggest_success_paths(
            query="帮我汇总这周的项目摘要",
            session_id="session_cn",
            goal_id=goal["goal_id"],
        )

        assert suggestions
        assert suggestions[0]["tool_sequence"] == ["web.fetch_and_extract", "file.read_write"]
        assert suggestions[0]["match_score"] > 0
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)


def test_work_context_store_surfaces_failure_avoidance_hints():
    state_dir = _make_test_dir()
    store = WorkContextStore(state_dir=state_dir)

    try:
        goal = store.create_goal(session_id="session_fail", title="Weather lookup")
        store.record_experience(
            session_id="session_fail",
            job_id="job_fail_1",
            user_input="查询上海天气并保存截图",
            intent="weather_query",
            tool_sequence=["browser.interact", "file.read_write"],
            success=False,
            goal_id=goal["goal_id"],
            summary="页面一直重定向到广告页",
            failure_reason="The browser flow kept landing on an irrelevant ad page.",
            visited_urls=["https://example.com/weather", "https://example.com/ad"],
        )

        failures = store.suggest_failure_avoidance(
            query="帮我查上海天气并保存结果",
            session_id="session_fail",
            goal_id=goal["goal_id"],
        )

        assert failures
        assert failures[0]["tool_sequence"][0] == "browser.interact"
        assert failures[0]["failure_reason"].startswith("The browser flow")
        assert failures[0]["match_score"] > 0
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)
