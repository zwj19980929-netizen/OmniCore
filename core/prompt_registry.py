"""
OmniCore Prompt Section Registry (S1)

将 system prompt 从字符串拼接升级为 section 化管理，支持：
- 按 section 注册/toggle/排序
- 静态/动态分离（cacheable 标记）
- 单 section token 预算截断
- 总 token 预算管理（低优先级 section 自动关闭）
- token 分布可观测（debug 报告）

参考：Claude Code 的 getSystemPrompt() → string[] 设计
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("omnicore.prompt_registry")

# ---------------------------------------------------------------------------
# Token 计数
# ---------------------------------------------------------------------------
_tiktoken_encoder = None
_tiktoken_load_failed = False


def _count_tokens(text: str) -> int:
    """Count tokens using tiktoken (cl100k_base) with fallback to char/4."""
    global _tiktoken_encoder, _tiktoken_load_failed
    if not _tiktoken_load_failed and _tiktoken_encoder is None:
        try:
            import tiktoken
            _tiktoken_encoder = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _tiktoken_load_failed = True
    if _tiktoken_encoder is not None:
        return len(_tiktoken_encoder.encode(text))
    # fallback: 1 token ≈ 4 chars (中英文混合场景偏保守)
    return max(1, len(text) // 4)


# ---------------------------------------------------------------------------
# PromptSection
# ---------------------------------------------------------------------------
DYNAMIC_BOUNDARY = "--- DYNAMIC BOUNDARY ---"


@dataclass
class PromptSection:
    """A named, independently manageable section of the system prompt."""

    name: str
    content: str
    cacheable: bool = True
    enabled: bool = True
    priority: int = 50
    max_tokens: Optional[int] = None

    # ---- derived (lazily computed) ----
    _token_count: Optional[int] = field(default=None, repr=False, compare=False)

    @property
    def token_count(self) -> int:
        if self._token_count is None:
            self._token_count = _count_tokens(self.content)
        return self._token_count

    def invalidate_cache(self) -> None:
        """Call when content changes to recompute token count."""
        self._token_count = None


# ---------------------------------------------------------------------------
# PromptRegistry
# ---------------------------------------------------------------------------

class PromptRegistry:
    """
    Registry that assembles system prompts from ordered sections.

    Usage::

        reg = PromptRegistry(total_budget=4000)
        reg.register(PromptSection(name="identity", content="你是…", cacheable=True, priority=100))
        reg.register(PromptSection(name="rules", content="规则…", cacheable=True, priority=90))
        reg.register(PromptSection(name="tools", content=dynamic_str, cacheable=False, priority=50))
        prompt = reg.render()
    """

    def __init__(self, total_budget: int = 0):
        """
        Args:
            total_budget: Total token budget for the assembled prompt.
                          0 means no limit.
        """
        self._sections: list[PromptSection] = []
        self.total_budget = total_budget

    # ---- registration ----

    def register(self, section: PromptSection) -> PromptRegistry:
        """Register a section. Returns self for chaining."""
        self._sections.append(section)
        return self

    def register_many(self, sections: list[PromptSection]) -> PromptRegistry:
        self._sections.extend(sections)
        return self

    # ---- query ----

    def get_section(self, name: str) -> Optional[PromptSection]:
        for s in self._sections:
            if s.name == name:
                return s
        return None

    def get_sections(self, *, enabled_only: bool = True, cacheable: Optional[bool] = None) -> list[PromptSection]:
        result = []
        for s in self._sections:
            if enabled_only and not s.enabled:
                continue
            if cacheable is not None and s.cacheable != cacheable:
                continue
            result.append(s)
        return result

    # ---- toggle ----

    def enable(self, name: str) -> None:
        s = self.get_section(name)
        if s:
            s.enabled = True

    def disable(self, name: str) -> None:
        s = self.get_section(name)
        if s:
            s.enabled = False

    # ---- render ----

    def render(self, *, include_boundary: bool = True) -> str:
        """
        Assemble enabled sections into a single prompt string.

        Order: cacheable (static) sections first, then non-cacheable (dynamic),
        separated by DYNAMIC_BOUNDARY marker.

        If total_budget > 0, sections exceeding their individual max_tokens are
        truncated, and if the total still exceeds budget, lowest-priority sections
        are disabled.
        """
        enabled = [s for s in self._sections if s.enabled]

        # --- per-section truncation ---
        for s in enabled:
            if s.max_tokens and s.token_count > s.max_tokens:
                s.content = self._truncate_content(s.content, s.max_tokens)
                s.invalidate_cache()
                logger.debug(f"Section [{s.name}] truncated to {s.max_tokens} tokens")

        # --- total budget enforcement ---
        if self.total_budget > 0:
            total = sum(s.token_count for s in enabled)
            if total > self.total_budget:
                # disable lowest-priority sections until within budget
                by_priority = sorted(enabled, key=lambda s: s.priority)
                for s in by_priority:
                    if total <= self.total_budget:
                        break
                    total -= s.token_count
                    s.enabled = False
                    logger.debug(
                        f"Section [{s.name}] (priority={s.priority}) disabled "
                        f"to meet total budget {self.total_budget}"
                    )
                enabled = [s for s in enabled if s.enabled]

        # --- assemble ---
        static_parts = [s.content for s in enabled if s.cacheable]
        dynamic_parts = [s.content for s in enabled if not s.cacheable]

        parts = []
        if static_parts:
            parts.append("\n\n".join(static_parts))
        if dynamic_parts:
            if include_boundary and static_parts:
                parts.append(DYNAMIC_BOUNDARY)
            parts.append("\n\n".join(dynamic_parts))

        return "\n\n".join(parts)

    # ---- observability ----

    def token_report(self) -> list[dict]:
        """
        Return a per-section token report.

        Each entry: {name, token_count, cacheable, enabled, priority, pct}
        """
        enabled = [s for s in self._sections if s.enabled]
        total = sum(s.token_count for s in enabled) or 1
        report = []
        for s in self._sections:
            report.append({
                "name": s.name,
                "token_count": s.token_count,
                "cacheable": s.cacheable,
                "enabled": s.enabled,
                "priority": s.priority,
                "pct": round(s.token_count / total * 100, 1) if s.enabled else 0.0,
            })
        return report

    def log_report(self) -> None:
        """Log token report at debug level."""
        report = self.token_report()
        lines = ["Prompt Section Token Report:"]
        for r in report:
            status = "ON" if r["enabled"] else "OFF"
            cache = "C" if r["cacheable"] else "D"
            lines.append(
                f"  [{status}][{cache}] {r['name']:30s} "
                f"{r['token_count']:5d} tokens ({r['pct']:5.1f}%) "
                f"priority={r['priority']}"
            )
        total = sum(r["token_count"] for r in report if r["enabled"])
        lines.append(f"  Total (enabled): {total} tokens")
        if self.total_budget > 0:
            lines.append(f"  Budget: {self.total_budget} tokens")
        logger.debug("\n".join(lines))

    # ---- internal ----

    @staticmethod
    def _truncate_content(content: str, max_tokens: int) -> str:
        """Truncate content to approximately max_tokens, keeping head portion."""
        # Rough estimation: keep head chars proportional to token budget
        current = _count_tokens(content)
        if current <= max_tokens:
            return content
        ratio = max_tokens / current
        # Keep 95% of estimated chars to leave room for marker
        keep_chars = int(len(content) * ratio * 0.95)
        return content[:keep_chars] + "\n[…truncated]"


# ---------------------------------------------------------------------------
# Convenience: single-section prompt (for simple nodes like critic/replanner)
# ---------------------------------------------------------------------------

def build_single_section_prompt(
    name: str,
    content: str,
    *,
    cacheable: bool = True,
    debug: bool = False,
) -> str:
    """Build a prompt through PromptRegistry for a node that has only one section.

    This keeps even simple nodes observable: token_report() is logged when
    DEBUG_PROMPT is enabled.
    """
    reg = PromptRegistry()
    reg.register(PromptSection(name=name, content=content, cacheable=cacheable))
    if debug:
        reg.log_report()
    return reg.render(include_boundary=False)
