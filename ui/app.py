"""
OmniCore Streamlit 前端界面
简洁的 Web UI，支持任务输入和结果展示
"""
import streamlit as st
from datetime import datetime

# 页面配置
st.set_page_config(
    page_title="OmniCore - 智能体操作系统",
    page_icon="🚀",
    layout="wide",
    initial_sidebar_state="expanded",
)

# 导入核心模块（延迟导入避免循环依赖）
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.settings import settings
from core.statuses import WAITING_JOB_STATUSES
from utils.runtime_metrics_store import get_runtime_metrics_store


def _build_metric_trend_rows(task_history, limit=5):
    rows = []
    for item in task_history[-limit:]:
        if item.get("is_special_command"):
            continue
        delta = item.get("runtime_delta", {}) or {}
        llm_cache = delta.get("llm_cache", {}) or {}
        browser_pool = delta.get("browser_pool", {}) or {}
        rows.append({
            "time": item.get("timestamp", ""),
            "ok": "Y" if item.get("success") else "N",
            "cache_hits": llm_cache.get("hits", 0),
            "cache_miss": llm_cache.get("misses", 0),
            "cache_sets": llm_cache.get("sets", 0),
            "reuse_hits": browser_pool.get("reuse_hits", 0),
            "launches": browser_pool.get("launches", 0),
        })
    return rows


def init_session_state():
    """初始化会话状态"""
    metrics_store = get_runtime_metrics_store()
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "task_history" not in st.session_state:
        st.session_state.task_history = metrics_store.load_recent(limit=20)
    if "conversation_history" not in st.session_state:
        st.session_state.conversation_history = []
    if "agent_session_id" not in st.session_state:
        st.session_state.agent_session_id = ""
    if "current_goal_id" not in st.session_state:
        st.session_state.current_goal_id = ""
    if "current_project_id" not in st.session_state:
        st.session_state.current_project_id = ""
    if "current_todo_id" not in st.session_state:
        st.session_state.current_todo_id = ""
    if "memory_initialized" not in st.session_state:
        st.session_state.memory_initialized = False
    if "last_runtime_metrics" not in st.session_state:
        if st.session_state.task_history:
            last_record = st.session_state.task_history[-1]
            st.session_state.last_runtime_metrics = last_record.get("runtime_metrics", {}) or {}
        else:
            st.session_state.last_runtime_metrics = {}


