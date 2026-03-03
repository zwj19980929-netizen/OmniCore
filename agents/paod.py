"""
OmniCore PAOD (Plan-Action-Observation-Decision) 微反思基础设施
所有 Worker 共享的工具模块
"""
import ast
import operator
from typing import Dict, Any, List, Optional, Union

from core.constants import (
    FailureType,
    FAILURE_KEYWORDS,
    MAX_STEPS_PER_TASK,
    MAX_FALLBACK_ATTEMPTS,
    classify_failure_type,
)
from utils.logger import log_agent_action, log_warning

# 重导出常量以保持向后兼容
__all__ = [
    "MAX_STEPS_PER_TASK",
    "MAX_FALLBACK_ATTEMPTS",
    "classify_failure",
    "make_trace_step",
    "evaluate_success_criteria",
    "execute_fallback",
    "SafeExpressionEvaluator",
]


def classify_failure(error_msg: str) -> str:
    """根据错误信息关键词匹配分类 failure_type（向后兼容包装）"""
    return str(classify_failure_type(error_msg))


def make_trace_step(step_no: int, plan: str, action: str, observation: str, decision: str) -> Dict[str, Any]:
    """构建 execution_trace 条目"""
    return {
        "step_no": step_no,
        "plan": plan,
        "action": action,
        "observation": observation,
        "decision": decision,
    }


