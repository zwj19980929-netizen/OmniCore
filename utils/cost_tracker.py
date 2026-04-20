"""
CostTracker — LLM 调用成本计算与统计。
MonthlyCostGuard — 月度预算守卫。
"""

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import yaml

from utils.logger import log_warning


class CostTracker:
    """根据模型定价表计算单次 LLM 调用成本。"""

    _pricing: Optional[Dict] = None

    @classmethod
    def _get_pricing(cls) -> Dict:
        if cls._pricing is None:
            pricing_path = os.path.join(
                os.path.dirname(__file__), "..", "config", "model_pricing.yaml"
            )
            if os.path.exists(pricing_path):
                with open(pricing_path, "r", encoding="utf-8") as f:
                    cls._pricing = yaml.safe_load(f).get("pricing", {})
            else:
                cls._pricing = {}
        return cls._pricing

    @classmethod
    def calculate_cost(
        cls,
        model_full_name: str,
        tokens_in: int,
        tokens_out: int,
    ) -> float:
        """
        计算单次调用成本（USD）。

        model_full_name 格式接受：
        - "deepseek/deepseek-chat"
        - "openai/gpt-4o"
        - "gpt-4o"（不含 provider 前缀时自动推断）
        """
        if not model_full_name or (tokens_in == 0 and tokens_out == 0):
            return 0.0

        pricing = cls._get_pricing()
        provider, model_id = cls._parse_model_name(model_full_name)
        if not provider:
            return 0.0

        model_pricing = pricing.get(provider, {}).get(model_id, {})
        if not model_pricing:
            return 0.0

        input_cost = (tokens_in / 1_000_000) * model_pricing.get("input", 0)
        output_cost = (tokens_out / 1_000_000) * model_pricing.get("output", 0)
        return round(input_cost + output_cost, 8)

    @classmethod
    def _parse_model_name(cls, model_name: str) -> Tuple[str, str]:
        """解析 'provider/model' 格式，无前缀时按名称前缀猜测 provider。"""
        if "/" in model_name:
            parts = model_name.split("/", 1)
            return parts[0], parts[1]

        known_prefixes = {
            "gpt-": "openai",
            "o1": "openai",
            "claude-": "anthropic",
            "gemini-": "gemini",
            "deepseek-": "deepseek",
            "minimax-": "minimax",
            "MiniMax-": "minimax",
            "moonshot-": "kimi",
            "kimi-": "kimi",
            "glm-": "zhipu",
        }
        for prefix, provider in known_prefixes.items():
            if model_name.lower().startswith(prefix.lower()):
                return provider, model_name
        return "", model_name


class MonthlyCostGuard:
    """月度预算守卫：记录成本，接近上限时发出警告。"""

    def __init__(self, monthly_budget_usd: float, data_dir: str = "data"):
        self.monthly_budget_usd = monthly_budget_usd
        self._cost_file = os.path.join(str(data_dir), "monthly_cost.jsonl")

    def record_cost(
        self,
        cost_usd: float,
        model: str,
        job_id: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        """追加一条成本记录。tokens_in/tokens_out 为可选 token 用量字段(E3-lite)。"""
        if cost_usd <= 0 and tokens_in <= 0 and tokens_out <= 0:
            return
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "cost_usd": cost_usd,
            "model": model,
            "job_id": job_id,
            "tokens_in": int(tokens_in),
            "tokens_out": int(tokens_out),
        }
        os.makedirs(os.path.dirname(os.path.abspath(self._cost_file)), exist_ok=True)
        with open(self._cost_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def get_current_month_cost(self) -> float:
        """统计当月累计成本（USD）。"""
        if not os.path.exists(self._cost_file):
            return 0.0

        now = datetime.now(timezone.utc)
        total = 0.0
        with open(self._cost_file, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line.strip())
                    ts = datetime.fromisoformat(rec["ts"])
                    if ts.year == now.year and ts.month == now.month:
                        total += rec.get("cost_usd", 0)
                except Exception:
                    continue
        return round(total, 6)

    def get_token_usage(self, period: str = "month") -> Dict[str, int]:
        """
        统计 token 用量（E3-lite 监控)。

        period: "month"（当月）| "day"（今日 UTC）| "all"（全部历史）
        Returns: {"tokens_in": int, "tokens_out": int, "total": int, "calls": int}
        """
        result = {"tokens_in": 0, "tokens_out": 0, "total": 0, "calls": 0}
        if not os.path.exists(self._cost_file):
            return result

        now = datetime.now(timezone.utc)
        with open(self._cost_file, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line.strip())
                    ts = datetime.fromisoformat(rec["ts"])
                    if period == "month":
                        if ts.year != now.year or ts.month != now.month:
                            continue
                    elif period == "day":
                        if ts.date() != now.date():
                            continue
                    # period == "all" 不过滤
                    ti = int(rec.get("tokens_in", 0) or 0)
                    to = int(rec.get("tokens_out", 0) or 0)
                    if ti == 0 and to == 0:
                        continue
                    result["tokens_in"] += ti
                    result["tokens_out"] += to
                    result["calls"] += 1
                except Exception:
                    continue
        result["total"] = result["tokens_in"] + result["tokens_out"]
        return result

    def check_budget(self) -> Tuple[float, float, bool]:
        """
        检查预算状态。

        Returns:
            (used_usd, budget_usd, is_warning)
            当 budget > 0 且 used >= budget * 0.8 时 is_warning=True。
        """
        used = self.get_current_month_cost()
        if self.monthly_budget_usd <= 0:
            return used, 0.0, False
        warning = used >= self.monthly_budget_usd * 0.8
        return used, self.monthly_budget_usd, warning

    def get_top_models_by_cost(self, limit: int = 10) -> List[Dict]:
        """按成本统计当月各模型消耗排名（降序）。同时附带 tokens_in/tokens_out。"""
        if not os.path.exists(self._cost_file):
            return []

        model_costs: Dict[str, float] = defaultdict(float)
        model_calls: Dict[str, int] = defaultdict(int)
        model_tokens_in: Dict[str, int] = defaultdict(int)
        model_tokens_out: Dict[str, int] = defaultdict(int)

        now = datetime.now(timezone.utc)
        with open(self._cost_file, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line.strip())
                    ts = datetime.fromisoformat(rec["ts"])
                    if ts.year == now.year and ts.month == now.month:
                        model = rec.get("model", "unknown")
                        model_costs[model] += rec.get("cost_usd", 0)
                        model_calls[model] += 1
                        model_tokens_in[model] += int(rec.get("tokens_in", 0) or 0)
                        model_tokens_out[model] += int(rec.get("tokens_out", 0) or 0)
                except Exception:
                    continue

        results = [
            {
                "model": m,
                "cost_usd": round(c, 6),
                "calls": model_calls[m],
                "tokens_in": model_tokens_in[m],
                "tokens_out": model_tokens_out[m],
            }
            for m, c in model_costs.items()
        ]
        results.sort(key=lambda x: x["cost_usd"], reverse=True)
        return results[:limit]