def render_sidebar():
    """渲染侧边栏"""
    with st.sidebar:
        st.title("🚀 OmniCore")
        st.caption("全栈智能体操作系统核心 v0.1.0")

        st.divider()

        # 快捷任务
        st.subheader("⚡ 快捷任务")

        if st.button("🔍 搜索示例任务", use_container_width=True):
            return "搜索最新的 AI 进展，整理前 5 条结果的标题和链接"

        if st.button("📊 系统状态", use_container_width=True):
            return "memory stats"

        st.divider()

        # 设置
        st.subheader("⚙️ 设置")

        model = st.selectbox(
            "选择模型",
            ["gpt-4o", "gpt-4o-mini", "claude-3-opus", "claude-3-sonnet"],
            index=0,
        )

        require_confirm = st.checkbox("高危操作需确认", value=True)

        if st.session_state.agent_session_id:
            st.divider()
            st.subheader("🧭 Work Context")
            try:
                from core.runtime import get_work_dashboard

                dashboard = get_work_dashboard(st.session_state.agent_session_id)
                goals = dashboard.get("goals", []) or []
                projects = dashboard.get("projects", []) or []
                todos = dashboard.get("todos", []) or []
                goal_map = {item.get("goal_id", ""): item for item in goals if isinstance(item, dict)}
                project_map = {item.get("project_id", ""): item for item in projects if isinstance(item, dict)}
                todo_map = {item.get("todo_id", ""): item for item in todos if isinstance(item, dict)}

                st.session_state.current_goal_id = st.selectbox(
                    "Goal",
                    options=[""] + list(goal_map.keys())[::-1],
                    index=0 if st.session_state.current_goal_id not in goal_map else ([""] + list(goal_map.keys())[::-1]).index(st.session_state.current_goal_id),
                    format_func=lambda key: goal_map.get(key, {}).get("title", "") if key else "(none)",
                )
                st.session_state.current_project_id = st.selectbox(
                    "Project",
                    options=[""] + list(project_map.keys())[::-1],
                    index=0 if st.session_state.current_project_id not in project_map else ([""] + list(project_map.keys())[::-1]).index(st.session_state.current_project_id),
                    format_func=lambda key: project_map.get(key, {}).get("title", "") if key else "(none)",
                )
                st.session_state.current_todo_id = st.selectbox(
                    "Todo",
                    options=[""] + list(todo_map.keys())[::-1],
                    index=0 if st.session_state.current_todo_id not in todo_map else ([""] + list(todo_map.keys())[::-1]).index(st.session_state.current_todo_id),
                    format_func=lambda key: todo_map.get(key, {}).get("title", "") if key else "(none)",
                )
            except Exception as e:
                st.caption(f"工作上下文加载失败: {e}")

        if settings.DEBUG_MODE and st.session_state.last_runtime_metrics:
            st.divider()
            st.subheader("Debug Metrics")
            st.json(st.session_state.last_runtime_metrics)
            trend_rows = _build_metric_trend_rows(st.session_state.task_history)
            if trend_rows:
                st.caption("Recent Task Deltas")
                st.table(trend_rows)
            summary = get_runtime_metrics_store().summarize_recent(
                records=st.session_state.task_history,
                limit=10,
            )
            if summary.get("record_count"):
                cache_ratio = summary.get("cache_hit_ratio")
                browser_ratio = summary.get("browser_reuse_ratio")
                recommended_settings = summary.get("recommended_settings", {}) or {}
                left, right = st.columns(2)
                left.metric(
                    "Cache Hit Rate",
                    f"{cache_ratio:.0%}" if cache_ratio is not None else "n/a",
                )
                right.metric(
                    "Browser Reuse",
                    f"{browser_ratio:.0%}" if browser_ratio is not None else "n/a",
                )
                st.caption("Tuning Hints")
                for hint in summary.get("suggestions", [])[:2]:
                    st.write(f"- {hint}")
                changed_settings = [
                    name
                    for name, item in recommended_settings.items()
                    if isinstance(item, dict) and item.get("changed")
                ]
                if changed_settings:
                    st.caption("Recommended Defaults")
                    st.write(", ".join(changed_settings))
                st.caption("Open the Runtime Metrics page for the full history and tuning view.")

        st.divider()

        # 记忆统计
        st.subheader("🧠 记忆系统")
        try:
            from memory.manager import build_memory_scope
            from memory.scoped_chroma_store import ChromaMemory
            if not st.session_state.memory_initialized:
                st.session_state.memory = ChromaMemory()
                st.session_state.memory_initialized = True
            scope = build_memory_scope(session_id=st.session_state.agent_session_id or "")
            total_stats = st.session_state.memory.get_stats()

            stats = st.session_state.memory.get_stats(scope=scope)
            st.metric("历史记录", f"{stats['total_memories']} 条")

            if st.button("🗑️ 清空记忆", use_container_width=True):
                cleared = st.session_state.memory.clear_scope(scope)
                st.success("记忆已清空")
                st.rerun()
        except Exception as e:
            st.warning(f"记忆系统未就绪: {e}")

        if st.session_state.agent_session_id:
            st.divider()
            st.subheader("🔔 通知")
            try:
                from core.runtime import get_notification_feed

                notifications = get_notification_feed(
                    session_id=st.session_state.agent_session_id,
                    unread_only=True,
                    limit=5,
                )
                if notifications:
                    for item in notifications[::-1]:
                        prefix = "⚠️" if item.get("requires_action") else "ℹ️"
                        st.caption(
                            f"{prefix} {item.get('title', '')}: {item.get('message', '')}"
                        )
                else:
                    st.caption("暂无未读通知")
            except Exception as e:
                st.caption(f"通知加载失败: {e}")

    return None


def render_chat_message(role: str, content: str, timestamp: str = None):
    """渲染聊天消息"""
    with st.chat_message(role):
        st.markdown(content)
        if timestamp:
            st.caption(timestamp)


