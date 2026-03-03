from pathlib import Path
from datetime import date

from config.settings import settings
from utils.runtime_metrics_store import RuntimeMetricsStore


def _make_store_file(name: str) -> Path:
    path = settings.DATA_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return path


def test_append_record_persists_history_and_computes_delta():
    store = RuntimeMetricsStore(
        file_path=_make_store_file("test_runtime_metrics_store_unit_a.jsonl"),
        max_records=2,
    )
    first = store.append_record(
        user_input="task one",
        success=True,
        status="completed",
        runtime_metrics={
            "llm_cache": {"hits": 1, "misses": 2, "sets": 2},
            "browser_pool": {"reuse_hits": 0, "launches": 1},
        },
    )
    second = store.append_record(
        user_input="task two",
        success=True,
        status="completed",
        runtime_metrics={
            "llm_cache": {"hits": 4, "misses": 3, "sets": 4},
            "browser_pool": {"reuse_hits": 2, "launches": 1},
        },
    )
    third = store.append_record(
        user_input="task three",
        success=False,
        status="error",
        runtime_metrics={
            "llm_cache": {"hits": 5, "misses": 5, "sets": 5},
            "browser_pool": {"reuse_hits": 3, "launches": 2},
        },
    )

    recent = store.load_recent(limit=10)

    assert first["runtime_delta"]["llm_cache"]["hits"] == 1
    assert second["runtime_delta"]["llm_cache"]["hits"] == 3
    assert third["runtime_delta"]["browser_pool"]["launches"] == 1
    assert len(recent) == 2
    assert recent[0]["user_input"] == "task two"
    assert recent[1]["user_input"] == "task three"


def test_summarize_recent_returns_tuning_hints():
    store = RuntimeMetricsStore(
        file_path=_make_store_file("test_runtime_metrics_store_unit_b.jsonl"),
        max_records=5,
    )
    store.append_record(
        user_input="first",
        success=True,
        status="completed",
        runtime_metrics={
            "llm_cache": {"hits": 0, "misses": 3, "sets": 3},
            "browser_pool": {"reuse_hits": 0, "launches": 1},
        },
    )
    store.append_record(
        user_input="second",
        success=True,
        status="completed",
        runtime_metrics={
            "llm_cache": {"hits": 1, "misses": 8, "sets": 6},
            "browser_pool": {"reuse_hits": 0, "launches": 3},
        },
    )

    summary = store.summarize_recent(limit=5)

    assert summary["record_count"] == 2
    assert summary["cache_hit_ratio"] is not None
    assert summary["browser_reuse_ratio"] is not None
    assert any("LLM cache hit rate is low" in hint for hint in summary["suggestions"])
    assert any("Browser pool reuse is low" in hint for hint in summary["suggestions"])
    assert summary["recommended_settings"]["URL_ANALYSIS_CACHE_TTL_SECONDS"]["recommended"] > settings.URL_ANALYSIS_CACHE_TTL_SECONDS
    assert summary["recommended_settings"]["PAGE_ANALYSIS_CACHE_TTL_SECONDS"]["recommended"] > settings.PAGE_ANALYSIS_CACHE_TTL_SECONDS
    assert summary["recommended_settings"]["BROWSER_POOL_IDLE_TTL_SECONDS"]["recommended"] > settings.BROWSER_POOL_IDLE_TTL_SECONDS


def test_summarize_recent_keeps_current_defaults_when_reuse_is_healthy():
    store = RuntimeMetricsStore(
        file_path=_make_store_file("test_runtime_metrics_store_unit_c.jsonl"),
        max_records=5,
    )
    store.append_record(
        user_input="first",
        success=True,
        status="completed",
        runtime_metrics={
            "llm_cache": {"hits": 8, "misses": 1, "sets": 2},
            "browser_pool": {"reuse_hits": 3, "launches": 1},
        },
    )
    store.append_record(
        user_input="second",
        success=True,
        status="completed",
        runtime_metrics={
            "llm_cache": {"hits": 14, "misses": 2, "sets": 3},
            "browser_pool": {"reuse_hits": 7, "launches": 1},
        },
    )

    summary = store.summarize_recent(limit=5)

    assert summary["cache_hit_ratio"] is not None
    assert summary["cache_hit_ratio"] > 0.8
    assert summary["browser_reuse_ratio"] is not None
    assert summary["browser_reuse_ratio"] > 0.75
    assert summary["recommended_settings"]["URL_ANALYSIS_CACHE_TTL_SECONDS"]["changed"] is False
    assert summary["recommended_settings"]["PAGE_ANALYSIS_CACHE_TTL_SECONDS"]["changed"] is False
    assert summary["recommended_settings"]["BROWSER_POOL_IDLE_TTL_SECONDS"]["changed"] is False


