"""
Daily dashboard for actionable work review.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys

import streamlit as st

st.set_page_config(
    page_title="OmniCore - Daily Dashboard",
    page_icon="📅",
    layout="wide",
)

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.statuses import BLOCKED, WAITING_FOR_APPROVAL, WAITING_FOR_EVENT  # noqa: E402
from core.runtime import get_notification_feed, get_work_dashboard, list_directory_watches  # noqa: E402
from utils.runtime_state_store import get_runtime_state_store  # noqa: E402


def main() -> None:
    st.title("Daily Dashboard")

    sessions = get_runtime_state_store().load_sessions(limit=100)
    if not sessions:
        st.info("No sessions yet. Run at least one task first.")
        return

    session_options = {item["session_id"]: item for item in sessions}
    selected_session_id = st.selectbox(
        "Select Session",
        options=list(session_options.keys())[::-1],
    )

    store = get_runtime_state_store()
    jobs = store.load_jobs(session_id=selected_session_id, limit=200)
    dashboard = get_work_dashboard(selected_session_id)
    watches = list_directory_watches(session_id=selected_session_id, limit=200)
    notifications = get_notification_feed(session_id=selected_session_id, limit=50)

    today = datetime.now().date().isoformat()
    completed_today = [
        item for item in jobs
        if str(item.get("created_at", "") or "").startswith(today)
        and str(item.get("status", "") or "") in {"completed", "completed_with_issues"}
    ]
    waiting_approval = [
        item for item in jobs
        if str(item.get("status", "") or "") == WAITING_FOR_APPROVAL
    ]
    blocked = [
        item for item in jobs
        if str(item.get("status", "") or "") == BLOCKED
    ]
    waiting_events = [
        item for item in watches
        if str(item.get("status", "") or "") == WAITING_FOR_EVENT
    ]
    todo_summary = dashboard.get("todo_summary", {}) or {}

    top = st.columns(5)
    top[0].metric("Completed Today", len(completed_today))
    top[1].metric("Waiting Approval", len(waiting_approval))
    top[2].metric("Waiting Event", len(waiting_events))
    top[3].metric("Blocked", len(blocked))
    top[4].metric("Open Todos", todo_summary.get("pending", 0) + todo_summary.get("in_progress", 0))

    st.subheader("Today Completed")
    if completed_today:
        st.dataframe(
            [
                {
                    "job_id": item.get("job_id", ""),
                    "status": item.get("status", ""),
                    "input": item.get("user_input", ""),
                    "output_preview": item.get("output_preview", ""),
                }
                for item in completed_today[::-1]
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No completed jobs today.")

    st.subheader("Needs Your Attention")
    if waiting_approval or blocked:
        attention_rows = []
        for item in waiting_approval:
            attention_rows.append(
                {
                    "type": "approval",
                    "job_id": item.get("job_id", ""),
                    "status": item.get("status", ""),
                    "input": item.get("user_input", ""),
                    "detail": item.get("error", "") or "Waiting for approval",
                }
            )
        for item in blocked:
            attention_rows.append(
                {
                    "type": "blocked",
                    "job_id": item.get("job_id", ""),
                    "status": item.get("status", ""),
                    "input": item.get("user_input", ""),
                    "detail": item.get("error", "") or "Blocked",
                }
            )
        st.dataframe(attention_rows, use_container_width=True, hide_index=True)
    else:
        st.info("Nothing needs manual attention right now.")

    st.subheader("Waiting For Event")
    if waiting_events:
        st.dataframe(
            [
                {
                    "watch_id": item.get("watch_id", ""),
                    "directory": item.get("directory_path", ""),
                    "last_event": item.get("last_event_path", ""),
                    "last_triggered_at": item.get("last_triggered_at", ""),
                }
                for item in waiting_events[::-1]
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No active event waits.")

    st.subheader("Todo Focus")
    todos = dashboard.get("todos", []) or []
    open_todos = [
        item for item in todos
        if str(item.get("status", "") or "") in {"pending", "in_progress"}
    ]
    if open_todos:
        st.dataframe(
            [
                {
                    "todo": item.get("title", ""),
                    "status": item.get("status", ""),
                    "goal_id": item.get("goal_id", ""),
                    "project_id": item.get("project_id", ""),
                }
                for item in open_todos[::-1]
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No open todos.")

    st.subheader("Recommended Next Step")
    recommendation = ""
    if waiting_approval:
        recommendation = "Review approval requests first so blocked work can continue."
    elif blocked:
        recommendation = "Inspect blocked jobs and decide whether to rerun or reject them."
    elif open_todos:
        recommendation = f"Continue the top open todo: {open_todos[-1].get('title', '')}"
    elif waiting_events:
        recommendation = "Keep active directory watches running and review newly triggered jobs."
    elif completed_today:
        recommendation = "Capture a successful workflow as a template if you will reuse it."
    else:
        recommendation = "Create a goal or queue a task to start today's work."
    st.info(recommendation)

    st.subheader("Recent Notifications")
    if notifications:
        st.dataframe(
            [
                {
                    "time": item.get("created_at", ""),
                    "level": item.get("level", ""),
                    "title": item.get("title", ""),
                    "message": item.get("message", ""),
                    "read": item.get("read", False),
                }
                for item in notifications[::-1]
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No notifications yet.")


if __name__ == "__main__":
    main()