def execute_task(user_input: str) -> dict:
    """执行任务"""
    from core.runtime import run_task

    metrics_store = get_runtime_metrics_store()
    memory = st.session_state.memory if st.session_state.memory_initialized else None
    result = run_task(
        user_input,
        memory=memory,
        conversation_history=st.session_state.conversation_history,
        session_id=st.session_state.agent_session_id or None,
        goal_id=st.session_state.current_goal_id or None,
        project_id=st.session_state.current_project_id or None,
        todo_id=st.session_state.current_todo_id or None,
    )
    current_metrics = result.get("runtime_metrics", {}) or {}
    if result.get("session_id"):
        st.session_state.agent_session_id = result.get("session_id")

    if not result.get("is_special_command"):
        turn_record = {
            "user_input": user_input,
            "success": result.get("success", False),
            "output": (result.get("output") or result.get("error") or "")[:300],
        }
        st.session_state.conversation_history.append(turn_record)
        if len(st.session_state.conversation_history) > 5:
            st.session_state.conversation_history.pop(0)

    if current_metrics:
        st.session_state.last_runtime_metrics = current_metrics
    st.session_state.task_history = metrics_store.load_recent(limit=20)

    return result


def main():
    """主函数"""
    init_session_state()

    # 渲染侧边栏（可能返回快捷任务）
    quick_task = render_sidebar()

    # 主界面标题
    st.title("🎯 OmniCore 任务中心")

    # 显示历史消息
    for msg in st.session_state.messages:
        render_chat_message(msg["role"], msg["content"], msg.get("timestamp"))

    # 处理快捷任务
    if quick_task:
        st.session_state.pending_input = quick_task

    # 多模态文件上传区
    uploaded_file = st.file_uploader(
        "附件（图片/文档/语音）",
        type=["png", "jpg", "jpeg", "webp", "gif", "pdf", "docx", "txt", "md", "csv",
              "mp3", "wav", "m4a", "ogg"],
        label_visibility="collapsed",
        key="multimodal_upload",
    )

    # 用户输入
    user_input = st.chat_input("输入你的指令...")

    # 如果有待处理的快捷任务
    if hasattr(st.session_state, "pending_input") and st.session_state.pending_input:
        user_input = st.session_state.pending_input
        st.session_state.pending_input = None

    # 有上传文件时也触发提交（即使无文字输入）
    if uploaded_file and not user_input:
        user_input = f"(附件: {uploaded_file.name})"

    if user_input:
        timestamp = datetime.now().strftime("%H:%M:%S")

        # 多模态文件预处理：将附件转换为文本后注入 user_input
        if uploaded_file:
            import tempfile
            import os as _os
            from utils.multimodal_input import (
                MultimodalInputProcessor,
                build_multimodal_input,
            )

            with tempfile.NamedTemporaryFile(
                suffix=_os.path.splitext(uploaded_file.name)[1],
                delete=False,
            ) as tmp:
                tmp.write(uploaded_file.read())
                tmp_path = tmp.name

            try:
                text_hint = user_input if user_input != f"(附件: {uploaded_file.name})" else ""
                inp = build_multimodal_input(tmp_path, text_hint)
                if inp:
                    processor = MultimodalInputProcessor()
                    processed = processor.process(inp)
                    if processed:
                        user_input = processed
            finally:
                try:
                    _os.unlink(tmp_path)
                except OSError:
                    pass

        # 添加用户消息
        st.session_state.messages.append({
            "role": "user",
            "content": user_input,
            "timestamp": timestamp,
        })
        render_chat_message("user", user_input, timestamp)

        # 执行任务
        with st.chat_message("assistant"):
            with st.spinner("🔄 正在处理..."):
                try:
                    result = execute_task(user_input)
                    status = str(result.get("status", "") or "")

                    if result["success"]:
                        response = f"✅ {result.get('output', '任务完成')}"
                        st.success("任务执行成功")
                    elif status in WAITING_JOB_STATUSES:
                        detail = result.get("output") or result.get("error") or f"状态: {status}"
                        response = f"⏸️ {detail}"
                        st.warning(f"任务进入等待状态: {status}")
                    else:
                        detail = result.get("error") or result.get("output") or f"状态: {status}"
                        response = f"❌ {detail}"
                        st.error("任务执行失败")

                    st.markdown(response)

                    # 显示任务详情
                    if result.get("tasks"):
                        with st.expander("📋 任务详情"):
                            for task in result["tasks"]:
                                task_status = str(task.get("status", "") or "")
                                if task_status == "completed":
                                    status_icon = "✅"
                                elif task_status in WAITING_JOB_STATUSES:
                                    status_icon = "⏸️"
                                elif task_status in {"pending", "running"}:
                                    status_icon = "…"
                                else:
                                    status_icon = "❌"
                                st.write(f"{status_icon} {task['description']}")

                    if result.get("delivery_package"):
                        with st.expander("Delivery Package"):
                            delivery = result.get("delivery_package", {}) or {}
                            st.write(delivery.get("headline", ""))
                            goal = delivery.get("goal", {}) or {}
                            project = delivery.get("project", {}) or {}
                            todo = delivery.get("todo", {}) or {}
                            if goal.get("title") or project.get("title") or todo.get("title"):
                                st.caption(
                                    "Work Context: "
                                    f"{goal.get('title', '')} / "
                                    f"{project.get('title', '')} / "
                                    f"{todo.get('title', '')}"
                                )
                            summary_cols = st.columns(3)
                            summary_cols[0].metric("Completed", delivery.get("completed_task_count", 0))
                            summary_cols[1].metric("Planned", delivery.get("total_task_count", 0))
                            summary_cols[2].metric("Review", str(delivery.get("review_status", "")))
                            if delivery.get("completed_tasks"):
                                st.caption("Completed Work")
                                st.dataframe(
                                    [{"task": item} for item in delivery.get("completed_tasks", [])],
                                    use_container_width=True,
                                    hide_index=True,
                                )
                            if delivery.get("deliverables"):
                                st.caption("Deliverables")
                                st.dataframe(
                                    [
                                        {
                                            "type": item.get("artifact_type", ""),
                                            "name": item.get("name", ""),
                                            "location": item.get("location", ""),
                                        }
                                        for item in delivery.get("deliverables", [])
                                        if isinstance(item, dict)
                                    ],
                                    use_container_width=True,
                                    hide_index=True,
                                )
                            if delivery.get("issues"):
                                st.caption("Open Issues")
                                st.dataframe(
                                    [
                                        {
                                            "task": item.get("description", ""),
                                            "issue": item.get("error", ""),
                                        }
                                        for item in delivery.get("issues", [])
                                        if isinstance(item, dict)
                                    ],
                                    use_container_width=True,
                                    hide_index=True,
                                )
                            if delivery.get("recommended_next_step"):
                                st.caption(f"Next: {delivery.get('recommended_next_step', '')}")
                            if delivery.get("open_todos"):
                                st.caption("Pending Work")
                                st.dataframe(
                                    [
                                        {
                                            "todo": item.get("title", ""),
                                            "status": item.get("status", ""),
                                        }
                                        for item in delivery.get("open_todos", [])
                                        if isinstance(item, dict)
                                    ],
                                    use_container_width=True,
                                    hide_index=True,
                                )

                    if result.get("artifacts"):
                        with st.expander("Artifacts"):
                            for artifact in result["artifacts"]:
                                path_value = artifact.get("path", "")
                                label = artifact.get("name") or path_value
                                if path_value:
                                    st.write(f"- {label}: `{path_value}`")
                                else:
                                    preview = artifact.get("preview", "")
                                    st.write(f"- {label}: {preview}")

                    if result.get("policy_decisions"):
                        with st.expander("Policy Decisions"):
                            st.dataframe(
                                [
                                    {
                                        "task_id": item.get("task_id", ""),
                                        "tool_name": item.get("tool_name", ""),
                                        "decision": item.get("decision", ""),
                                        "risk_level": item.get("risk_level", ""),
                                        "target_resource": item.get("target_resource", ""),
                                        "approved_by": item.get("approved_by", ""),
                                    }
                                    for item in result["policy_decisions"]
                                    if isinstance(item, dict)
                                ],
                                use_container_width=True,
                                hide_index=True,
                            )

                    if settings.DEBUG_MODE and result.get("runtime_metrics"):
                        with st.expander("Debug Metrics"):
                            if result.get("session_id") or result.get("job_id"):
                                st.caption(
                                    f"Session: {result.get('session_id', '')} | Job: {result.get('job_id', '')}"
                                )
                            st.json(result["runtime_metrics"])
                            if result.get("runtime_delta"):
                                st.caption("Current Task Delta")
                                st.json(result["runtime_delta"])

                except Exception as e:
                    response = f"❌ 执行出错: {str(e)}"
                    st.error(response)

        # 保存助手回复
        st.session_state.messages.append({
            "role": "assistant",
            "content": response,
            "timestamp": datetime.now().strftime("%H:%M:%S"),
        })


if __name__ == "__main__":
    main()
