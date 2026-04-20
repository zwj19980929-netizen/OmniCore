"""
Unit tests for core/complexity_scorer.py and utils/cost_tracker.py (P2-2).
"""

import json
import os
import tempfile

import pytest

from core.complexity_scorer import (
    _compute_dependency_depth,
    _score_from_input_only,
    complexity_to_cost_preference,
    score_task_complexity,
)
from utils.cost_tracker import CostTracker, MonthlyCostGuard


# ─────────────────────────────────────────────
# complexity_scorer — _score_from_input_only
# ─────────────────────────────────────────────

class TestScoreFromInputOnly:
    def test_empty_input_returns_default(self):
        assert _score_from_input_only("") == pytest.approx(0.3, abs=0.05)

    def test_simple_keyword_lowers_score(self):
        base = _score_from_input_only("分析一下")
        simple = _score_from_input_only("查看一下")
        assert simple < base

    def test_complex_keyword_raises_score(self):
        simple = _score_from_input_only("查看")
        complex_ = _score_from_input_only("帮我分析整份市场报告并汇总")
        assert complex_ > simple

    def test_long_input_raises_score(self):
        short = _score_from_input_only("搜索")
        long_ = _score_from_input_only("搜索" * 50)
        assert long_ > short

    def test_score_clamped_0_to_1(self):
        s = _score_from_input_only("分析" * 20)
        assert 0.0 <= s <= 1.0

    def test_english_complex_keywords(self):
        base = _score_from_input_only("show me the weather")
        complex_ = _score_from_input_only("analyze and summarize the complete pipeline")
        assert complex_ > base


# ─────────────────────────────────────────────
# complexity_scorer — _compute_dependency_depth
# ─────────────────────────────────────────────

class TestComputeDependencyDepth:
    def test_no_tasks_returns_zero(self):
        assert _compute_dependency_depth([]) == 0

    def test_single_task_no_deps(self):
        tasks = [{"task_id": "t1", "depends_on": []}]
        assert _compute_dependency_depth(tasks) == 1

    def test_linear_chain(self):
        tasks = [
            {"task_id": "t1", "depends_on": []},
            {"task_id": "t2", "depends_on": ["t1"]},
            {"task_id": "t3", "depends_on": ["t2"]},
        ]
        assert _compute_dependency_depth(tasks) == 3

    def test_parallel_tasks(self):
        tasks = [
            {"task_id": "t1", "depends_on": []},
            {"task_id": "t2", "depends_on": []},
            {"task_id": "t3", "depends_on": ["t1", "t2"]},
        ]
        assert _compute_dependency_depth(tasks) == 2

    def test_missing_dep_graceful(self):
        tasks = [{"task_id": "t1", "depends_on": ["ghost"]}]
        depth = _compute_dependency_depth(tasks)
        assert depth >= 1  # t1 itself counts


# ─────────────────────────────────────────────
# complexity_scorer — score_task_complexity
# ─────────────────────────────────────────────

class TestScoreTaskComplexity:
    def test_empty_tasks_falls_back_to_input(self):
        score = score_task_complexity([], "查一下天气")
        assert 0.0 <= score <= 1.0

    def test_browser_task_scores_higher(self):
        browser_tasks = [{"task_id": "t1", "tool_name": "browser_agent", "depends_on": []}]
        file_tasks = [{"task_id": "t1", "tool_name": "file_worker", "depends_on": []}]
        assert score_task_complexity(browser_tasks) > score_task_complexity(file_tasks)

    def test_more_steps_scores_higher(self):
        one = [{"task_id": "t1", "tool_name": "web_worker", "depends_on": []}]
        five = [{"task_id": f"t{i}", "tool_name": "web_worker", "depends_on": []} for i in range(5)]
        assert score_task_complexity(five) >= score_task_complexity(one)

    def test_mcp_complexity_below_browser(self):
        mcp_tasks = [{"task_id": "t1", "tool_name": "mcp.github.create_issue", "depends_on": []}]
        browser_tasks = [{"task_id": "t1", "tool_name": "browser_agent", "depends_on": []}]
        assert score_task_complexity(browser_tasks) > score_task_complexity(mcp_tasks)

    def test_result_in_range(self):
        tasks = [
            {"task_id": "t1", "tool_name": "browser_agent", "depends_on": []},
            {"task_id": "t2", "tool_name": "file_worker", "depends_on": ["t1"]},
            {"task_id": "t3", "tool_name": "web_worker", "depends_on": ["t2"]},
        ]
        score = score_task_complexity(tasks, "批量爬取并汇总")
        assert 0.0 <= score <= 1.0

    def test_rounded_to_3_decimal_places(self):
        tasks = [{"task_id": "t1", "tool_name": "web_worker", "depends_on": []}]
        score = score_task_complexity(tasks, "test")
        assert score == round(score, 3)


# ─────────────────────────────────────────────
# complexity_to_cost_preference
# ─────────────────────────────────────────────

