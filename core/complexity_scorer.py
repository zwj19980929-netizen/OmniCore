"""
TaskComplexityScorer — 对 Router 输出的任务列表打复杂度分。

评分维度（0~1.0）：
- 步骤数（多步骤更复杂）
- 工具类型（Browser > System > Web > File）
- 依赖链深度
- 用户输入关键词和长度
"""

from typing import Any, Dict, List

# 工具复杂度基准分（前缀匹配）
_TOOL_COMPLEXITY: Dict[str, float] = {
    "browser_agent": 0.8,
    "browser.": 0.8,
    "system_worker": 0.7,
    "terminal_worker": 0.7,
    "web_worker": 0.4,
    "web.": 0.4,
    "mcp.": 0.3,
    "file_worker": 0.2,
    "file.": 0.2,
}

# 高复杂度关键词（中英文）
_COMPLEX_KEYWORDS = [
    "分析", "对比", "总结", "规划", "设计", "生成报告", "汇总",
    "多步", "自动化", "批量", "爬取所有", "完整流程",
    "analyze", "compare", "summarize", "plan", "design", "automate",
    "report", "batch", "comprehensive", "pipeline",
]

# 低复杂度关键词（中英文）
_SIMPLE_KEYWORDS = [
    "查看", "打开", "搜索一下", "告诉我", "查询", "是什么", "怎么",
    "open", "show", "search", "tell me", "lookup", "what is", "how to",
]


def score_task_complexity(tasks: List[Dict[str, Any]], user_input: str = "") -> float:
    """
    对任务列表整体打复杂度分（0~1.0）。

    分段说明：
    - 0.0~0.35: 简单（单步查询）→ 用 low cost 模型
    - 0.35~0.65: 中等（多步执行）→ 用 medium cost 模型
    - 0.65~1.0:  复杂（浏览器/推理/批量）→ 用 high cost 模型
    """
    if not tasks:
        return _score_from_input_only(user_input)

    scores: List[float] = []

    # 维度 1：步骤数（5 步以上满分）
    step_score = min(len(tasks) / 5.0, 1.0)
    scores.append(step_score * 0.25)

    # 维度 2：最重工具类型
    tool_max = 0.0
    for task in tasks:
        tool_name = (task.get("tool_name") or "").lower()
        for prefix, base_score in _TOOL_COMPLEXITY.items():
            if tool_name.startswith(prefix) or prefix in tool_name:
                tool_max = max(tool_max, base_score)
                break
    scores.append(tool_max * 0.35)

    # 维度 3：依赖链深度
    max_depth = _compute_dependency_depth(tasks)
    depth_score = min(max_depth / 3.0, 1.0)
    scores.append(depth_score * 0.15)

    # 维度 4：用户输入关键词
    scores.append(_score_from_input_only(user_input) * 0.25)

    return round(min(sum(scores), 1.0), 3)


def _score_from_input_only(user_input: str) -> float:
    """仅基于输入文本估算复杂度。"""
    if not user_input:
        return 0.3

    lowered = user_input.lower()
    complex_hits = sum(1 for kw in _COMPLEX_KEYWORDS if kw in lowered)
    simple_hits = sum(1 for kw in _SIMPLE_KEYWORDS if kw in lowered)
    length_score = min(len(user_input) / 200.0, 0.5)

    base = 0.3 + length_score * 0.2 + complex_hits * 0.1 - simple_hits * 0.1
    return round(max(0.1, min(base, 1.0)), 3)


def _compute_dependency_depth(tasks: List[Dict[str, Any]]) -> int:
    """计算任务依赖链的最大深度。"""
    id_to_idx = {t.get("task_id"): i for i, t in enumerate(tasks)}
    max_depth = 0

    def dfs(task_id: str, visited: set) -> int:
        if task_id in visited:
            return 0
        visited.add(task_id)
        idx = id_to_idx.get(task_id)
        if idx is None:
            return 0
        task = tasks[idx]
        deps = task.get("depends_on") or []
        if not deps:
            return 1
        return 1 + max(dfs(dep, visited.copy()) for dep in deps)

    for task in tasks:
        depth = dfs(task.get("task_id", ""), set())
        max_depth = max(max_depth, depth)

    return max_depth


def complexity_to_cost_preference(complexity: float) -> str:
    """将复杂度分（0~1.0）映射到 cost_preference (low/medium/high)。"""
    if complexity < 0.35:
        return "low"
    if complexity < 0.65:
        return "medium"
    return "high"