class SafeExpressionEvaluator:
    """
    安全的表达式求值器
    使用 AST 解析替代 eval，只允许白名单内的操作
    """

    # 允许的比较操作符
    ALLOWED_COMPARISONS = {
        ast.Eq: operator.eq,
        ast.NotEq: operator.ne,
        ast.Lt: operator.lt,
        ast.LtE: operator.le,
        ast.Gt: operator.gt,
        ast.GtE: operator.ge,
        ast.In: lambda a, b: a in b,
        ast.NotIn: lambda a, b: a not in b,
        ast.Is: operator.is_,
        ast.IsNot: operator.is_not,
    }

    # 允许的二元操作符
    ALLOWED_BINOPS = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Mod: operator.mod,
        ast.And: operator.and_,
        ast.Or: operator.or_,
    }

    # 允许的一元操作符
    ALLOWED_UNARYOPS = {
        ast.Not: operator.not_,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    # 允许的布尔操作符
    ALLOWED_BOOLOPS = {
        ast.And: lambda values: all(values),
        ast.Or: lambda values: any(values),
    }

    # 允许的内置函数（白名单）
    ALLOWED_BUILTINS = {
        "len": len,
        "str": str,
        "int": int,
        "float": float,
        "bool": bool,
        "abs": abs,
        "min": min,
        "max": max,
        "sum": sum,
        "any": any,
        "all": all,
        "isinstance": isinstance,
        "type": type,
    }

    # 允许的类型常量
    ALLOWED_TYPES = {
        "True": True,
        "False": False,
        "None": None,
        "list": list,
        "dict": dict,
        "str": str,
        "int": int,
        "float": float,
        "bool": bool,
    }

    def __init__(self, context: Dict[str, Any] = None):
        """
        初始化求值器

        Args:
            context: 求值上下文，如 {"result": result_dict}
        """
        self.context = context or {}

    def evaluate(self, expression: str) -> Any:
        """
        安全地求值表达式

        Args:
            expression: Python 表达式字符串

        Returns:
            表达式的求值结果

        Raises:
            ValueError: 如果表达式包含不允许的操作
        """
        try:
            tree = ast.parse(expression, mode="eval")
            return self._eval_node(tree.body)
        except SyntaxError as e:
            raise ValueError(f"表达式语法错误: {e}")

    def _eval_node(self, node: ast.AST) -> Any:
        """递归求值 AST 节点"""

        # 常量（数字、字符串等）
        if isinstance(node, ast.Constant):
            return node.value

        # Python 3.7 兼容：Num, Str, NameConstant
        if isinstance(node, ast.Num):
            return node.n
        if isinstance(node, ast.Str):
            return node.s

        # 名称引用（变量）
        if isinstance(node, ast.Name):
            name = node.id

            # 先检查上下文
            if name in self.context:
                return self.context[name]

            # 检查允许的内置函数
            if name in self.ALLOWED_BUILTINS:
                return self.ALLOWED_BUILTINS[name]

            # 检查允许的类型常量
            if name in self.ALLOWED_TYPES:
                return self.ALLOWED_TYPES[name]

            raise ValueError(f"未知的名称: {name}")

        # 属性访问（如 result.data）
        if isinstance(node, ast.Attribute):
            obj = self._eval_node(node.value)
            attr = node.attr

            # 禁止访问私有属性
            if attr.startswith("_"):
                raise ValueError(f"不允许访问私有属性: {attr}")

            # 禁止访问危险属性
            forbidden_attrs = {"__class__", "__dict__", "__globals__", "__code__", "__func__"}
            if attr in forbidden_attrs:
                raise ValueError(f"不允许访问属性: {attr}")

            # 安全地获取属性
            if isinstance(obj, dict):
                return obj.get(attr)
            return getattr(obj, attr, None)

        # 下标访问（如 result["data"]）
        if isinstance(node, ast.Subscript):
            obj = self._eval_node(node.value)

            # Python 3.9+ 使用 node.slice 直接作为节点
            # Python 3.8 及以下使用 node.slice.value
            if isinstance(node.slice, ast.Index):
                key = self._eval_node(node.slice.value)
            else:
                key = self._eval_node(node.slice)

            if isinstance(obj, dict):
                return obj.get(key)
            elif isinstance(obj, (list, tuple, str)):
                if isinstance(key, int) and 0 <= key < len(obj):
                    return obj[key]
                return None
            return None

        # 比较表达式（如 a == b, a > b）
        if isinstance(node, ast.Compare):
            left = self._eval_node(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                op_type = type(op)
                if op_type not in self.ALLOWED_COMPARISONS:
                    raise ValueError(f"不允许的比较操作: {op_type.__name__}")
                right = self._eval_node(comparator)
                try:
                    if not self.ALLOWED_COMPARISONS[op_type](left, right):
                        return False
                except TypeError:
                    return False
                left = right
            return True

        # 二元操作（如 a + b）
        if isinstance(node, ast.BinOp):
            op_type = type(node.op)
            if op_type not in self.ALLOWED_BINOPS:
                raise ValueError(f"不允许的二元操作: {op_type.__name__}")
            left = self._eval_node(node.left)
            right = self._eval_node(node.right)
            return self.ALLOWED_BINOPS[op_type](left, right)

        # 一元操作（如 not a, -a）
        if isinstance(node, ast.UnaryOp):
            op_type = type(node.op)
            if op_type not in self.ALLOWED_UNARYOPS:
                raise ValueError(f"不允许的一元操作: {op_type.__name__}")
            operand = self._eval_node(node.operand)
            return self.ALLOWED_UNARYOPS[op_type](operand)

        # 布尔操作（如 a and b, a or b）
        if isinstance(node, ast.BoolOp):
            op_type = type(node.op)
            if op_type not in self.ALLOWED_BOOLOPS:
                raise ValueError(f"不允许的布尔操作: {op_type.__name__}")
            values = [self._eval_node(v) for v in node.values]
            return self.ALLOWED_BOOLOPS[op_type](values)

        # 函数调用（如 len(data)）
        if isinstance(node, ast.Call):
            func = self._eval_node(node.func)

            # 确保是允许的函数
            if func not in self.ALLOWED_BUILTINS.values():
                # 检查是否是类型检查
                if func not in (list, dict, str, int, float, bool, type):
                    raise ValueError(f"不允许的函数调用: {func}")

            args = [self._eval_node(arg) for arg in node.args]
            kwargs = {kw.arg: self._eval_node(kw.value) for kw in node.keywords}
            return func(*args, **kwargs)

        # 列表字面量
        if isinstance(node, ast.List):
            return [self._eval_node(elt) for elt in node.elts]

        # 元组字面量
        if isinstance(node, ast.Tuple):
            return tuple(self._eval_node(elt) for elt in node.elts)

        # 字典字面量
        if isinstance(node, ast.Dict):
            return {
                self._eval_node(k): self._eval_node(v)
                for k, v in zip(node.keys, node.values)
            }

        # 条件表达式（三元运算符）
        if isinstance(node, ast.IfExp):
            test = self._eval_node(node.test)
            if test:
                return self._eval_node(node.body)
            return self._eval_node(node.orelse)

        raise ValueError(f"不支持的表达式类型: {type(node).__name__}")


class DotDict(dict):
    """允许 result.data 式访问的字典"""

    def __getattr__(self, key):
        try:
            value = self[key]
            if isinstance(value, dict) and not isinstance(value, DotDict):
                return DotDict(value)
            return value
        except KeyError:
            return None

    def __setattr__(self, key, value):
        self[key] = value


def evaluate_success_criteria(criteria: List[str], result: Any) -> bool:
    """
    安全评估 success_criteria 条件列表。
    使用 AST 解析替代 eval，防止代码注入。

    Args:
        criteria: 条件表达式列表，如 ["len(result.data) >= 5", "result.success == True"]
        result: 任务执行结果

    Returns:
        所有条件都满足返回 True，任一失败返回 False
    """
    if not criteria:
        return True

    # 将 result 包装为 DotDict 以支持属性访问
    if isinstance(result, dict):
        safe_result = DotDict(result)
    else:
        safe_result = result

    # 创建求值器
    evaluator = SafeExpressionEvaluator(context={"result": safe_result})

    for cond in criteria:
        try:
            if not evaluator.evaluate(cond):
                log_warning(f"success_criteria 未满足: {cond}")
                return False
        except ValueError as e:
            log_warning(f"success_criteria 求值失败 [{cond}]: {e}")
            return False
        except Exception as e:
            log_warning(f"success_criteria 异常 [{cond}]: {e}")
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
        log_agent_action("PAOD", f"Fallback #{fb_index + 1}: switch_worker -> {target}")
        return {"action": "switch_worker", "target": target, "param_patch": patch}

    return None