class TestComplexityToCostPreference:
    @pytest.mark.parametrize("score,expected", [
        (0.0, "low"),
        (0.2, "low"),
        (0.34, "low"),
        (0.35, "medium"),
        (0.5, "medium"),
        (0.64, "medium"),
        (0.65, "high"),
        (0.9, "high"),
        (1.0, "high"),
    ])
    def test_mapping(self, score, expected):
        assert complexity_to_cost_preference(score) == expected


# ─────────────────────────────────────────────
# CostTracker — calculate_cost
# ─────────────────────────────────────────────

class TestCostTrackerCalculateCost:
    def test_zero_tokens_returns_zero(self):
        assert CostTracker.calculate_cost("openai/gpt-4o", 0, 0) == 0.0

    def test_known_model_calculates_cost(self):
        # gpt-4o: input $2.50/M, output $10.00/M
        cost = CostTracker.calculate_cost("openai/gpt-4o", 1_000_000, 1_000_000)
        assert abs(cost - 12.5) < 0.001

    def test_unknown_model_returns_zero(self):
        assert CostTracker.calculate_cost("unknown/phantom-model", 1000, 500) == 0.0

    def test_model_without_provider_prefix(self):
        cost = CostTracker.calculate_cost("gpt-4o-mini", 1_000_000, 0)
        assert cost > 0  # should resolve to openai

    def test_deepseek_model(self):
        cost = CostTracker.calculate_cost("deepseek/deepseek-chat", 1_000_000, 1_000_000)
        assert cost > 0
        assert cost < 2.0  # deep seek 低价

    def test_anthropic_prefix(self):
        cost = CostTracker.calculate_cost("claude-sonnet-4-6", 500_000, 500_000)
        assert cost > 0

    def test_empty_model_name_returns_zero(self):
        assert CostTracker.calculate_cost("", 1000, 500) == 0.0

    def test_slash_model_parsing(self):
        cost = CostTracker.calculate_cost("deepseek/deepseek-coder", 1_000_000, 0)
        assert cost == pytest.approx(0.27, abs=0.01)


# ─────────────────────────────────────────────
# MonthlyCostGuard
# ─────────────────────────────────────────────

