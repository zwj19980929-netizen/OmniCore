"""
Session and job history view for the runtime state store.
"""
from __future__ import annotations

import streamlit as st
from pathlib import Path
import sys

st.set_page_config(
    page_title="OmniCore - Session History",
    page_icon="🗂️",
    layout="wide",
)

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.runtime import run_next_queued_task, submit_task  # noqa: E402
from utils.runtime_state_store import get_runtime_state_store  # noqa: E402


def _render_queue_summary(summary: dict) -> None:
    cols = st.columns(5)
    cols[0].metric("Queued", summary.get("queued", 0))
    cols[1].metric("Running", summary.get("running", 0))
    cols[2].metric("Completed", summary.get("completed", 0))
    cols[3].metric("Error", summary.get("error", 0))
    cols[4].metric("Cancelled", summary.get("cancelled", 0))


def main() -> None:
    st.title("Session History")

    store = get_runtime_state_store()
    sessions = store.load_sessions(limit=50)
    queue_summary = store.get_queue_summary()

    st.subheader("Job Queue")
    _render_queue_summary(queue_summary)

    with st.expander("Queue a Task"):
        queued_input = st.text_area("Task Input", height=100)
        queue_session_id = st.text_input("Session ID (optional)", value="")
        if st.button("Submit to Queue", use_container_width=True):
            if queued_input.strip():
                submission = submit_task(queued_input, session_id=queue_session_id or None)
                st.success(
                    f"Queued job {submission.get('job_id', '')} in session {submission.get('session_id', '')}"
                )
            else:
                st.warning("Enter a task before submitting.")

    if st.button("Run Next Queued Job", use_container_width=True):
        result = run_next_queued_task()
        if result is None:
            st.info("No queued jobs available.")
        elif result.get("success"):
            st.success(
                f"Executed {result.get('job_id', '')}: {result.get('output', 'Job completed')}"
            )
        else:
            st.error(
                f"Executed {result.get('job_id', '')}: {result.get('error') or result.get('output') or 'Job failed'}"
            )

    st.divider()
    st.subheader("Sessions")

    if not sessions:
        st.info("No sessions recorded yet.")
        return

    session_options = {item["session_id"]: item for item in sessions}
    selected_session_id = st.selectbox(
        "Select Session",
        options=list(session_options.keys())[::-1],
    )
    selected_session = session_options[selected_session_id]

    left, right = st.columns(2)
    left.metric("Job Count", selected_session.get("job_count", 0))
    right.metric("Last Job", selected_session.get("last_job_id", ""))
    st.caption(
        f"Updated: {selected_session.get('updated_at', '')} | Last Input: {selected_session.get('last_user_input', '')}"
    )

    jobs = store.load_jobs(session_id=selected_session_id, limit=100)
    artifacts = store.load_artifacts(session_id=selected_session_id, limit=200)

    st.subheader("Jobs")
    if jobs:
        st.dataframe(
            [
                {
                    "job_id": item.get("job_id", ""),
                    "status": item.get("status", ""),
                    "success": item.get("success", ""),
                    "created_at": item.get("created_at", ""),
                    "tasks_completed": item.get("tasks_completed", 0),
                    "task_count": item.get("task_count", 0),
                    "user_input": item.get("user_input", ""),
                }
                for item in jobs[::-1]
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No jobs in this session yet.")

    st.subheader("Artifacts")
    if artifacts:
        st.dataframe(
            [
                {
                    "artifact_id": item.get("artifact_id", ""),
                    "job_id": item.get("job_id", ""),
                    "type": item.get("artifact_type", ""),
                    "name": item.get("name", ""),
                    "path": item.get("path", ""),
                    "preview": item.get("preview", ""),
                    "created_at": item.get("created_at", ""),
                }
                for item in artifacts[::-1]
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No artifacts in this session yet.")


if __name__ == "__main__":
    main()
