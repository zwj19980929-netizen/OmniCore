"""
S4: Tool 六阶段执行 Pipeline

将工具执行从简单的 "调用 → 拿结果" 升级为六阶段流水线：
  1. Schema 校验（JSON Schema）
  2. 语义校验（validate_input 回调）
  3. 上下文注入（cwd / session_id / job_id / 路径展开）
  4. 权限检查（policy_engine 下沉）
  5. 实际执行（tool adapter）
  6. 结果规范化（统一 ToolResult）

设计要点：
- 不改 worker 内部实现，Pipeline 包裹在 adapter 外层
- strict_mode=False 时，校验失败降级 + warning（与旧行为兼容）
- ToolResult 通过 to_outcome_dict() 转换后兼容现有 _apply_task_outcome()
"""
from __future__ import annotations

import copy
import datetime
import enum
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import settings
from utils.logger import log_agent_action, log_warning


# ---------------------------------------------------------------------------
# Stage 枚举
# ---------------------------------------------------------------------------

class ToolPipelineStage(enum.Enum):
    SCHEMA_VALIDATE = "schema_validate"
    SEMANTIC_VALIDATE = "semantic_validate"
    INJECT_CONTEXT = "inject_context"
    CHECK_PERMISSION = "check_permission"
    EXECUTE = "execute"
    NORMALIZE_RESULT = "normalize_result"


# ---------------------------------------------------------------------------
# ToolResult — 规范化执行结果
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """Pipeline 内部的结构化执行结果。"""

    success: bool
    output: str = ""
    structured_data: Optional[Dict[str, Any]] = None
    artifacts: List[str] = field(default_factory=list)
    error: Optional[str] = None
    error_type: Optional[str] = None

    def to_outcome_dict(
        self,
        *,
        task_type: str = "",
        tool_name: str = "",
        params: Optional[Dict[str, Any]] = None,
        execution_trace: Optional[List[Any]] = None,
        risk_level: str = "medium",
    ) -> Dict[str, Any]:
        """转换为兼容 _apply_task_outcome() 的 outcome dict。"""
        from core.constants import TaskStatus

        result_dict: Dict[str, Any] = {}
        if self.structured_data is not None:
            result_dict.update(self.structured_data)
        if "success" not in result_dict:
            result_dict["success"] = self.success
        if self.output and "output" not in result_dict:
            result_dict["output"] = self.output
        if self.error and "error" not in result_dict:
            result_dict["error"] = self.error

        status = str(TaskStatus.COMPLETED) if self.success else str(TaskStatus.FAILED)
        failure_type = None
        if not self.success and self.error_type:
            failure_type = self.error_type

        return {
            "status": status,
            "task_type": task_type,
            "tool_name": tool_name,
            "params": params or {},
            "result": result_dict,
            "execution_trace": execution_trace or [],
            "failure_type": failure_type,
            "error_trace": self.error or "",
            "risk_level": risk_level,
        }


# ---------------------------------------------------------------------------
# StageError — 阶段错误记录
# ---------------------------------------------------------------------------

@dataclass
class StageError:
    stage: ToolPipelineStage
    message: str
    fatal: bool = False


# ---------------------------------------------------------------------------
# ToolExecutionContext — 贯穿整个 Pipeline 的上下文
# ---------------------------------------------------------------------------

@dataclass
class ToolExecutionContext:
    tool_name: str
    raw_params: Dict[str, Any]
    validated_params: Optional[Dict[str, Any]] = None
    injected_params: Optional[Dict[str, Any]] = None
    permission_result: Optional[str] = None  # "allow" | "deny" | "ask"
    permission_reason: str = ""
    raw_result: Optional[Dict[str, Any]] = None
    normalized_result: Optional[ToolResult] = None
    stage_errors: List[StageError] = field(default_factory=list)
    stage_timings: Dict[str, float] = field(default_factory=dict)

    @property
    def has_fatal_error(self) -> bool:
        return any(e.fatal for e in self.stage_errors)

    @property
    def effective_params(self) -> Dict[str, Any]:
        return self.injected_params or self.validated_params or self.raw_params


