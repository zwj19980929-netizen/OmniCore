"""
OmniCore PAOD (Plan-Action-Observation-Decision) 微反思基础设施
所有 Worker 共享的工具模块
"""
from typing import Dict, Any, List, Optional

from utils.logger import log_agent_action, log_warning

# === 常量 ===
MAX_STEPS_PER_TASK = 6
MAX_FALLBACK_ATTEMPTS = 2

FAILURE_TYPES: Dict[str, List[str]] = {
    "timeout": ["timeout", "timed out", "超时", "TimeoutError"],
    "selector_not_found": ["selector", "not found", "找不到元素", "no element", "query_selector"],
    "blocked_or_captcha": ["captcha", "验证码", "blocked", "forbidden", "403", "anti-bot", "反爬"],
    "permission_denied": ["permission", "denied", "权限", "PermissionError", "access denied"],
    "invalid_input": ["invalid", "参数错误", "missing param", "ValueError", "KeyError"],
}


def classify_failure(error_msg: str) -> str:
    """根据错误信息关键词匹配分类 failure_type"""
    if not error_msg:
        return "unknown"
    lower = error_msg.lower()
    for ftype, keywords in FAILURE_TYPES.items():
        for kw in keywords:
            if kw.lower() in lower:
                return ftype
    return "unknown"


def make_trace_step(step_no: int, plan: str, action: str, observation: str, decision: str) -> Dict[str, Any]:
    """构建 execution_trace 条目"""
    return {
        "step_no": step_no,
        "plan": plan,
        "action": action,
        "observation": observation,
        "decision": decision,
    }

def evaluate_success_criteria(criteria: List[str], result: Any) -> bool:
    """
    安全 eval 评估 success_criteria 条件列表。
    所有条件都满足返回 True，任一失败或异常返回 False。
    result 可以是 dict，eval 环境中以 `result` 变量暴露，
    同时将 dict 的 key 展开为 DotDict 以支持 result.data 语法。
    """
    if not criteria:
        return True

    class _DotDict(dict):
        """允许 result.data 式访问"""
        def __getattr__(self, key):
            try:
                return self[key]
            except KeyError:
                return None

    safe_result = _DotDict(result) if isinstance(result, dict) else result

    for cond in criteria:
        try:
            if not eval(cond, {"__builtins__": {}}, {"result": safe_result, "len": len, "str": str, "bool": bool}):
                return False
        except Exception:
            log_warning(f"success_criteria eval 失败: {cond}")
            return False
    return True


def execute_fallback(task: Dict[str, Any], fb_index: int, shared_memory: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    执行 fallback 策略。返回值含义：
    - None: 没有更多 fallback 可执行
    - {"action": "retry", "param_patch": {...}}: 用 patch 后的 params 重试当前 worker
    - {"action": "switch_worker", "target": "browser_agent", "param_patch": {...}}: 切换 worker
    """
    fallbacks = task.get("fallbacks", [])
    if fb_index >= len(fallbacks) or fb_index >= MAX_FALLBACK_ATTEMPTS:
        return None

    fb = fallbacks[fb_index]
    fb_type = fb.get("type", "retry")

    if fb_type == "retry":
        patch = fb.get("param_patch", {})
        log_agent_action("PAOD", f"Fallback #{fb_index + 1}: retry", f"patch={patch}")
        return {"action": "retry", "param_patch": patch}

    elif fb_type == "switch_worker":
        target = fb.get("target", "")
        patch = fb.get("param_patch", {})
        log_agent_action("PAOD", f"Fallback #{fb_index + 1}: switch_worker → {target}")
        return {"action": "switch_worker", "target": target, "param_patch": patch}

    return None

