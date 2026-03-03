"""
Dedicated Streamlit page for persisted runtime metrics and tuning guidance.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, List

import streamlit as st


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.settings import settings
from core.llm_cache import get_llm_cache
from utils.browser_runtime_pool import snapshot_browser_runtime_metrics
from utils.runtime_metrics_store import get_runtime_metrics_store


st.set_page_config(
    page_title="Runtime Metrics",
    layout="wide",
)


def _build_delta_rows(records: List[Dict[str, Any]], limit: int = 30) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for item in records[-limit:]:
        delta = item.get("runtime_delta", {}) or {}
        llm_cache = delta.get("llm_cache", {}) or {}
        browser_pool = delta.get("browser_pool", {}) or {}
        rows.append(
            {
                "timestamp": item.get("timestamp", ""),
                "task": item.get("user_input", ""),
                "status": item.get("status", ""),
                "success": bool(item.get("success")),
                "special": bool(item.get("is_special_command")),
                "cache_hits": int(llm_cache.get("hits", 0) or 0),
                "cache_misses": int(llm_cache.get("misses", 0) or 0),
                "cache_sets": int(llm_cache.get("sets", 0) or 0),
                "browser_reuse_hits": int(browser_pool.get("reuse_hits", 0) or 0),
                "browser_launches": int(browser_pool.get("launches", 0) or 0),
            }
        )
    return rows


def _build_recommendation_rows(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    recommendations = summary.get("recommended_settings", {}) or {}
    for name, item in recommendations.items():
        if not isinstance(item, dict):
            continue
        rows.append(
            {
                "setting": name,
                "current": item.get("current"),
                "recommended": item.get("recommended"),
                "action": "change" if item.get("changed") else "keep",
                "reason": item.get("reason", ""),
            }
        )
    return rows


def _format_ratio(value: Any) -> str:
    if value is None:
        return "n/a"
    return f"{float(value):.0%}"


def _build_window_compare_rows(comparison: Dict[str, Any]) -> List[Dict[str, Any]]:
    recent = comparison.get("recent", {}) or {}
    baseline = comparison.get("baseline", {}) or {}
    delta = comparison.get("delta", {}) or {}
    return [
        {
            "metric": "Cache Hit Rate",
            "baseline": _format_ratio(baseline.get("cache_hit_ratio")),
            "recent": _format_ratio(recent.get("cache_hit_ratio")),
            "delta": _format_ratio(delta.get("cache_hit_ratio")),
        },
        {
            "metric": "Browser Reuse",
            "baseline": _format_ratio(baseline.get("browser_reuse_ratio")),
            "recent": _format_ratio(recent.get("browser_reuse_ratio")),
            "delta": _format_ratio(delta.get("browser_reuse_ratio")),
        },
    ]


def main() -> None:
    st.title("Runtime Metrics")
    st.caption("Persisted cache and browser-pool telemetry for tuning the local agent runtime.")

    store = get_runtime_metrics_store()
    records = store.load_recent(limit=settings.RUNTIME_METRICS_HISTORY_LIMIT)
    summary = store.summarize_recent(records=records, limit=min(20, max(len(records), 1)))
    comparison_window = st.selectbox(
        "Comparison Window",
        options=[5, 10, 20],
        index=1,
        help="Compare the most recent N non-special tasks against the N tasks before them.",
    )
    comparison = store.compare_windows(
        records=records,
        recent_limit=comparison_window,
        baseline_limit=comparison_window,
    )
    env_override_text = store.build_env_override_text(summary)

    top_left, top_mid, top_right = st.columns(3)
    top_left.metric("Recent Tasks", summary.get("record_count", 0))

    cache_ratio = summary.get("cache_hit_ratio")
    top_mid.metric(
        "Cache Hit Rate",
        f"{cache_ratio:.0%}" if cache_ratio is not None else "n/a",
    )

    browser_ratio = summary.get("browser_reuse_ratio")
    top_right.metric(
        "Browser Reuse",
        f"{browser_ratio:.0%}" if browser_ratio is not None else "n/a",
    )

    recommendation_rows = _build_recommendation_rows(summary)
    st.subheader("Recommended Defaults")
    if recommendation_rows:
        st.dataframe(recommendation_rows, use_container_width=True)
    else:
        st.info("No tuning recommendation is available yet.")

    st.subheader("Export .env Overrides")
    st.code(env_override_text, language="dotenv")
    changed_setting_count = sum(1 for row in recommendation_rows if row.get("action") == "change")
    if st.button(
        "Apply recommended overrides",
        disabled=changed_setting_count == 0,
        use_container_width=True,
    ):
        target_path = store.persist_env_overrides(summary)
        st.success(
            f"Saved overrides to {target_path}. Restart the app or runtime process to load the new values."
        )
    st.download_button(
        "Download overrides",
        data=env_override_text + "\n",
        file_name="runtime-metrics-overrides.env",
        mime="text/plain",
        use_container_width=True,
    )

    st.subheader("Tuning Notes")
    for hint in summary.get("suggestions", []):
        st.write(f"- {hint}")

    st.subheader("Window Comparison")
    compare_rows = _build_window_compare_rows(comparison)
    st.caption(
        f"Comparing the latest {comparison.get('recent_window_size', 0)} non-special tasks "
        f"against the previous {comparison.get('baseline_window_size', 0)}."
    )
    st.table(compare_rows)

    st.subheader("Date Range View")
    if records:
        valid_dates: List[date] = []
        for item in records:
            raw_timestamp = item.get("timestamp")
            if not raw_timestamp:
                continue
            try:
                valid_dates.append(date.fromisoformat(str(raw_timestamp)[:10]))
            except ValueError:
                continue

        if not valid_dates:
            filtered_records = []
            st.info("No dated runtime metrics are available yet.")
        else:
            first_day = min(valid_dates)
            last_day = max(valid_dates)
            left, right = st.columns(2)
            start_date = left.date_input(
                "Start Date",
                value=first_day,
                min_value=first_day,
                max_value=last_day,
            )
            end_date = right.date_input(
                "End Date",
                value=last_day,
                min_value=first_day,
                max_value=last_day,
            )
            if start_date > end_date:
                st.warning("Start date must be earlier than or equal to end date.")
                filtered_records = []
            else:
                filtered_records = store.filter_records_by_date_range(
                    records=records,
                    start_date=start_date,
                    end_date=end_date,
                )

        filtered_summary = store.summarize_recent(
            records=filtered_records,
            limit=max(len(filtered_records), 1),
        )
        metric_left, metric_mid, metric_right = st.columns(3)
        metric_left.metric("Tasks In Range", filtered_summary.get("record_count", 0))
        metric_mid.metric("Range Cache Hit", _format_ratio(filtered_summary.get("cache_hit_ratio")))
        metric_right.metric("Range Browser Reuse", _format_ratio(filtered_summary.get("browser_reuse_ratio")))

        filtered_delta_rows = _build_delta_rows(filtered_records)
        if filtered_delta_rows:
            st.dataframe(filtered_delta_rows, use_container_width=True)
        else:
            st.info("No runtime metrics matched the selected date range.")
    else:
        st.info("No runtime metrics have been persisted yet.")

    st.subheader("Live Runtime State")
    live_left, live_right = st.columns(2)
    live_left.caption("Browser Pool")
    live_left.json(snapshot_browser_runtime_metrics())

    llm_cache = get_llm_cache()
    live_cache_stats = llm_cache.snapshot_stats()
    live_right.caption("LLM Cache")
    live_right.json(live_cache_stats)

    st.subheader("LLM Cache Ops")
    known_namespaces = sorted(
        {
            *live_cache_stats.get("namespace_limits", {}).keys(),
            *live_cache_stats.get("namespace_sizes", {}).keys(),
        }
    )
    if not known_namespaces:
        known_namespaces = ["url_analysis", "page_structure_analysis"]

    selected_namespace = st.selectbox(
        "Namespace",
        options=known_namespaces,
        index=0,
        help="Choose the cache namespace to inspect or clear.",
    )
    prefix_left, prefix_right = st.columns(2)
    url_prefix = prefix_left.text_input(
        "URL Prefix",
        value="",
        help="Clear cached entries whose normalized URL starts with this prefix.",
    )
    prompt_version = prefix_right.text_input(
        "Prompt Version",
        value="",
        help="Clear cached entries created by a specific prompt version.",
    )
    ops_left, ops_mid, ops_right, ops_last = st.columns(4)
    if ops_left.button("Clear Namespace", use_container_width=True):
        removed = llm_cache.clear_namespace(selected_namespace)
        st.success(f"Removed {removed} cache entries from {selected_namespace}.")
    if ops_mid.button("Clear URL Prefix", use_container_width=True):
        removed = llm_cache.clear_by_url_prefix(selected_namespace, url_prefix)
        st.success(f"Removed {removed} cache entries by URL prefix.")
    if ops_right.button("Clear Prompt Version", use_container_width=True):
        removed = llm_cache.clear_by_prompt_version(selected_namespace, prompt_version)
        st.success(f"Removed {removed} cache entries by prompt version.")
    if ops_last.button("Clear All Cache", use_container_width=True):
        removed = llm_cache.clear()
        st.success(f"Removed {removed} cache entries from all namespaces.")

    if settings.DEBUG_MODE and records:
        with st.expander("Latest Snapshot"):
            st.json(records[-1])


if __name__ == "__main__":
    main()