# ---------------------------------------------------------------------------
# 内置语义校验规则
# ---------------------------------------------------------------------------

_DANGEROUS_PATH_PREFIXES = (
    "/etc/", "/usr/", "/bin/", "/sbin/", "/var/",
    "/System/", "/Library/",
    "C:\\Windows\\", "C:\\Program Files\\",
)


def _validate_file_paths(params: Dict[str, Any]) -> Optional[str]:
    """拒绝系统关键路径的写操作。"""
    action = str(params.get("action", "")).lower()
    if action in ("read", "read_file", "glob", "grep"):
        return None

    for key in ("file_path", "target_path", "source_path"):
        path_val = str(params.get(key, "") or "").strip()
        if not path_val:
            continue
        for prefix in _DANGEROUS_PATH_PREFIXES:
            if path_val.startswith(prefix):
                return f"Blocked: path '{path_val}' is in a protected system directory ({prefix})"
    return None


# ---------------------------------------------------------------------------
# S6: 终端命令黑名单 — 拒绝显然危险的命令
# ---------------------------------------------------------------------------

_DANGEROUS_COMMAND_PATTERNS = [
    re.compile(r"\brm\s+(-[a-zA-Z]*r[a-zA-Z]*f|--recursive)\s+/\s*$", re.IGNORECASE),  # rm -rf /
    re.compile(r"\brm\s+(-[a-zA-Z]*f[a-zA-Z]*r|--force)\s+/\s*$", re.IGNORECASE),
    re.compile(r"\bmkfs\b", re.IGNORECASE),
    re.compile(r"\bdd\s+if=", re.IGNORECASE),
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;", re.IGNORECASE),  # fork bomb
    re.compile(r"\bchmod\s+(-[a-zA-Z]*R)?\s*(0?777|a\+rwx)\s+/\s*$", re.IGNORECASE),
    re.compile(r"\bchown\s+(-[a-zA-Z]*R)?\s+\S+\s+/\s*$", re.IGNORECASE),
    re.compile(r">\s*/dev/sd[a-z]", re.IGNORECASE),
]


def _validate_terminal_command(params: Dict[str, Any]) -> Optional[str]:
    """S6: 拦截显然危险的终端命令。"""
    command = str(params.get("command", "") or "").strip()
    if not command:
        return None
    for pattern in _DANGEROUS_COMMAND_PATTERNS:
        if pattern.search(command):
            return f"Blocked: command matches dangerous pattern — {pattern.pattern}"
    return None


# S6: 文件操作路径白名单 — 写操作只允许 data/、用户桌面、/tmp/
_ALLOWED_WRITE_PREFIXES: Optional[tuple] = None


def _get_allowed_write_prefixes() -> tuple:
    """懒加载允许写入的路径前缀。"""
    global _ALLOWED_WRITE_PREFIXES
    if _ALLOWED_WRITE_PREFIXES is None:
        data_dir = str(getattr(settings, "DATA_DIR", Path.cwd() / "data"))
        desktop = str(getattr(settings, "USER_DESKTOP_PATH", Path.home() / "Desktop"))
        output_dir = str(getattr(settings, "DEFAULT_OUTPUT_DIRECTORY", "")).strip()
        prefixes = [data_dir, desktop, "/tmp/", str(Path.home())]
        if output_dir:
            prefixes.append(output_dir)
        _ALLOWED_WRITE_PREFIXES = tuple(prefixes)
    return _ALLOWED_WRITE_PREFIXES


_BUILTIN_VALIDATORS = {
    "file.read_write": _validate_file_paths,
    "terminal.edit_file": _validate_file_paths,
    "terminal.execute": _validate_terminal_command,
    "system.control": _validate_terminal_command,
}


# ---------------------------------------------------------------------------
# ToolPipeline — 六阶段执行引擎
# ---------------------------------------------------------------------------

