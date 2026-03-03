"""
Persistence and lightweight analysis for runtime metrics snapshots.
"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import settings


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


def _safe_ratio_delta(current: Optional[float], baseline: Optional[float]) -> Optional[float]:
    if current is None or baseline is None:
        return None
    return current - baseline


def _parse_record_timestamp(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def diff_metrics(current: Any, previous: Any) -> Any:
    if isinstance(current, dict):
        previous_dict = previous if isinstance(previous, dict) else {}
        delta: Dict[str, Any] = {}
        for key, value in current.items():
            child = diff_metrics(value, previous_dict.get(key))
            if child not in ({}, None):
                delta[key] = child
        return delta
    if _is_number(current):
        baseline = previous if _is_number(previous) else 0
        return current - baseline
    return None


class RuntimeMetricsStore:
    def __init__(self, file_path: Optional[Path] = None, max_records: Optional[int] = None):
        self.file_path = Path(file_path) if file_path else settings.DATA_DIR / "runtime_metrics_history.jsonl"
        self.max_records = max_records or settings.RUNTIME_METRICS_HISTORY_LIMIT
        self._lock = threading.Lock()

    def _ensure_parent_dir(self) -> None:
        self.file_path.parent.mkdir(parents=True, exist_ok=True)

    def _read_records_locked(self) -> List[Dict[str, Any]]:
        if not self.file_path.exists():
            return []
        records: List[Dict[str, Any]] = []
        try:
            with self.file_path.open("r", encoding="utf-8") as handle:
                for raw_line in handle:
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict):
                        records.append(item)
        except FileNotFoundError:
            return []
        return records

    def _write_records_locked(self, records: List[Dict[str, Any]]) -> None:
        self._ensure_parent_dir()
        with self.file_path.open("w", encoding="utf-8") as handle:
            for item in records:
                handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True))
                handle.write("\n")

    def load_recent(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        with self._lock:
            records = self._read_records_locked()
        if limit is None:
            return records
        return records[-max(limit, 0):]

    def filter_records_by_date_range(
        self,
        records: Optional[List[Dict[str, Any]]] = None,
        *,
        start_date: Optional[date] = None,
        end_date: Optional[date] = None,
    ) -> List[Dict[str, Any]]:
        source_records = list(records) if records is not None else self.load_recent(limit=None)
        filtered: List[Dict[str, Any]] = []
        for item in source_records:
            timestamp = _parse_record_timestamp(item.get("timestamp"))
            if timestamp is None:
                continue
            record_date = timestamp.date()
            if start_date and record_date < start_date:
                continue
            if end_date and record_date > end_date:
                continue
            filtered.append(item)
        return filtered

    def append_record(
        self,
        *,
        user_input: str,
        success: bool,
        status: str,
        runtime_metrics: Dict[str, Any],
        is_special_command: bool = False,
    ) -> Dict[str, Any]:
        with self._lock:
            records = self._read_records_locked()
            previous_metrics = {}
            if records:
                previous_metrics = records[-1].get("runtime_metrics", {}) or {}

            runtime_delta = diff_metrics(runtime_metrics or {}, previous_metrics)
            record = {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "user_input": (user_input or "")[:120],
                "success": bool(success),
                "status": status or "",
                "is_special_command": bool(is_special_command),
                "runtime_metrics": runtime_metrics or {},
                "runtime_delta": runtime_delta or {},
            }
            records.append(record)
            if len(records) > self.max_records:
                records = records[-self.max_records:]
            self._write_records_locked(records)
            return record

    def summarize_recent(
        self,
        records: Optional[List[Dict[str, Any]]] = None,
        *,
        limit: int = 20,
    ) -> Dict[str, Any]:
        recent_records = records[-limit:] if records is not None else self.load_recent(limit=limit)
        totals = {
            "cache_hits": 0,
            "cache_misses": 0,
            "cache_sets": 0,
            "browser_reuse_hits": 0,
            "browser_launches": 0,
        }

        considered = 0
        for item in recent_records:
            if item.get("is_special_command"):
                continue
            considered += 1
            delta = item.get("runtime_delta", {}) or {}
            llm_cache = delta.get("llm_cache", {}) or {}
            browser_pool = delta.get("browser_pool", {}) or {}
            totals["cache_hits"] += int(llm_cache.get("hits", 0) or 0)
            totals["cache_misses"] += int(llm_cache.get("misses", 0) or 0)
            totals["cache_sets"] += int(llm_cache.get("sets", 0) or 0)
            totals["browser_reuse_hits"] += int(browser_pool.get("reuse_hits", 0) or 0)
            totals["browser_launches"] += int(browser_pool.get("launches", 0) or 0)

        hints: List[str] = []
        cache_lookups = totals["cache_hits"] + totals["cache_misses"]
        cache_hit_ratio = None
        if cache_lookups > 0:
            cache_hit_ratio = totals["cache_hits"] / cache_lookups
            if cache_hit_ratio < 0.35 and totals["cache_sets"] > 0:
                hints.append(
                    "LLM cache hit rate is low. Consider increasing URL_ANALYSIS_CACHE_TTL_SECONDS "
                    "or PAGE_ANALYSIS_CACHE_TTL_SECONDS, or check whether cache keys are too specific."
                )
            elif cache_hit_ratio > 0.8 and totals["cache_hits"] >= 10:
                hints.append(
                    "LLM cache reuse is strong. Current TTLs look effective; reduce them only if "
                    "memory pressure becomes noticeable."
                )

        browser_ops = totals["browser_reuse_hits"] + totals["browser_launches"]
        browser_reuse_ratio = None
        if browser_ops > 0:
            browser_reuse_ratio = totals["browser_reuse_hits"] / browser_ops
            if browser_reuse_ratio < 0.4 and totals["browser_launches"] >= 2:
                hints.append(
                    "Browser pool reuse is low. Consider increasing BROWSER_POOL_IDLE_TTL_SECONDS "
                    "if tasks tend to arrive in short bursts."
                )
            elif browser_reuse_ratio > 0.75 and totals["browser_reuse_hits"] >= 4:
                hints.append(
                    "Browser pool reuse is healthy. The current idle TTL is likely adequate."
                )

        if not hints:
            hints.append("Not enough recent signal to recommend a tuning change yet.")

        recommended_settings = self._recommend_settings(
            record_count=considered,
            totals=totals,
            cache_hit_ratio=cache_hit_ratio,
            browser_reuse_ratio=browser_reuse_ratio,
        )

        return {
            "record_count": considered,
            "cache_hit_ratio": cache_hit_ratio,
            "browser_reuse_ratio": browser_reuse_ratio,
            "totals": totals,
            "suggestions": hints,
            "recommended_settings": recommended_settings,
        }

    def compare_windows(
        self,
        records: Optional[List[Dict[str, Any]]] = None,
        *,
        recent_limit: int = 10,
        baseline_limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        recent_limit = max(int(recent_limit), 1)
        baseline_limit = max(int(baseline_limit or recent_limit), 1)
        source_records = list(records) if records is not None else self.load_recent(
            limit=recent_limit + baseline_limit + 20
        )
        normal_records = [item for item in source_records if not item.get("is_special_command")]

        recent_window = normal_records[-recent_limit:]
        baseline_window = normal_records[-(recent_limit + baseline_limit):-recent_limit] if len(normal_records) > recent_limit else []

        recent_summary = self.summarize_recent(records=recent_window, limit=max(len(recent_window), 1))
        baseline_summary = self.summarize_recent(records=baseline_window, limit=max(len(baseline_window), 1))

        return {
            "recent_window_size": len(recent_window),
            "baseline_window_size": len(baseline_window),
            "recent": recent_summary,
            "baseline": baseline_summary,
            "delta": {
                "cache_hit_ratio": _safe_ratio_delta(
                    recent_summary.get("cache_hit_ratio"),
                    baseline_summary.get("cache_hit_ratio"),
                ),
                "browser_reuse_ratio": _safe_ratio_delta(
                    recent_summary.get("browser_reuse_ratio"),
                    baseline_summary.get("browser_reuse_ratio"),
                ),
            },
        }

    def build_env_override_text(
        self,
        summary: Optional[Dict[str, Any]] = None,
        *,
        include_unchanged: bool = False,
    ) -> str:
        resolved_summary = summary or self.summarize_recent()
        recommendations = resolved_summary.get("recommended_settings", {}) or {}
        lines = ["# Suggested runtime tuning generated from recent metrics"]

        for name in sorted(recommendations):
            item = recommendations.get(name, {}) or {}
            if not include_unchanged and not item.get("changed"):
                continue
            lines.append(f"{name}={item.get('recommended')}")

        if len(lines) == 1:
            lines.append("# No runtime tuning overrides recommended yet.")

        return "\n".join(lines)

    def persist_env_overrides(
        self,
        summary: Optional[Dict[str, Any]] = None,
        *,
        file_path: Optional[Path] = None,
        include_unchanged: bool = False,
    ) -> Path:
        target_path = Path(file_path) if file_path else settings.RUNTIME_METRICS_OVERRIDE_PATH
        target_path.parent.mkdir(parents=True, exist_ok=True)
        override_text = self.build_env_override_text(
            summary,
            include_unchanged=include_unchanged,
        )
        target_path.write_text(override_text + "\n", encoding="utf-8")
        return target_path

    def _recommend_settings(
        self,
        *,
        record_count: int,
        totals: Dict[str, int],
        cache_hit_ratio: Optional[float],
        browser_reuse_ratio: Optional[float],
    ) -> Dict[str, Dict[str, Any]]:
        recommendations: Dict[str, Dict[str, Any]] = {}

        cache_lookups = totals["cache_hits"] + totals["cache_misses"]
        cache_ready = record_count > 0 and cache_lookups >= 8 and totals["cache_sets"] > 0

        url_ttl = settings.URL_ANALYSIS_CACHE_TTL_SECONDS
        page_ttl = settings.PAGE_ANALYSIS_CACHE_TTL_SECONDS
        cache_reason = "Not enough cache traffic yet to recommend a TTL change."
        if cache_ready and cache_hit_ratio is not None and cache_hit_ratio < 0.35:
            url_ttl = _clamp_int(
                max(settings.URL_ANALYSIS_CACHE_TTL_SECONDS * 2, settings.URL_ANALYSIS_CACHE_TTL_SECONDS + 300),
                60,
                7200,
            )
            page_ttl = _clamp_int(
                max(settings.PAGE_ANALYSIS_CACHE_TTL_SECONDS * 2, settings.PAGE_ANALYSIS_CACHE_TTL_SECONDS + 300),
                60,
                7200,
            )
            cache_reason = (
                "Low cache reuse across recent tasks suggests the URL/page analysis TTLs are"
                " too short for your current workload."
            )
        elif cache_ready and cache_hit_ratio is not None and cache_hit_ratio > 0.8 and totals["cache_hits"] >= 10:
            cache_reason = (
                "Cache reuse is healthy. Keep the current analysis TTLs unless you start seeing"
                " memory pressure."
            )

        recommendations["URL_ANALYSIS_CACHE_TTL_SECONDS"] = {
            "current": settings.URL_ANALYSIS_CACHE_TTL_SECONDS,
            "recommended": url_ttl,
            "changed": url_ttl != settings.URL_ANALYSIS_CACHE_TTL_SECONDS,
            "reason": cache_reason,
        }
        recommendations["PAGE_ANALYSIS_CACHE_TTL_SECONDS"] = {
            "current": settings.PAGE_ANALYSIS_CACHE_TTL_SECONDS,
            "recommended": page_ttl,
            "changed": page_ttl != settings.PAGE_ANALYSIS_CACHE_TTL_SECONDS,
            "reason": cache_reason,
        }

        browser_ops = totals["browser_reuse_hits"] + totals["browser_launches"]
        browser_ready = record_count > 0 and browser_ops > 0 and totals["browser_launches"] >= 2
        browser_idle_ttl = settings.BROWSER_POOL_IDLE_TTL_SECONDS
        browser_reason = "Not enough browser activity yet to recommend an idle TTL change."
        if (
            browser_ready
            and browser_reuse_ratio is not None
            and browser_reuse_ratio < 0.4
            and totals["browser_launches"] >= 2
        ):
            browser_idle_ttl = _clamp_int(
                max(settings.BROWSER_POOL_IDLE_TTL_SECONDS * 2, settings.BROWSER_POOL_IDLE_TTL_SECONDS + 30),
                15,
                900,
            )
            browser_reason = (
                "Browser reuse is low, so the pool is likely expiring instances before nearby"
                " tasks can reuse them."
            )
        elif (
            browser_ready
            and browser_reuse_ratio is not None
            and browser_reuse_ratio > 0.75
            and totals["browser_reuse_hits"] >= 4
        ):
            browser_reason = "Browser reuse is healthy. Keep the current idle TTL."

        recommendations["BROWSER_POOL_IDLE_TTL_SECONDS"] = {
            "current": settings.BROWSER_POOL_IDLE_TTL_SECONDS,
            "recommended": browser_idle_ttl,
            "changed": browser_idle_ttl != settings.BROWSER_POOL_IDLE_TTL_SECONDS,
            "reason": browser_reason,
        }

        return recommendations


_runtime_metrics_store: Optional[RuntimeMetricsStore] = None
_runtime_metrics_store_lock = threading.Lock()


def get_runtime_metrics_store() -> RuntimeMetricsStore:
    global _runtime_metrics_store
    if _runtime_metrics_store is None:
        with _runtime_metrics_store_lock:
            if _runtime_metrics_store is None:
                _runtime_metrics_store = RuntimeMetricsStore()
    return _runtime_metrics_store
