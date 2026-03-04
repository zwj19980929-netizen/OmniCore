"""
Work context management for goals, projects, todos, and reusable artifacts.
"""
from __future__ import annotations

import streamlit as st
from pathlib import Path
import sys

st.set_page_config(
    page_title="OmniCore - Workbench",
    page_icon="🧭",
    layout="wide",
)

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.runtime import (  # noqa: E402
    create_goal,
    create_template_from_job,
    create_work_template,
    create_directory_watch,
    create_project,
    create_todo,
    delete_directory_watch,
    delete_work_template,
    get_work_dashboard,
    list_directory_watch_events,
    list_directory_watches,
    list_work_templates,
    pause_directory_watch,
    resume_directory_watch,
    submit_template,
    update_todo_status,
)
from utils.runtime_state_store import get_runtime_state_store  # noqa: E402


def main() -> None:
    st.title("Workbench")

    sessions = get_runtime_state_store().load_sessions(limit=100)
    if not sessions:
        st.info("No sessions yet. Run at least one task first.")
        return

    session_options = {item["session_id"]: item for item in sessions}
    selected_session_id = st.selectbox(
        "Select Session",
        options=list(session_options.keys())[::-1],
    )
    dashboard = get_work_dashboard(selected_session_id)
    goals = dashboard.get("goals", [])
    projects = dashboard.get("projects", [])
    todos = dashboard.get("todos", [])
    artifacts = dashboard.get("artifacts", [])
    templates = list_work_templates(session_id=selected_session_id, limit=100)
    watches = list_directory_watches(session_id=selected_session_id, limit=100)
    watch_events = list_directory_watch_events(session_id=selected_session_id, limit=50)
    jobs = get_runtime_state_store().load_jobs(session_id=selected_session_id, limit=100)

    summary = dashboard.get("todo_summary", {}) or {}
    cols = st.columns(3)
    cols[0].metric("Pending Todos", summary.get("pending", 0))
    cols[1].metric("In Progress", summary.get("in_progress", 0))
    cols[2].metric("Done", summary.get("done", 0))

    with st.expander("Create Goal"):
        goal_title = st.text_input("Goal Title")
        goal_description = st.text_area("Goal Description", height=80)
        if st.button("Create Goal", use_container_width=True):
            if goal_title.strip():
                goal = create_goal(
                    session_id=selected_session_id,
                    title=goal_title,
                    description=goal_description,
                )
                st.success(f"Created {goal.get('goal_id', '')}")
                st.rerun()
            else:
                st.warning("Enter a goal title.")

    goal_map = {item.get("goal_id", ""): item for item in goals}
    project_map = {item.get("project_id", ""): item for item in projects}

    with st.expander("Create Project"):
        project_title = st.text_input("Project Title")
        project_description = st.text_area("Project Description", height=80)
        project_goal_id = st.selectbox(
            "Parent Goal (optional)",
            options=[""] + list(goal_map.keys())[::-1],
            format_func=lambda key: goal_map.get(key, {}).get("title", "") if key else "(none)",
        )
        if st.button("Create Project", use_container_width=True):
            if project_title.strip():
                project = create_project(
                    session_id=selected_session_id,
                    title=project_title,
                    goal_id=project_goal_id or None,
                    description=project_description,
                )
                st.success(f"Created {project.get('project_id', '')}")
                st.rerun()
            else:
                st.warning("Enter a project title.")

    with st.expander("Create Todo"):
        todo_title = st.text_input("Todo Title")
        todo_details = st.text_area("Todo Details", height=80)
        todo_goal_id = st.selectbox(
            "Goal (optional)",
            options=[""] + list(goal_map.keys())[::-1],
            format_func=lambda key: goal_map.get(key, {}).get("title", "") if key else "(none)",
            key="todo_goal_select",
        )
        todo_project_id = st.selectbox(
            "Project (optional)",
            options=[""] + list(project_map.keys())[::-1],
            format_func=lambda key: project_map.get(key, {}).get("title", "") if key else "(none)",
            key="todo_project_select",
        )
        if st.button("Create Todo", use_container_width=True):
            if todo_title.strip():
                todo = create_todo(
                    session_id=selected_session_id,
                    title=todo_title,
                    goal_id=todo_goal_id or None,
                    project_id=todo_project_id or None,
                    details=todo_details,
                )
                st.success(f"Created {todo.get('todo_id', '')}")
                st.rerun()
            else:
                st.warning("Enter a todo title.")

    with st.expander("Create Work Template"):
        template_name = st.text_input("Template Name")
        template_input = st.text_area("Template Prompt", height=100)
        template_notes = st.text_input("Template Notes")
        template_goal_id = st.selectbox(
            "Template Goal (optional)",
            options=[""] + list(goal_map.keys())[::-1],
            format_func=lambda key: goal_map.get(key, {}).get("title", "") if key else "(none)",
            key="template_goal_select",
        )
        template_project_id = st.selectbox(
            "Template Project (optional)",
            options=[""] + list(project_map.keys())[::-1],
            format_func=lambda key: project_map.get(key, {}).get("title", "") if key else "(none)",
            key="template_project_select",
        )
        if st.button("Create Template", use_container_width=True):
            if template_name.strip() and template_input.strip():
                created = create_work_template(
                    session_id=selected_session_id,
                    name=template_name,
                    user_input=template_input,
                    goal_id=template_goal_id or None,
                    project_id=template_project_id or None,
                    notes=template_notes,
                )
                st.success(f"Created {created.get('template_id', '')}")
                st.rerun()
            else:
                st.warning("Enter a template name and prompt.")

    if jobs:
        with st.expander("Save Successful Job As Template"):
            successful_jobs = [
                item for item in jobs
                if bool(item.get("success", False))
            ]
            if successful_jobs:
                job_map = {item.get("job_id", ""): item for item in successful_jobs}
                selected_job_id = st.selectbox(
                    "Successful Job",
                    options=list(job_map.keys())[::-1],
                    key="template_from_job_select",
                )
                saved_template_name = st.text_input("Template Name", key="template_from_job_name")
                if st.button("Save Job As Template", use_container_width=True):
                    if saved_template_name.strip():
                        created = create_template_from_job(selected_job_id, saved_template_name)
                        if created:
                            st.success(f"Created {created.get('template_id', '')}")
                            st.rerun()
                    else:
                        st.warning("Enter a template name.")
            else:
                st.info("No successful jobs available for templating yet.")

    with st.expander("Create Directory Watch"):
        watch_directory = st.text_input("Directory Path")
        template_map = {item.get("template_id", ""): item for item in templates}
        watch_template_id = st.selectbox(
            "Template (optional)",
            options=[""] + list(template_map.keys())[::-1],
            format_func=lambda key: template_map.get(key, {}).get("name", "") if key else "(custom prompt)",
        )
        watch_prompt = st.text_area("Fallback Prompt", height=80)
        watch_goal_id = st.selectbox(
            "Watch Goal (optional)",
            options=[""] + list(goal_map.keys())[::-1],
            format_func=lambda key: goal_map.get(key, {}).get("title", "") if key else "(none)",
            key="watch_goal_select",
        )
        watch_project_id = st.selectbox(
            "Watch Project (optional)",
            options=[""] + list(project_map.keys())[::-1],
            format_func=lambda key: project_map.get(key, {}).get("title", "") if key else "(none)",
            key="watch_project_select",
        )
        watch_todo_map = {item.get("todo_id", ""): item for item in todos}
        watch_todo_id = st.selectbox(
            "Watch Todo (optional)",
            options=[""] + list(watch_todo_map.keys())[::-1],
            format_func=lambda key: watch_todo_map.get(key, {}).get("title", "") if key else "(none)",
            key="watch_todo_select",
        )
        if st.button("Create Directory Watch", use_container_width=True):
            if watch_directory.strip() and (watch_template_id or watch_prompt.strip()):
                watch = create_directory_watch(
                    session_id=selected_session_id,
                    directory_path=watch_directory,
                    template_id=watch_template_id,
                    user_input=watch_prompt,
                    goal_id=watch_goal_id or None,
                    project_id=watch_project_id or None,
                    todo_id=watch_todo_id or None,
                )
                st.success(f"Created {watch.get('watch_id', '')}")
                st.rerun()
            else:
                st.warning("Enter a directory and choose a template or fallback prompt.")

    st.subheader("Goals")
    if goals:
        st.dataframe(
            [
                {
                    "goal_id": item.get("goal_id", ""),
                    "title": item.get("title", ""),
                    "status": item.get("status", ""),
                    "last_job_id": item.get("last_job_id", ""),
                    "updated_at": item.get("updated_at", ""),
                }
                for item in goals[::-1]
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No goals yet.")

    st.subheader("Projects")
    if projects:
        st.dataframe(
            [
                {
                    "project_id": item.get("project_id", ""),
                    "goal_id": item.get("goal_id", ""),
                    "title": item.get("title", ""),
                    "status": item.get("status", ""),
                    "last_job_id": item.get("last_job_id", ""),
                }
                for item in projects[::-1]
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No projects yet.")

    st.subheader("Todos")
    if todos:
        st.dataframe(
            [
                {
                    "todo_id": item.get("todo_id", ""),
                    "goal_id": item.get("goal_id", ""),
                    "project_id": item.get("project_id", ""),
                    "title": item.get("title", ""),
                    "status": item.get("status", ""),
                    "last_job_id": item.get("last_job_id", ""),
                }
                for item in todos[::-1]
            ],
            use_container_width=True,
            hide_index=True,
        )
        todo_options = {item.get("todo_id", ""): item for item in todos}
        selected_todo_id = st.selectbox(
            "Update Todo Status",
            options=list(todo_options.keys())[::-1],
        )
        new_status = st.selectbox("New Status", options=["pending", "in_progress", "done", "cancelled"])
        if st.button("Apply Todo Status", use_container_width=True):
            updated = update_todo_status(selected_todo_id, new_status)
            if updated:
                st.success(f"Updated {selected_todo_id} -> {new_status}")
                st.rerun()
    else:
        st.info("No todos yet.")

    st.subheader("Reusable Artifacts")
    if artifacts:
        st.dataframe(
            [
                {
                    "catalog_id": item.get("catalog_id", ""),
                    "type": item.get("artifact_type", ""),
                    "name": item.get("name", ""),
                    "path": item.get("path", ""),
                    "preview": item.get("preview", ""),
                    "job_id": item.get("job_id", ""),
                    "goal_id": item.get("goal_id", ""),
                    "project_id": item.get("project_id", ""),
                    "todo_id": item.get("todo_id", ""),
                }
                for item in artifacts[::-1]
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No cataloged artifacts yet.")

    st.subheader("Work Templates")
    if templates:
        st.dataframe(
            [
                {
                    "template_id": item.get("template_id", ""),
                    "name": item.get("name", ""),
                    "source_job_id": item.get("source_job_id", ""),
                    "goal_id": item.get("goal_id", ""),
                    "project_id": item.get("project_id", ""),
                }
                for item in templates[::-1]
            ],
            use_container_width=True,
            hide_index=True,
        )
        template_map = {item.get("template_id", ""): item for item in templates}
        selected_template_id = st.selectbox(
            "Run / Delete Template",
            options=list(template_map.keys())[::-1],
            key="template_manage_select",
        )
        left, right = st.columns(2)
        if left.button("Run Template", use_container_width=True):
            submission = submit_template(selected_template_id, session_id=selected_session_id)
            if submission:
                st.success(f"Queued {submission.get('job_id', '')}")
                st.rerun()
        if right.button("Delete Template", use_container_width=True):
            removed = delete_work_template(selected_template_id)
            if removed:
                st.success(f"Deleted {selected_template_id}")
                st.rerun()
    else:
        st.info("No templates yet.")

    st.subheader("Directory Watches")
    if watches:
        st.dataframe(
            [
                {
                    "watch_id": item.get("watch_id", ""),
                    "directory": item.get("directory_path", ""),
                    "status": item.get("status", ""),
                    "template_id": item.get("template_id", ""),
                    "last_event": item.get("last_event_path", ""),
                    "last_triggered_at": item.get("last_triggered_at", ""),
                }
                for item in watches[::-1]
            ],
            use_container_width=True,
            hide_index=True,
        )
        watch_map = {item.get("watch_id", ""): item for item in watches}
        selected_watch_id = st.selectbox(
            "Manage Directory Watch",
            options=list(watch_map.keys())[::-1],
        )
        left, mid, right = st.columns(3)
        if left.button("Pause Watch", use_container_width=True):
            pause_directory_watch(selected_watch_id)
            st.success(f"Paused {selected_watch_id}")
            st.rerun()
        if mid.button("Resume Watch", use_container_width=True):
            resume_directory_watch(selected_watch_id)
            st.success(f"Resumed {selected_watch_id}")
            st.rerun()
        if right.button("Delete Watch", use_container_width=True):
            delete_directory_watch(selected_watch_id)
            st.success(f"Deleted {selected_watch_id}")
            st.rerun()
    else:
        st.info("No directory watches yet.")

    st.subheader("Directory Watch Events")
    if watch_events:
        st.dataframe(
            [
                {
                    "time": item.get("created_at", ""),
                    "watch_id": item.get("watch_id", ""),
                    "file_path": item.get("file_path", ""),
                    "template_id": item.get("template_id", ""),
                }
                for item in watch_events[::-1]
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("No directory watch events yet.")


if __name__ == "__main__":
    main()