class TestMonthlyCostGuard:
    def _make_guard(self, budget: float = 10.0):
        tmpdir = tempfile.mkdtemp()
        return MonthlyCostGuard(monthly_budget_usd=budget, data_dir=tmpdir), tmpdir

    def test_initial_cost_is_zero(self):
        guard, _ = self._make_guard()
        assert guard.get_current_month_cost() == 0.0

    def test_record_and_retrieve_cost(self):
        guard, _ = self._make_guard()
        guard.record_cost(0.05, model="gpt-4o")
        guard.record_cost(0.10, model="deepseek-chat")
        total = guard.get_current_month_cost()
        assert abs(total - 0.15) < 1e-5

    def test_zero_cost_not_recorded(self):
        guard, tmpdir = self._make_guard()
        guard.record_cost(0.0, model="gpt-4o")
        cost_file = os.path.join(tmpdir, "monthly_cost.jsonl")
        assert not os.path.exists(cost_file)

    def test_check_budget_no_budget(self):
        guard, _ = self._make_guard(budget=0.0)
        guard.record_cost(5.0, model="gpt-4o")
        used, budget, warning = guard.check_budget()
        assert budget == 0.0
        assert not warning

    def test_check_budget_below_threshold(self):
        guard, _ = self._make_guard(budget=10.0)
        guard.record_cost(5.0, model="gpt-4o")
        used, budget, warning = guard.check_budget()
        assert not warning

    def test_check_budget_above_threshold(self):
        guard, _ = self._make_guard(budget=10.0)
        guard.record_cost(9.0, model="gpt-4o")
        used, budget, warning = guard.check_budget()
        assert warning

    def test_get_top_models_empty(self):
        guard, _ = self._make_guard()
        assert guard.get_top_models_by_cost() == []

    def test_get_top_models_sorted(self):
        guard, _ = self._make_guard()
        guard.record_cost(0.01, model="cheap-model")
        guard.record_cost(1.00, model="expensive-model")
        guard.record_cost(0.50, model="medium-model")
        top = guard.get_top_models_by_cost()
        assert top[0]["model"] == "expensive-model"
        assert top[-1]["model"] == "cheap-model"

    def test_get_top_models_call_count(self):
        guard, _ = self._make_guard()
        for _ in range(3):
            guard.record_cost(0.01, model="gpt-4o")
        guard.record_cost(0.02, model="deepseek-chat")
        top = guard.get_top_models_by_cost()
        gpt_entry = next(x for x in top if x["model"] == "gpt-4o")
        assert gpt_entry["calls"] == 3

    def test_record_persists_to_file(self):
        guard, tmpdir = self._make_guard()
        guard.record_cost(0.05, model="gpt-4o", job_id="job-1")
        cost_file = os.path.join(tmpdir, "monthly_cost.jsonl")
        assert os.path.exists(cost_file)
        with open(cost_file) as f:
            record = json.loads(f.readline())
        assert record["model"] == "gpt-4o"
        assert record["cost_usd"] == pytest.approx(0.05)
        assert record["job_id"] == "job-1"

    def test_corrupt_line_ignored(self):
        from datetime import datetime, timezone
        guard, tmpdir = self._make_guard()
        cost_file = os.path.join(tmpdir, "monthly_cost.jsonl")
        os.makedirs(tmpdir, exist_ok=True)
        # Use current month so the record passes the year/month filter
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-01T00:00:00+00:00")
        with open(cost_file, "w") as f:
            f.write("NOT JSON\n")
            f.write(json.dumps({"ts": ts, "cost_usd": 0.1, "model": "x"}) + "\n")
        total = guard.get_current_month_cost()
        assert total == pytest.approx(0.1)

    # ─── E3-lite: token 用量监控 ───────────────────────
    def test_record_persists_tokens(self):
        guard, tmpdir = self._make_guard()
        guard.record_cost(0.05, model="gpt-4o", tokens_in=1000, tokens_out=500)
        cost_file = os.path.join(tmpdir, "monthly_cost.jsonl")
        with open(cost_file) as f:
            record = json.loads(f.readline())
        assert record["tokens_in"] == 1000
        assert record["tokens_out"] == 500

    def test_record_with_only_tokens_no_cost(self):
        """tokens>0 但 cost=0（定价表缺失）仍应落盘，方便观测。"""
        guard, tmpdir = self._make_guard()
        guard.record_cost(0.0, model="unknown-model", tokens_in=100, tokens_out=50)
        cost_file = os.path.join(tmpdir, "monthly_cost.jsonl")
        assert os.path.exists(cost_file)
        usage = guard.get_token_usage(period="month")
        assert usage["tokens_in"] == 100
        assert usage["tokens_out"] == 50
        assert usage["total"] == 150
        assert usage["calls"] == 1

    def test_get_token_usage_empty(self):
        guard, _ = self._make_guard()
        usage = guard.get_token_usage(period="month")
        assert usage == {"tokens_in": 0, "tokens_out": 0, "total": 0, "calls": 0}

    def test_get_token_usage_aggregates_month(self):
        guard, _ = self._make_guard()
        guard.record_cost(0.01, model="m1", tokens_in=100, tokens_out=50)
        guard.record_cost(0.02, model="m2", tokens_in=200, tokens_out=80)
        usage = guard.get_token_usage(period="month")
        assert usage["tokens_in"] == 300
        assert usage["tokens_out"] == 130
        assert usage["total"] == 430
        assert usage["calls"] == 2

    def test_get_token_usage_day_filter(self):
        """period='day' 应只统计今日 UTC 的记录。"""
        from datetime import datetime, timezone
        guard, tmpdir = self._make_guard()
        cost_file = os.path.join(tmpdir, "monthly_cost.jsonl")
        os.makedirs(tmpdir, exist_ok=True)
        now = datetime.now(timezone.utc)
        today_ts = now.isoformat()
        yesterday_ts = now.replace(day=max(1, now.day - 1) if now.day > 1 else 1).isoformat()
        with open(cost_file, "w") as f:
            f.write(json.dumps({
                "ts": today_ts, "cost_usd": 0.01, "model": "m",
                "tokens_in": 100, "tokens_out": 50,
            }) + "\n")
            if now.day > 1:
                f.write(json.dumps({
                    "ts": yesterday_ts, "cost_usd": 0.01, "model": "m",
                    "tokens_in": 999, "tokens_out": 999,
                }) + "\n")
        day_usage = guard.get_token_usage(period="day")
        assert day_usage["tokens_in"] == 100
        assert day_usage["tokens_out"] == 50

    def test_get_token_usage_ignores_rows_without_tokens(self):
        """旧格式无 tokens 字段的行不计入 calls。"""
        from datetime import datetime, timezone
        guard, tmpdir = self._make_guard()
        cost_file = os.path.join(tmpdir, "monthly_cost.jsonl")
        os.makedirs(tmpdir, exist_ok=True)
        now = datetime.now(timezone.utc)
        ts = now.isoformat()
        with open(cost_file, "w") as f:
            f.write(json.dumps({"ts": ts, "cost_usd": 0.05, "model": "legacy"}) + "\n")
        usage = guard.get_token_usage(period="month")
        assert usage["calls"] == 0
        assert usage["total"] == 0

    def test_top_models_includes_tokens(self):
        guard, _ = self._make_guard()
        guard.record_cost(0.10, model="m1", tokens_in=300, tokens_out=100)
        guard.record_cost(0.01, model="m2", tokens_in=50, tokens_out=20)
        top = guard.get_top_models_by_cost()
        top_map = {t["model"]: t for t in top}
        assert top_map["m1"]["tokens_in"] == 300
        assert top_map["m1"]["tokens_out"] == 100
        assert top_map["m2"]["tokens_in"] == 50
