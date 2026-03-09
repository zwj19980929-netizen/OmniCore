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

from core.statuses import WAITING_FOR_APPROVAL, WAITING_JOB_STATUSES  # noqa: E402
from core.runtime import (  # noqa: E402
    approve_waiting_job,
    create_scheduled_task,
    delete_scheduled_task,
    get_background_worker_status,
    get_notification_feed,
    get_scheduled_tasks,
    get_user_preferences,
    mark_all_notifications_read,
    mark_notification_read,
    pause_scheduled_task,
    reject_waiting_job,
    rerun_job,
    resume_scheduled_task,
    resume_job_from_checkpoint,
    resume_failed_job,
    run_next_queued_task,
    start_background_worker,
    stop_background_worker,
    submit_task,
    update_user_preferences,
)
from core.tool_adapters import (  # noqa: E402
    disable_tool_plugin,
    enable_tool_plugin,
    get_tool_adapter_plugin_status,
    install_tool_plugin_directory,
    install_tool_plugin_module,
    uninstall_tool_plugin,
)
from utils.runtime_state_store import get_runtime_state_store  # noqa: E402


def _render_queue_summary(summary: dict) -> None:
    cols = st.columns(8)
    cols[0].metric("Queued", summary.get("queued", 0))
    cols[1].metric("Running", summary.get("running", 0))
    cols[2].metric("Completed", summary.get("completed", 0))
    cols[3].metric("Error", summary.get("error", 0))
    cols[4].metric("Cancelled", summary.get("cancelled", 0))
    cols[5].metric("Await Approval", summary.get("waiting_for_approval", 0))
    cols[6].metric("Await Event", summary.get("waiting_for_event", 0))
    cols[7].metric("Blocked", summary.get("blocked", 0))


def _render_schedule_summary(summary: dict) -> None:
    cols = st.columns(3)
    cols[0].metric("Active Schedules", summary.get("active", 0))
    cols[1].metric("Paused", summary.get("paused", 0))
    cols[2].metric("Completed", summary.get("completed", 0))


def _render_notifications(rows: list[dict]) -> None:
    st.subheader("Notifications")
    if not rows:
        st.info("No notifications recorded for this scope yet.")
        return

    unread = [item for item in rows if not bool(item.get("read", False))]
    left, right = st.columns(2)
    left.metric("Unread", len(unread))
    right.metric("Action Required", sum(1 for item in rows if bool(item.get("requires_action", False))))

    st.dataframe(
        [
            {
                "time": item.get("created_at", ""),
                "level": item.get("level", ""),
                "category": item.get("category", ""),
                "title": item.get("title", ""),
                "message": item.get("message", ""),
                "job_id": item.get("job_id", ""),
                "read": item.get("read", False),
                "requires_action": item.get("requires_action", False),
                "notification_id": item.get("notification_id", ""),
            }
            for item in rows[::-1]
        ],
        use_container_width=True,
        hide_index=True,
    )


def _render_policy_decisions(decisions: list[dict]) -> None:
    st.subheader("Policy Decisions")
    if not decisions:
        st.info("No policy decisions recorded for this job.")
        return

    st.dataframe(
        [
            {
                "task_id": item.get("task_id", ""),
                "tool_name": item.get("tool_name", ""),
                "decision": item.get("decision", ""),
                "risk_level": item.get("risk_level", ""),
                "target_resource": item.get("target_resource", ""),
                "reason": item.get("reason", ""),
                "approved_by": item.get("approved_by", ""),
                "approved_at": item.get("approved_at", ""),
            }
            for item in decisions
            if isinstance(item, dict)
        ],
        use_container_width=True,
        hide_index=True,
    )


def _collect_policy_decisions(jobs: list[dict]) -> list[dict]:
    rows: list[dict] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        job_id = str(job.get("job_id", "") or "")
        job_status = str(job.get("status", "") or "")
        user_input = str(job.get("user_input", "") or "")
        for item in job.get("policy_decisions", []) or []:
            if not isinstance(item, dict):
                continue
            rows.append(
                {
                    "job_id": job_id,
                    "job_status": job_status,
                    "user_input": user_input,
                    "task_id": item.get("task_id", ""),
                    "tool_name": item.get("tool_name", ""),
                    "decision": item.get("decision", ""),
                    "risk_level": item.get("risk_level", ""),
                    "target_resource": item.get("target_resource", ""),
                    "reason": item.get("reason", ""),
                    "approved_by": item.get("approved_by", ""),
                    "approved_at": item.get("approved_at", ""),
                }
            )
    return rows


