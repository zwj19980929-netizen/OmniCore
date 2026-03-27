"""
OmniCore Plan Pre-Validator — 规划前可行性预检

纯 Python 静态检查，零 LLM 调用。
在 Router 产出任务 DAG 之后、执行之前运行，拦截结构性错误。
"""
from __future__ import annotations

from collections import deque
from typing import Any, Dict, List, NamedTuple, Set

from utils.logger import log_agent_action, log_warning, log_success


# 能产出结构化数据（result.data）的 task_type 集合
_DATA_PRODUCING_TASK_TYPES = frozenset({
    "web_worker",
    "browser_agent",
})


class PlanValidationResult(NamedTuple):
    passed: bool
    issues: List[str]
    auto_fixes: List[str]


def _detect_cycles(task_queue: List[Dict[str, Any]]) -> List[str]:
    """Kahn 拓扑排序检测循环依赖，返回形成环的 task_id 列表。"""
    id_set = {t["task_id"] for t in task_queue}
    # 只保留队列内存在的依赖
    adj: Dict[str, List[str]] = {t["task_id"]: [] for t in task_queue}
    in_degree: Dict[str, int] = {t["task_id"]: 0 for t in task_queue}

    for task in task_queue:
        for dep in task.get("depends_on") or []:
            if dep in id_set:
                adj[dep].append(task["task_id"])
                in_degree[task["task_id"]] += 1

    queue: deque[str] = deque(tid for tid, deg in in_degree.items() if deg == 0)
    visited: Set[str] = set()
    while queue:
        node = queue.popleft()
        visited.add(node)
        for neighbor in adj[node]:
            in_degree[neighbor] -= 1
            if in_degree[neighbor] == 0:
                queue.append(neighbor)

    cycle_nodes = [tid for tid in id_set if tid not in visited]
    return cycle_nodes


def _detect_orphan_deps(task_queue: List[Dict[str, Any]]) -> List[tuple]:
    """检测引用不存在 task_id 的 depends_on 条目。返回 (task_id, orphan_dep) 列表。"""
    id_set = {t["task_id"] for t in task_queue}
    orphans = []
    for task in task_queue:
        for dep in task.get("depends_on") or []:
            if dep not in id_set:
                orphans.append((task["task_id"], dep))
    return orphans


def _detect_invalid_data_sources(task_queue: List[Dict[str, Any]]) -> List[tuple]:
    """检测 data_source/data_sources 引用的任务类型是否能产出数据。
    返回 (task_id, source_ref, reason) 列表。
    """
    task_map = {t["task_id"]: t for t in task_queue}
    issues = []
    for task in task_queue:
        params = task.get("params", {}) if isinstance(task.get("params"), dict) else {}
        sources: List[str] = []

        ds = params.get("data_source")
        if isinstance(ds, str) and ds.strip():
            sources.append(ds.strip())

        ds_list = params.get("data_sources")
        if isinstance(ds_list, list):
            sources.extend(str(s).strip() for s in ds_list if str(s).strip())

        for source_ref in sources:
            # 尝试匹配 task_id
            matched = None
            for tid, t in task_map.items():
                if source_ref == tid or source_ref in tid or tid in source_ref:
                    matched = t
                    break
            if matched is None:
                issues.append((task["task_id"], source_ref, "referenced task not found"))
            elif matched.get("task_type", "") not in _DATA_PRODUCING_TASK_TYPES:
                issues.append((
                    task["task_id"], source_ref,
                    f"task type '{matched.get('task_type', '')}' cannot produce structured data",
                ))
    return issues


def validate_plan(task_queue: List[Dict[str, Any]]) -> PlanValidationResult:
    """
    纯 Python 静态检查，零 LLM 调用。

    检查项：
    1. 循环依赖检测（Kahn 拓扑排序）
    2. 孤立依赖检测（depends_on 引用不存在的 task_id）
    3. data_source 引用合法性（目标任务能产出数据）
    4. 空任务队列检测

    自动修复策略：
    - 孤立依赖 → 自动删除无效的 depends_on 条目
    - 循环依赖 → 删除形成环的最低优先级边
    - 不可修复 → passed=False
    """
    log_agent_action("PlanValidator", "开始规划前可行性预检")

    if not task_queue:
        log_warning("PlanValidator: 任务队列为空")
        return PlanValidationResult(passed=True, issues=[], auto_fixes=[])

    issues: List[str] = []
    auto_fixes: List[str] = []

    # 1. 孤立依赖检测 + 自动修复
    orphans = _detect_orphan_deps(task_queue)
    for task_id, orphan_dep in orphans:
        # 自动修复：删除无效依赖
        for task in task_queue:
            if task["task_id"] == task_id:
                deps = task.get("depends_on", [])
                if orphan_dep in deps:
                    deps.remove(orphan_dep)
                    fix_msg = f"自动删除 {task_id} 的无效依赖 '{orphan_dep}'"
                    auto_fixes.append(fix_msg)
                    log_warning(f"PlanValidator: {fix_msg}")
                break

    # 2. 循环依赖检测 + 自动修复
    cycle_nodes = _detect_cycles(task_queue)
    if cycle_nodes:
        # 尝试修复：在环中找到优先级最低的任务，删除其所有 depends_on
        task_map = {t["task_id"]: t for t in task_queue}
        cycle_tasks = [task_map[tid] for tid in cycle_nodes if tid in task_map]
        if cycle_tasks:
            # 按优先级排序，优先级最低的任务断开依赖
            cycle_tasks.sort(key=lambda t: t.get("priority", 5))
            weakest = cycle_tasks[0]
            removed_deps = list(weakest.get("depends_on", []))
            weakest["depends_on"] = []
            fix_msg = f"循环依赖修复：清除 {weakest['task_id']} 的依赖 {removed_deps}"
            auto_fixes.append(fix_msg)
            log_warning(f"PlanValidator: {fix_msg}")

            # 二次检测
            remaining_cycles = _detect_cycles(task_queue)
            if remaining_cycles:
                issues.append(
                    f"循环依赖无法自动修复: {remaining_cycles}"
                )

    # 3. data_source 引用合法性
    invalid_sources = _detect_invalid_data_sources(task_queue)
    for task_id, source_ref, reason in invalid_sources:
        issues.append(
            f"任务 {task_id} 的 data_source '{source_ref}' 无效: {reason}"
        )

    passed = len(issues) == 0
    if passed:
        log_success("PlanValidator: 规划预检通过")
    else:
        log_warning(f"PlanValidator: 发现 {len(issues)} 个不可自动修复的问题")

    return PlanValidationResult(passed=passed, issues=issues, auto_fixes=auto_fixes)