class ToolPipeline:
    """六阶段工具执行 Pipeline。

    用法:
        pipeline = ToolPipeline(strict_mode=False)
        ctx = await pipeline.execute(tool_name, params, state, registered_tool)
    """

    def __init__(self, *, strict_mode: Optional[bool] = None):
        if strict_mode is not None:
            self._strict = strict_mode
        else:
            self._strict = getattr(settings, "TOOL_PIPELINE_STRICT_MODE", False)

    # ---- public API ----

    async def execute(
        self,
        tool_name: str,
        params: Dict[str, Any],
        state: Dict[str, Any],
        registered_tool: Any,
        *,
        shared_memory_snapshot: Optional[Dict[str, Any]] = None,
    ) -> ToolExecutionContext:
        """按顺序执行六阶段，返回 ToolExecutionContext。"""
        ctx = ToolExecutionContext(
            tool_name=tool_name,
            raw_params=copy.deepcopy(params),
        )

        spec = registered_tool.spec if registered_tool else None

        # Stage 1: Schema 校验
        self._run_stage(ctx, ToolPipelineStage.SCHEMA_VALIDATE,
                        lambda: self._stage_schema_validate(ctx, spec))
        if ctx.has_fatal_error:
            ctx.normalized_result = self._make_error_result(ctx)
            return ctx

        # Stage 2: 语义校验
        self._run_stage(ctx, ToolPipelineStage.SEMANTIC_VALIDATE,
                        lambda: self._stage_semantic_validate(ctx, spec))
        if ctx.has_fatal_error:
            ctx.normalized_result = self._make_error_result(ctx)
            return ctx

        # Stage 3: 上下文注入
        self._run_stage(ctx, ToolPipelineStage.INJECT_CONTEXT,
                        lambda: self._stage_inject_context(ctx, spec, state))

        # Stage 4: 权限检查（S6: 传入 spec 以获取 trust_level）
        self._run_stage(ctx, ToolPipelineStage.CHECK_PERMISSION,
                        lambda: self._stage_check_permission(ctx, state, spec))
        if ctx.has_fatal_error:
            ctx.normalized_result = self._make_error_result(ctx)
            return ctx

        # Stage 5: 执行
        await self._run_stage_async(ctx, ToolPipelineStage.EXECUTE,
                                    lambda: self._stage_execute(ctx, state, registered_tool, shared_memory_snapshot))

        # Stage 6: 结果规范化
        self._run_stage(ctx, ToolPipelineStage.NORMALIZE_RESULT,
                        lambda: self._stage_normalize_result(ctx))

        # S6: 审计日志
        self._write_audit_log(ctx, spec)

        # C2: per-tool failure profile (sliding-window stats for planner hints)
        self._record_tool_failure_profile(ctx)

        return ctx

    # ---- Stage 实现 ----

    def _stage_schema_validate(self, ctx: ToolExecutionContext, spec: Any) -> None:
        if spec is None:
            ctx.validated_params = copy.deepcopy(ctx.raw_params)
            return

        schema = spec.input_schema
        if not schema:
            ctx.validated_params = copy.deepcopy(ctx.raw_params)
            return

        try:
            import jsonschema
            jsonschema.validate(instance=ctx.raw_params, schema=schema)
            ctx.validated_params = copy.deepcopy(ctx.raw_params)
        except Exception as e:
            error_msg = self._format_schema_error(e)
            if self._strict:
                ctx.stage_errors.append(StageError(
                    stage=ToolPipelineStage.SCHEMA_VALIDATE,
                    message=error_msg,
                    fatal=True,
                ))
            else:
                ctx.stage_errors.append(StageError(
                    stage=ToolPipelineStage.SCHEMA_VALIDATE,
                    message=error_msg,
                    fatal=False,
                ))
                log_warning(f"[ToolPipeline] Schema validation warning for {ctx.tool_name}: {error_msg}")
                ctx.validated_params = copy.deepcopy(ctx.raw_params)

    def _stage_semantic_validate(self, ctx: ToolExecutionContext, spec: Any) -> None:
        validators = []

        # ToolSpec.validate_input 自定义校验
        if spec is not None and spec.validate_input is not None:
            validators.append(("custom", spec.validate_input))

        # 内置语义校验
        builtin = _BUILTIN_VALIDATORS.get(ctx.tool_name)
        if builtin is not None:
            validators.append(("builtin", builtin))

        params = ctx.validated_params or ctx.raw_params
        for label, validator in validators:
            try:
                error = validator(params)
            except Exception as e:
                error = f"Validator ({label}) raised: {e}"
            if error:
                if self._strict:
                    ctx.stage_errors.append(StageError(
                        stage=ToolPipelineStage.SEMANTIC_VALIDATE,
                        message=error,
                        fatal=True,
                    ))
                    return
                else:
                    ctx.stage_errors.append(StageError(
                        stage=ToolPipelineStage.SEMANTIC_VALIDATE,
                        message=error,
                        fatal=False,
                    ))
                    log_warning(f"[ToolPipeline] Semantic validation warning for {ctx.tool_name}: {error}")

    def _stage_inject_context(self, ctx: ToolExecutionContext, spec: Any, state: Dict[str, Any]) -> None:
        params = copy.deepcopy(ctx.validated_params or ctx.raw_params)

        # 从 state 中提取可注入的上下文
        injectable = {
            "cwd": str(Path.cwd()),
            "session_id": state.get("session_id", ""),
            "job_id": state.get("job_id", ""),
            "timeout": getattr(settings, "SYSTEM_COMMAND_TIMEOUT", 30),
        }

        # 按 required_context 注入
        required = spec.required_context if (spec is not None and spec.required_context) else []
        for key in required:
            if key in injectable and key not in params:
                params[key] = injectable[key]

        # 路径参数展开：~ → home dir，相对路径 → 绝对路径
        for key in ("file_path", "target_path", "source_path", "working_dir", "working_directory"):
            if key in params and isinstance(params[key], str) and params[key].strip():
                expanded = os.path.expanduser(params[key])
                if not os.path.isabs(expanded):
                    expanded = os.path.join(str(Path.cwd()), expanded)
                params[key] = expanded

        ctx.injected_params = params

    def _stage_check_permission(self, ctx: ToolExecutionContext, state: Dict[str, Any], spec: Any = None) -> None:
        """权限检查：调用 policy_engine，将结果记入 ctx。

        S6 增强：低信任工具的权限阈值更严格。
        注意：权限检查结果为 "ask" 或 "deny" 时不在 pipeline 内阻塞，
        而是将结果写入 ctx，由上层（graph / task_executor）决定是否暂停。
        Pipeline 只在 strict_mode + deny 时设为 fatal。
        """
        try:
            from core.policy_engine import evaluate_task_policy
        except ImportError:
            ctx.permission_result = "allow"
            ctx.permission_reason = "policy_engine not available"
            return

        task_dict = {
            "tool_name": ctx.tool_name,
            "params": ctx.effective_params,
            "description": "",
        }
        decision = evaluate_task_policy(task_dict)

        # S6: 低信任工具执行 destructive 操作时强制要求审批
        trust_level = spec.trust_level if (spec is not None and hasattr(spec, "trust_level")) else "builtin"
        is_destructive = spec.destructive if (spec is not None and hasattr(spec, "destructive")) else True
        if trust_level in ("mcp_remote",) and is_destructive and not decision.requires_confirmation:
            ctx.permission_result = "ask"
            ctx.permission_reason = f"S6 trust policy: {trust_level} + destructive requires approval"
            return

        if decision.requires_confirmation:
            ctx.permission_result = "ask"
            ctx.permission_reason = decision.reason
        else:
            ctx.permission_result = "allow"
            ctx.permission_reason = decision.reason

    async def _stage_execute(
        self,
        ctx: ToolExecutionContext,
        state: Dict[str, Any],
        registered_tool: Any,
        shared_memory_snapshot: Optional[Dict[str, Any]],
    ) -> None:
        """实际执行：委托给 tool adapter。"""
        from core.tool_adapters import execute_tool_via_adapter

        task_dict = self._build_task_dict(ctx, state)
        snapshot = shared_memory_snapshot or {}

        try:
            outcome = await execute_tool_via_adapter(task_dict, snapshot, registered_tool)
            ctx.raw_result = outcome
        except Exception as e:
            ctx.raw_result = {
                "status": "failed",
                "result": {"success": False, "error": str(e)},
                "error_trace": str(e),
                "failure_type": type(e).__name__,
            }
            ctx.stage_errors.append(StageError(
                stage=ToolPipelineStage.EXECUTE,
                message=str(e),
                fatal=False,
            ))

    def _stage_normalize_result(self, ctx: ToolExecutionContext) -> None:
        """将 raw_result (adapter outcome dict) 规范化为 ToolResult。"""
        raw = ctx.raw_result
        if raw is None:
            ctx.normalized_result = ToolResult(
                success=False,
                error="No result from execution stage",
                error_type="ExecutionError",
            )
            return

        result_data = raw.get("result", {})
        if isinstance(result_data, str):
            success = raw.get("status", "") == "completed"
            ctx.normalized_result = ToolResult(
                success=success,
                output=result_data,
                error=None if success else result_data,
            )
            return

        success = bool(result_data.get("success", False)) if isinstance(result_data, dict) else False
        output = ""
        if isinstance(result_data, dict):
            output = str(
                result_data.get("output")
                or result_data.get("content")
                or result_data.get("extracted_text")
                or result_data.get("text")
                or ""
            )

        ctx.normalized_result = ToolResult(
            success=success,
            output=output,
            structured_data=result_data if isinstance(result_data, dict) else None,
            error=raw.get("error_trace") or (result_data.get("error") if isinstance(result_data, dict) else None),
            error_type=raw.get("failure_type"),
        )

    # ---- S6: 审计日志 ----

    _SENSITIVE_PARAM_KEYS = {"password", "token", "secret", "api_key", "apikey", "auth", "credential"}

    @classmethod
    def _mask_sensitive_params(cls, params: Dict[str, Any]) -> Dict[str, Any]:
        """对审计日志中的敏感字段脱敏。"""
        masked = {}
        for k, v in params.items():
            if any(s in k.lower() for s in cls._SENSITIVE_PARAM_KEYS):
                masked[k] = "***REDACTED***"
            elif isinstance(v, str) and len(v) > 500:
                masked[k] = v[:200] + f"...[{len(v)} chars]"
            else:
                masked[k] = v
        return masked

    # ---- C2: per-tool failure profile ----

    @staticmethod
    def _record_tool_failure_profile(ctx: ToolExecutionContext) -> None:
        """Push one event into the C2 failure-profile store.

        No-ops when ``TOOL_FAILURE_PROFILE_ENABLED=false``. Skips bookkeeping
        for events the profile cannot meaningfully attribute to the tool
        itself: schema/permission rejections (fault is upstream caller) and
        approval-pending decisions (no execution happened).
        """
        try:
            if not getattr(settings, "TOOL_FAILURE_PROFILE_ENABLED", False):
                return
            # Skip pre-execution rejections / approval gates
            if ctx.has_fatal_error or ctx.permission_result in ("ask", "deny"):
                return
            from utils.tool_failure_profile import get_tool_failure_profile_store

            store = get_tool_failure_profile_store()
            if store is None:
                return
            result = ctx.normalized_result
            success = bool(result.success) if result else False
            latency_s = ctx.stage_timings.get(ToolPipelineStage.EXECUTE.value, 0.0) or 0.0
            store.record_outcome(
                ctx.tool_name,
                success=success,
                error_type=(result.error_type if result else None),
                error_message=(result.error if result else None),
                latency_ms=int(latency_s * 1000),
            )
        except Exception as exc:  # pragma: no cover — best-effort, never block
            log_warning(f"[C2] tool failure profile record skipped: {exc}")

    def _write_audit_log(self, ctx: ToolExecutionContext, spec: Any) -> None:
        """S6: 将工具执行记录写入审计日志 data/audit/{date}.jsonl。"""
        if not getattr(settings, "AUDIT_LOG_ENABLED", True):
            return

        try:
            trust_level = spec.trust_level if (spec and hasattr(spec, "trust_level")) else "unknown"
            result = ctx.normalized_result
            now = datetime.datetime.now(datetime.timezone.utc)

            # 判定结果状态
            if ctx.has_fatal_error:
                result_status = "rejected"
            elif ctx.permission_result == "deny":
                result_status = "denied"
            elif ctx.permission_result == "ask":
                result_status = "pending_approval"
            elif result and result.success:
                result_status = "success"
            elif result and not result.success:
                result_status = "failed"
            else:
                result_status = "unknown"

            rejection_reasons = [e.message for e in ctx.stage_errors if e.fatal]

            record = {
                "timestamp": now.isoformat(),
                "tool_name": ctx.tool_name,
                "trust_level": trust_level,
                "params": self._mask_sensitive_params(ctx.effective_params),
                "result_status": result_status,
                "rejection_reason": "; ".join(rejection_reasons) if rejection_reasons else None,
                "stage_timings": ctx.stage_timings,
            }

            audit_dir = Path(getattr(settings, "DATA_DIR", "data")) / "audit"
            audit_dir.mkdir(parents=True, exist_ok=True)
            audit_file = audit_dir / f"{now.strftime('%Y-%m-%d')}.jsonl"

            with open(audit_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")

        except Exception as e:
            # 审计日志写入失败不影响主流程
            log_warning(f"[S6] Audit log write failed: {e}")

    # ---- helpers ----

    def _run_stage(self, ctx: ToolExecutionContext, stage: ToolPipelineStage, fn) -> None:
        t0 = time.monotonic()
        try:
            fn()
        except Exception as e:
            ctx.stage_errors.append(StageError(stage=stage, message=str(e), fatal=self._strict))
            if not self._strict:
                log_warning(f"[ToolPipeline] Stage {stage.value} exception for {ctx.tool_name}: {e}")
        finally:
            ctx.stage_timings[stage.value] = time.monotonic() - t0

    async def _run_stage_async(self, ctx: ToolExecutionContext, stage: ToolPipelineStage, fn) -> None:
        t0 = time.monotonic()
        try:
            await fn()
        except Exception as e:
            ctx.stage_errors.append(StageError(stage=stage, message=str(e), fatal=self._strict))
            if not self._strict:
                log_warning(f"[ToolPipeline] Stage {stage.value} exception for {ctx.tool_name}: {e}")
        finally:
            ctx.stage_timings[stage.value] = time.monotonic() - t0

    @staticmethod
    def _format_schema_error(exc: Exception) -> str:
        try:
            # jsonschema.ValidationError has rich attributes
            path = ".".join(str(p) for p in getattr(exc, "absolute_path", []))
            msg = getattr(exc, "message", str(exc))
            if path:
                return f"Field '{path}': {msg}"
            return msg
        except Exception:
            return str(exc)

    @staticmethod
    def _make_error_result(ctx: ToolExecutionContext) -> ToolResult:
        errors = [e for e in ctx.stage_errors if e.fatal]
        msg = "; ".join(e.message for e in errors) if errors else "Pipeline validation failed"
        return ToolResult(
            success=False,
            error=msg,
            error_type="PipelineValidationError",
        )

    @staticmethod
    def _build_task_dict(ctx: ToolExecutionContext, state: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "tool_name": ctx.tool_name,
            "task_type": "",
            "task_id": state.get("current_task_id", ""),
            "job_id": state.get("job_id", ""),
            "params": ctx.effective_params,
            "tool_args": copy.deepcopy(ctx.effective_params),
            "description": "",
            "risk_level": "medium",
            "execution_trace": [],
        }


# ---------------------------------------------------------------------------
# 便捷工厂
# ---------------------------------------------------------------------------

def get_tool_pipeline(*, strict_mode: Optional[bool] = None) -> ToolPipeline:
    return ToolPipeline(strict_mode=strict_mode)