def _summarize_counts(rows: list[dict], field: str) -> list[dict]:
    counts: dict[str, int] = {}
    for item in rows:
        key = str(item.get(field, "") or "").strip() or "(empty)"
        counts[key] = counts.get(key, 0) + 1
    return [
        {field: key, "count": value}
        for key, value in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))
    ]


def _render_policy_audit(jobs: list[dict]) -> None:
    st.subheader("Policy Audit")
    rows = _collect_policy_decisions(jobs)
    if not rows:
        st.info("No policy decisions recorded for this session yet.")
        return

    decision_values = sorted({str(item.get("decision", "") or "") for item in rows if item.get("decision")})
    risk_values = sorted({str(item.get("risk_level", "") or "") for item in rows if item.get("risk_level")})
    tool_values = sorted({str(item.get("tool_name", "") or "") for item in rows if item.get("tool_name")})

    filter_left, filter_mid, filter_right = st.columns(3)
    decision_filter = filter_left.selectbox(
        "Decision Filter",
        options=["all", *decision_values],
        key="policy_audit_decision_filter",
    )
    risk_filter = filter_mid.selectbox(
        "Risk Filter",
        options=["all", *risk_values],
        key="policy_audit_risk_filter",
    )
    tool_filter = filter_right.selectbox(
        "Tool Filter",
        options=["all", *tool_values],
        key="policy_audit_tool_filter",
    )

    filtered = [
        item for item in rows
        if (decision_filter == "all" or item.get("decision") == decision_filter)
        and (risk_filter == "all" or item.get("risk_level") == risk_filter)
        and (tool_filter == "all" or item.get("tool_name") == tool_filter)
    ]

    if not filtered:
        st.info("No policy decisions match the current filters.")
        return

    pending_count = sum(1 for item in filtered if item.get("decision") == "pending")
    approved_count = sum(1 for item in filtered if item.get("decision") == "approved")
    rejected_count = sum(1 for item in filtered if item.get("decision") == "rejected")

    metric_left, metric_mid, metric_right, metric_last = st.columns(4)
    metric_left.metric("Visible Decisions", len(filtered))
    metric_mid.metric("Pending", pending_count)
    metric_right.metric("Approved", approved_count)
    metric_last.metric("Rejected", rejected_count)

    summary_left, summary_right = st.columns(2)
    with summary_left:
        st.caption("By Decision")
        st.dataframe(
            _summarize_counts(filtered, "decision"),
            use_container_width=True,
            hide_index=True,
        )
    with summary_right:
        st.caption("By Tool")
        st.dataframe(
            _summarize_counts(filtered, "tool_name"),
            use_container_width=True,
            hide_index=True,
        )

    st.dataframe(filtered, use_container_width=True, hide_index=True)