def test_build_env_override_text_only_emits_changed_values_by_default():
    store = RuntimeMetricsStore(
        file_path=_make_store_file("test_runtime_metrics_store_unit_d.jsonl"),
        max_records=5,
    )
    store.append_record(
        user_input="first",
        success=True,
        status="completed",
        runtime_metrics={
            "llm_cache": {"hits": 0, "misses": 3, "sets": 3},
            "browser_pool": {"reuse_hits": 0, "launches": 1},
        },
    )
    store.append_record(
        user_input="second",
        success=True,
        status="completed",
        runtime_metrics={
            "llm_cache": {"hits": 1, "misses": 8, "sets": 6},
            "browser_pool": {"reuse_hits": 0, "launches": 3},
        },
    )

    summary = store.summarize_recent(limit=5)
    override_text = store.build_env_override_text(summary)
    full_override_text = store.build_env_override_text(summary, include_unchanged=True)

    assert "URL_ANALYSIS_CACHE_TTL_SECONDS=" in override_text
    assert "PAGE_ANALYSIS_CACHE_TTL_SECONDS=" in override_text
    assert "BROWSER_POOL_IDLE_TTL_SECONDS=" in override_text
    assert full_override_text.count("=") >= override_text.count("=")


def test_compare_windows_detects_recent_improvement():
    store = RuntimeMetricsStore(
        file_path=_make_store_file("test_runtime_metrics_store_unit_e.jsonl"),
        max_records=10,
    )
    store.append_record(
        user_input="baseline one",
        success=True,
        status="completed",
        runtime_metrics={
            "llm_cache": {"hits": 0, "misses": 4, "sets": 4},
            "browser_pool": {"reuse_hits": 0, "launches": 1},
        },
    )
    store.append_record(
        user_input="baseline two",
        success=True,
        status="completed",
        runtime_metrics={
            "llm_cache": {"hits": 1, "misses": 9, "sets": 8},
            "browser_pool": {"reuse_hits": 0, "launches": 2},
        },
    )
    store.append_record(
        user_input="recent one",
        success=True,
        status="completed",
        runtime_metrics={
            "llm_cache": {"hits": 7, "misses": 10, "sets": 9},
            "browser_pool": {"reuse_hits": 3, "launches": 2},
        },
    )
    store.append_record(
        user_input="recent two",
        success=True,
        status="completed",
        runtime_metrics={
            "llm_cache": {"hits": 13, "misses": 11, "sets": 10},
            "browser_pool": {"reuse_hits": 7, "launches": 2},
        },
    )

    comparison = store.compare_windows(recent_limit=2, baseline_limit=2)

    assert comparison["baseline_window_size"] == 2
    assert comparison["recent_window_size"] == 2
    assert comparison["baseline"]["cache_hit_ratio"] is not None
    assert comparison["recent"]["cache_hit_ratio"] is not None
    assert comparison["delta"]["cache_hit_ratio"] is not None
    assert comparison["delta"]["cache_hit_ratio"] > 0
    assert comparison["delta"]["browser_reuse_ratio"] is not None
    assert comparison["delta"]["browser_reuse_ratio"] > 0


def test_persist_env_overrides_writes_override_file():
    store = RuntimeMetricsStore(
        file_path=_make_store_file("test_runtime_metrics_store_unit_f.jsonl"),
        max_records=5,
    )
    store.append_record(
        user_input="first",
        success=True,
        status="completed",
        runtime_metrics={
            "llm_cache": {"hits": 0, "misses": 3, "sets": 3},
            "browser_pool": {"reuse_hits": 0, "launches": 1},
        },
    )
    store.append_record(
        user_input="second",
        success=True,
        status="completed",
        runtime_metrics={
            "llm_cache": {"hits": 1, "misses": 8, "sets": 6},
            "browser_pool": {"reuse_hits": 0, "launches": 3},
        },
    )

    target_path = _make_store_file("test_runtime_metrics_overrides.env")
    written_path = store.persist_env_overrides(
        store.summarize_recent(limit=5),
        file_path=target_path,
    )
    content = written_path.read_text(encoding="utf-8")

    assert written_path == target_path
    assert "URL_ANALYSIS_CACHE_TTL_SECONDS=" in content
    assert "PAGE_ANALYSIS_CACHE_TTL_SECONDS=" in content
    assert "BROWSER_POOL_IDLE_TTL_SECONDS=" in content


def test_filter_records_by_date_range_returns_expected_subset():
    store = RuntimeMetricsStore(
        file_path=_make_store_file("test_runtime_metrics_store_unit_g.jsonl"),
        max_records=5,
    )
    records = [
        {"timestamp": "2026-03-01T09:00:00", "user_input": "first"},
        {"timestamp": "2026-03-02T09:00:00", "user_input": "second"},
        {"timestamp": "2026-03-03T09:00:00", "user_input": "third"},
        {"timestamp": "bad-timestamp", "user_input": "invalid"},
    ]

    filtered = store.filter_records_by_date_range(
        records=records,
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 2),
    )

    assert len(filtered) == 1
    assert filtered[0]["user_input"] == "second"