def _render_tool_adapter_plugins() -> None:
    status = get_tool_adapter_plugin_status()
    configured = status.get("configured_modules", [])
    configured_dirs = status.get("configured_directories", [])
    installed_modules = status.get("installed_modules", [])
    installed_directories = status.get("installed_directories", [])
    disabled_plugin_ids = status.get("disabled_plugin_ids", [])
    blocked_modules = status.get("blocked_modules", [])
    blocked_files = status.get("blocked_files", [])
    loaded = status.get("loaded_modules", [])
    loaded_files = status.get("loaded_files", [])
    registered_tools = status.get("registered_tools", [])
    plugin_manifests = status.get("plugin_manifests", [])
    errors = status.get("load_errors", {})

    if not configured and not configured_dirs and not installed_modules and not installed_directories and not loaded and not loaded_files and not registered_tools and not plugin_manifests and not errors:
        return

    with st.expander("Tool Adapter Plugins"):
        metric_left, metric_mid, metric_right = st.columns(3)
        metric_left.metric("Configured", len(configured) + len(configured_dirs) + len(installed_modules) + len(installed_directories))
        metric_mid.metric("Loaded", len(loaded) + len(loaded_files))
        metric_right.metric("Errors", len(errors))

        st.caption("Plugin Management")
        install_module_value = st.text_input("Install Module", key="plugin_install_module")
        install_dir_value = st.text_input("Install Directory", key="plugin_install_dir")
        action_left, action_right = st.columns(2)
        if action_left.button("Install Module Source", use_container_width=True):
            if install_module_value.strip():
                install_tool_plugin_module(install_module_value.strip())
                st.success(f"Installed plugin module source: {install_module_value.strip()}")
                st.rerun()
            else:
                st.warning("Enter a module name first.")
        if action_right.button("Install Directory Source", use_container_width=True):
            if install_dir_value.strip():
                install_tool_plugin_directory(install_dir_value.strip())
                st.success(f"Installed plugin directory source: {install_dir_value.strip()}")
                st.rerun()
            else:
                st.warning("Enter a directory path first.")

        if configured:
            st.caption("Configured Modules")
            st.code("\n".join(configured), language="text")
        if configured_dirs:
            st.caption("Configured Directories")
            st.code("\n".join(configured_dirs), language="text")
        if installed_modules:
            st.caption("Installed Module Sources")
            st.code("\n".join(installed_modules), language="text")
        if installed_directories:
            st.caption("Installed Directory Sources")
            st.code("\n".join(installed_directories), language="text")
        if disabled_plugin_ids:
            st.caption("Disabled Plugin IDs")
            st.code("\n".join(disabled_plugin_ids), language="text")
        if blocked_modules:
            st.caption("Blocked Modules")
            st.code("\n".join(blocked_modules), language="text")
        if blocked_files:
            st.caption("Blocked Files")
            st.code("\n".join(blocked_files), language="text")
        if loaded:
            st.caption("Loaded Modules")
            st.code("\n".join(loaded), language="text")
        if loaded_files:
            st.caption("Loaded Files")
            st.code("\n".join(loaded_files), language="text")
        if registered_tools:
            st.caption("Registered Plugin Tools")
            st.code("\n".join(registered_tools), language="text")
        if plugin_manifests:
            st.caption("Plugin Manifests")
            st.dataframe(
                [
                    {
                        "plugin_id": item.get("plugin_id", ""),
                        "version": item.get("version", ""),
                        "enabled": item.get("enabled", False),
                        "dependencies": ", ".join(item.get("dependencies", []) or []),
                        "tools": ", ".join(item.get("tools", []) or []),
                        "source": item.get("source", ""),
                    }
                    for item in plugin_manifests
                    if isinstance(item, dict)
                ],
                use_container_width=True,
                hide_index=True,
            )
            plugin_ids = [
                item.get("plugin_id", "")
                for item in plugin_manifests
                if isinstance(item, dict) and item.get("plugin_id")
            ]
            selected_plugin_id = st.selectbox(
                "Manage Plugin",
                options=plugin_ids,
                key="plugin_manage_plugin_id",
            )
            manage_left, manage_mid, manage_right = st.columns(3)
            if manage_left.button("Enable Plugin", use_container_width=True):
                enable_tool_plugin(selected_plugin_id)
                st.success(f"Enabled plugin: {selected_plugin_id}")
                st.rerun()
            if manage_mid.button("Disable Plugin", use_container_width=True):
                disable_tool_plugin(selected_plugin_id)
                st.success(f"Disabled plugin: {selected_plugin_id}")
                st.rerun()
            if manage_right.button("Uninstall Plugin", use_container_width=True):
                uninstall_tool_plugin(selected_plugin_id)
                st.success(f"Uninstalled plugin: {selected_plugin_id}")
                st.rerun()
        if errors:
            st.caption("Load Errors")
            st.dataframe(
                [
                    {"module": module_name, "error": message}
                    for module_name, message in sorted(errors.items())
                ],
                use_container_width=True,
                hide_index=True,
            )


def _render_checkpoints(checkpoints: list[dict]) -> None:
    st.subheader("Checkpoints")
    if not checkpoints:
        st.info("No checkpoints recorded for this job yet.")
        return

    st.dataframe(
        [
            {
                "checkpoint_id": item.get("checkpoint_id", ""),
                "stage": item.get("stage", ""),
                "created_at": item.get("created_at", ""),
                "note": item.get("note", ""),
            }
            for item in checkpoints[::-1]
        ],
        use_container_width=True,
        hide_index=True,
    )


def main() -> None:
    st.title("Session History")

    store = get_runtime_state_store()
    sessions = store.load_sessions(limit=50)
    queue_summary = store.get_queue_summary()
    schedule_summary = store.get_schedule_summary()
    worker_status = get_background_worker_status()

    st.subheader("Job Queue")
    _render_queue_summary(queue_summary)
    _render_schedule_summary(schedule_summary)
    st.caption(f"Background worker running: {'Yes' if worker_status.get('running') else 'No'}")
    persisted_status = worker_status.get("persisted") or {}
    if persisted_status:
        st.caption(
            "Worker state: "
            f"{persisted_status.get('status', '')} | "
            f"Mode: {persisted_status.get('mode', '')} | "
            f"PID: {persisted_status.get('pid', '')} | "
            f"Last job: {persisted_status.get('last_job_id', '')} | "
            f"Updated: {persisted_status.get('updated_at', '')}"
        )
    _render_tool_adapter_plugins()
    worker_left, worker_right = st.columns(2)
    if worker_left.button("Start Background Worker", use_container_width=True):
        started = start_background_worker()
        if started:
            st.success("Background worker started.")
        else:
            st.info("Background worker is already running.")
        st.rerun()
    if worker_right.button("Stop Background Worker", use_container_width=True):
        stopped = stop_background_worker()
        if stopped:
            st.success("Background worker stopped.")
        else:
            st.info("Background worker was not running.")
        st.rerun()
    queue_items = store.load_queue(limit=20)
    if queue_items:
        with st.expander("Recent Queue Items"):
            st.dataframe(
                [
                    {
                        "job_id": item.get("job_id", ""),
                        "status": item.get("status", ""),
                        "created_at": item.get("created_at", ""),
                        "updated_at": item.get("updated_at", ""),
                        "user_input": item.get("user_input", ""),
                    }
                    for item in queue_items[::-1]
                ],
                use_container_width=True,
                hide_index=True,
            )

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

    with st.expander("Schedule a Task"):
        schedule_input = st.text_area("Scheduled Task Input", height=100, key="schedule_input")
        schedule_session_id = st.text_input("Session ID (optional)", value="", key="schedule_session_id")
        schedule_type = st.selectbox("Schedule Type", options=["once", "interval", "daily"])
        run_at = st.text_input(
            "Run At (ISO time for once)",
            value="",
            help="Example: 2026-03-04T21:30:00",
            key="schedule_run_at",
        )
        interval_seconds = st.number_input(
            "Interval Seconds",
            min_value=60,
            value=3600,
            step=60,
            key="schedule_interval_seconds",
        )
        time_of_day = st.text_input(
            "Daily Time (HH:MM)",
            value="09:00",
            key="schedule_time_of_day",
        )
        schedule_note = st.text_input("Note", value="", key="schedule_note")
        if st.button("Create Schedule", use_container_width=True):
            if not schedule_input.strip():
                st.warning("Enter a task before creating a schedule.")
            else:
                schedule = create_scheduled_task(
                    user_input=schedule_input,
                    session_id=schedule_session_id or None,
                    schedule_type=schedule_type,
                    run_at=run_at,
                    interval_seconds=int(interval_seconds),
                    time_of_day=time_of_day,
                    note=schedule_note,
                )
                st.success(
                    f"Created schedule {schedule.get('schedule_id', '')} for session {schedule.get('session_id', '')}"
                )
                st.rerun()

    if st.button("Run Next Queued Job", use_container_width=True):
        result = run_next_queued_task()
        if result is None:
            st.info("No queued jobs available.")
        elif result.get("success"):
            st.success(
                f"Executed {result.get('job_id', '')}: {result.get('output', 'Job completed')}"
            )
        elif str(result.get("status", "") or "") in WAITING_JOB_STATUSES:
            st.warning(
                f"Executed {result.get('job_id', '')}: "
                f"{result.get('output') or result.get('error') or result.get('status', 'waiting')}"
            )
        else:
            st.error(
                f"Executed {result.get('job_id', '')}: {result.get('error') or result.get('output') or 'Job failed'}"
            )

    schedules = get_scheduled_tasks(limit=100)
    if schedules:
        with st.expander("Scheduled Tasks"):
            st.dataframe(
                [
                    {
                        "schedule_id": item.get("schedule_id", ""),
                        "session_id": item.get("session_id", ""),
                        "type": item.get("schedule_type", ""),
                        "status": item.get("status", ""),
                        "next_run_at": item.get("next_run_at", ""),
                        "last_run_at": item.get("last_run_at", ""),
                        "last_job_id": item.get("last_job_id", ""),
                        "user_input": item.get("user_input", ""),
                    }
                    for item in schedules[::-1]
                ],
                use_container_width=True,
                hide_index=True,
            )
            schedule_options = {item.get("schedule_id", ""): item for item in schedules}
            selected_schedule_id = st.selectbox(
                "Manage Schedule",
                options=list(schedule_options.keys())[::-1],
            )
            selected_schedule = schedule_options[selected_schedule_id]
            left, mid, right = st.columns(3)
            if left.button("Pause Schedule", use_container_width=True):
                updated = pause_scheduled_task(selected_schedule_id)
                if updated:
                    st.success(f"Paused {selected_schedule_id}")
                st.rerun()
            if mid.button("Resume Schedule", use_container_width=True):
                updated = resume_scheduled_task(selected_schedule_id)
                if updated:
                    st.success(f"Resumed {selected_schedule_id}")
                st.rerun()
            if right.button("Delete Schedule", use_container_width=True):
                removed = delete_scheduled_task(selected_schedule_id)
                if removed:
                    st.success(f"Deleted {selected_schedule_id}")
                st.rerun()
            st.caption(
                f"Selected: {selected_schedule.get('schedule_type', '')} | "
                f"Next: {selected_schedule.get('next_run_at', '')} | "
                f"Status: {selected_schedule.get('status', '')}"
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

    st.subheader("User Preferences")
    preferences = get_user_preferences(selected_session_id)
    preferred_tools_text = ", ".join(preferences.get("preferred_tools", []) or [])
    preferred_sites_text = ", ".join(preferences.get("preferred_sites", []) or [])
    pref_left, pref_right = st.columns(2)
    default_output_directory = pref_left.text_input(
        "Default Output Directory",
        value=str(preferences.get("default_output_directory", "") or ""),
    )
    auto_queue_confirmations = pref_right.checkbox(
        "Auto-approve queued confirmations",
        value=bool(preferences.get("auto_queue_confirmations", False)),
    )
    user_location = st.text_input(
        "User Location",
        value=str(preferences.get("user_location", "") or ""),
        help="Example: Shanghai, China or San Francisco, CA",
    )
    preferred_tools_input = st.text_input(
        "Preferred Tools (comma separated)",
        value=preferred_tools_text,
    )
    preferred_sites_input = st.text_input(
        "Preferred Sites (comma separated)",
        value=preferred_sites_text,
    )
    if st.button("Save Preferences", use_container_width=True):
        update_user_preferences(
            {
                "default_output_directory": default_output_directory,
                "user_location": user_location,
                "auto_queue_confirmations": auto_queue_confirmations,
                "preferred_tools": [item.strip() for item in preferred_tools_input.split(",") if item.strip()],
                "preferred_sites": [item.strip() for item in preferred_sites_input.split(",") if item.strip()],
            },
            session_id=selected_session_id,
        )
        st.success("Preferences updated.")
        st.rerun()

    status_filter = st.selectbox(
        "Job Status Filter",
        options=[
            "all",
            "queued",
            "running",
            "waiting_for_approval",
            "waiting_for_event",
            "blocked",
            "completed",
            "completed_with_issues",
            "error",
            "cancelled",
        ],
        index=0,
    )
    jobs = store.load_jobs(
        session_id=selected_session_id,
        status=None if status_filter == "all" else status_filter,
        limit=100,
    )
    artifacts = store.load_artifacts(session_id=selected_session_id, limit=200)
    notifications = get_notification_feed(session_id=selected_session_id, limit=100)

    _render_notifications(notifications)
    notice_left, notice_right = st.columns(2)
    if notice_left.button("Mark All Notifications Read", use_container_width=True):
        count = mark_all_notifications_read(selected_session_id)
        st.success(f"Marked {count} notification(s) as read.")
        st.rerun()
    unread_notifications = [item for item in notifications if not bool(item.get("read", False))]
    if unread_notifications and notice_right.button("Mark Latest Unread Read", use_container_width=True):
        updated = mark_notification_read(str(unread_notifications[-1].get("notification_id", "")))
        if updated:
            st.success("Latest unread notification marked as read.")
        st.rerun()

    _render_policy_audit(jobs)

    st.subheader("Jobs")
    if jobs:
        job_rows = [
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
        ]
        st.dataframe(job_rows, use_container_width=True, hide_index=True)

        job_options = {item.get("job_id", ""): item for item in jobs}
        selected_job_id = st.selectbox(
            "Inspect Job",
            options=list(job_options.keys())[::-1],
        )
        selected_job = job_options[selected_job_id]

        st.subheader("Job Detail")
        detail_left, detail_right = st.columns(2)
        detail_left.metric("Status", selected_job.get("status", ""))
        detail_right.metric("Success", str(selected_job.get("success", "")))
        st.caption(
            f"Created: {selected_job.get('created_at', '')} | Updated: {selected_job.get('updated_at', '')}"
        )
        st.write(f"Input: {selected_job.get('user_input', '')}")
        if selected_job.get("output_preview"):
            st.write(f"Output Preview: {selected_job.get('output_preview', '')}")
        if selected_job.get("error"):
            st.write(f"Error: {selected_job.get('error', '')}")
        job_checkpoints = store.load_checkpoints(job_id=selected_job_id, limit=20)
        selected_checkpoint_id = ""
        if job_checkpoints:
            checkpoint_options = {
                str(item.get("checkpoint_id", "") or ""): item
                for item in job_checkpoints
                if isinstance(item, dict)
            }
            selected_checkpoint_id = st.selectbox(
                "Checkpoint For Resume",
                options=list(checkpoint_options.keys())[::-1],
                format_func=lambda key: (
                    f"{checkpoint_options[key].get('stage', '')} | "
                    f"{checkpoint_options[key].get('created_at', '')} | "
                    f"{key}"
                ),
            )

        action_left, action_mid, action_right = st.columns(3)
        if action_left.button("Rerun Job", key=f"rerun_{selected_job_id}", use_container_width=True):
            submission = rerun_job(selected_job_id)
            if submission:
                st.success(f"Queued rerun as {submission.get('job_id', '')}")
            else:
                st.error("Unable to rerun this job.")
        if action_mid.button("Resume Failed Job", key=f"resume_{selected_job_id}", use_container_width=True):
            submission = resume_failed_job(selected_job_id)
            if submission:
                st.success(f"Queued recovery as {submission.get('job_id', '')}")
            else:
                st.warning("This job is not in a resumable failed state.")
        if action_right.button("Resume From Checkpoint", key=f"resume_checkpoint_{selected_job_id}", use_container_width=True):
            resumed = resume_job_from_checkpoint(
                selected_job_id,
                checkpoint_id=selected_checkpoint_id or None,
            )
            if resumed:
                if resumed.get("success"):
                    st.success(
                        f"Resumed {resumed.get('job_id', '')}: {resumed.get('output', 'Job completed')}"
                    )
                else:
                    st.warning(
                        f"Resume ran for {resumed.get('job_id', '')}: "
                        f"{resumed.get('error') or resumed.get('output') or resumed.get('status', 'completed_with_issues')}"
                    )
            else:
                st.warning("No usable checkpoint available for this job.")

        if str(selected_job.get("status", "") or "") == WAITING_FOR_APPROVAL:
            approval_left, approval_right = st.columns(2)
            if approval_left.button("Approve Waiting Job", key=f"approve_{selected_job_id}", use_container_width=True):
                resumed = approve_waiting_job(selected_job_id)
                if resumed:
                    st.success(f"Approved and resumed {selected_job_id}")
                else:
                    st.warning("Unable to approve this waiting job.")
                st.rerun()
            if approval_right.button("Reject Waiting Job", key=f"reject_{selected_job_id}", use_container_width=True):
                rejected = reject_waiting_job(selected_job_id)
                if rejected:
                    st.success(f"Rejected {selected_job_id}")
                else:
                    st.warning("Unable to reject this waiting job.")
                st.rerun()

        _render_policy_decisions(selected_job.get("policy_decisions", []) or [])
        _render_checkpoints(job_checkpoints)

        selected_job_artifacts = store.load_artifacts(job_id=selected_job_id, limit=100)
        st.subheader("Artifacts For Selected Job")
        if selected_job_artifacts:
            st.dataframe(
                [
                    {
                        "artifact_id": item.get("artifact_id", ""),
                        "type": item.get("artifact_type", ""),
                        "name": item.get("name", ""),
                        "path": item.get("path", ""),
                        "preview": item.get("preview", ""),
                        "created_at": item.get("created_at", ""),
                    }
                    for item in selected_job_artifacts[::-1]
                ],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No artifacts for this job yet.")
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
