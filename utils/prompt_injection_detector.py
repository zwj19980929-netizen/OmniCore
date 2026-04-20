"""
Prompt Injection 启发式检测 + 隔离包裹工具(E1).

入口:
- detect(text) -> DetectionResult            纯检测,不副作用
- wrap_untrusted(text, source=...) -> str    检测 + 落事件 + 返回 <UNTRUSTED> 包裹文本

设计要点:
- 默认 PROMPT_INJECTION_DETECT_ENABLED=true,启发式零成本(纯正则)
- 命中即标记并写 data/security_events.jsonl(只记 hash + preview,不存原文)
- 幂等:已含 <UNTRUSTED 前缀直接返回原文(避免重复包裹)
- BLOCK_ON_HIGH=true 时 risk=high 抛 PromptInjectionBlocked
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal


RiskLevel = Literal["none", "low", "medium", "high"]


# (rule_name, compiled regex, is_high_severity)
_RULES: list[tuple[str, re.Pattern[str], bool]] = [
    # 角色切换 / 忽略上文
    ("ignore_previous", re.compile(r"\bignore\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions?|prompts?|rules?)\b", re.I), True),
    ("disregard_above", re.compile(r"\bdisregard\s+(?:the\s+)?(?:above|previous)\b", re.I), True),
    ("forget_role",     re.compile(r"\bforget\s+(?:your|all)\s+(?:instructions?|role|rules?|previous)\b", re.I), True),
    ("override_system", re.compile(r"\boverride\s+(?:the\s+)?system\s+(?:prompt|instructions?)\b", re.I), True),
    ("new_instructions",re.compile(r"\bnew\s+instructions?\s*[:：]", re.I), True),
    # 系统消息伪造
    ("fake_system",     re.compile(r"(?:^|\n)\s*system\s*[:：]\s", re.I), True),
    ("im_start_system", re.compile(r"<\|im_start\|>\s*system", re.I), True),
    ("md_system",       re.compile(r"(?:^|\n)#{2,3}\s*system\b", re.I), True),
    # Tool 注入(中等;命中 + 任一其他规则升 high)
    ("tool_call",       re.compile(r"\bcall\s+(?:tool|function)\s+[a-z_][a-z0-9_]*", re.I), False),
    ("exec_dangerous",  re.compile(r"\bexec(?:ute)?\b[^\n]{0,40}\b(?:rm\s|sudo\s|curl\s|wget\s|chmod\s)", re.I), False),
    # 越狱
    ("jailbreak_dan",   re.compile(r"\b(?:do\s+anything\s+now|jailbreak|DAN\s+mode)\b", re.I), False),
    ("pretend_role",    re.compile(r"\bpretend\s+(?:you\s+are|to\s+be)\b", re.I), False),
    # 敏感文件路径
    ("ssh_key",         re.compile(r"~/\.ssh/|/\.ssh/id_rsa", re.I), False),
    ("etc_passwd",      re.compile(r"/etc/(?:passwd|shadow)\b", re.I), False),
    ("aws_creds",       re.compile(r"\.aws/credentials\b", re.I), False),
]


@dataclass
class DetectionResult:
    risk_level: RiskLevel = "none"
    hits: list[str] = field(default_factory=list)
    sampled_by_llm: bool = False


class PromptInjectionBlocked(Exception):
    """BLOCK_ON_HIGH=true 且检测到 high 风险时抛出."""

    def __init__(self, source: str, hits: list[str]):
        super().__init__(f"prompt injection blocked source={source} hits={hits}")
        self.source = source
        self.hits = hits


_event_lock = threading.Lock()


def _detect_enabled() -> bool:
    from config.settings import settings as _s
    return bool(getattr(_s, "PROMPT_INJECTION_DETECT_ENABLED", True))


def _block_on_high() -> bool:
    from config.settings import settings as _s
    return bool(getattr(_s, "PROMPT_INJECTION_BLOCK_ON_HIGH", False))


def _event_log_path() -> Path:
    from config.settings import settings as _s
    raw = getattr(_s, "PROMPT_INJECTION_EVENT_LOG", "data/security_events.jsonl")
    p = Path(raw)
    if not p.is_absolute():
        p = Path(getattr(_s, "PROJECT_ROOT", Path.cwd())) / p
    return p


def detect(text: str) -> DetectionResult:
    """启发式检测.text 为空或非字符串返回 none."""
    if not text or not isinstance(text, str):
        return DetectionResult()
    hits: list[str] = []
    high_hit = False
    for name, pattern, is_high in _RULES:
        if pattern.search(text):
            hits.append(name)
            if is_high:
                high_hit = True
    if not hits:
        return DetectionResult()
    if high_hit:
        risk: RiskLevel = "high"
    elif len(hits) >= 2:
        risk = "high"
    else:
        risk = "medium"
    return DetectionResult(risk_level=risk, hits=hits)


def _record_event(source: str, result: DetectionResult, text: str) -> None:
    """落 jsonl;不存原文,只存 sha256 + 200 字 preview."""
    try:
        path = _event_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        text_hash = "sha256:" + hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()[:32]
        record = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": source,
            "risk": result.risk_level,
            "hits": result.hits,
            "text_hash": text_hash,
            "preview": text[:200],
        }
        line = json.dumps(record, ensure_ascii=False)
        with _event_lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        # 安全日志失败绝不能影响主流程
        pass


def wrap_untrusted(
    text: str,
    source: str = "external",
    *,
    detect_enabled: bool | None = None,
) -> str:
    """检测 + 落事件 + 用 <UNTRUSTED source="..."> 包裹返回.

    - 空文本直接返回原值
    - detector 关 → 直接包裹(不检测、不落事件),让 prompt 头部声明仍生效
    - 已含 <UNTRUSTED 前缀 → 幂等返回原文
    - risk=high 且 BLOCK_ON_HIGH=true → 抛 PromptInjectionBlocked
    """
    if not text:
        return text
    if not isinstance(text, str):
        text = str(text)
    if "<UNTRUSTED" in text[:64]:
        return text

    enabled = _detect_enabled() if detect_enabled is None else bool(detect_enabled)
    if enabled:
        result = detect(text)
        if result.risk_level != "none":
            _record_event(source, result, text)
            if result.risk_level == "high" and _block_on_high():
                raise PromptInjectionBlocked(source, result.hits)

    safe_source = re.sub(r'[^a-zA-Z0-9._-]', '_', source)[:64] or "external"
    return f'<UNTRUSTED source="{safe_source}">\n{text}\n</UNTRUSTED>'


def wrap_many(
    items: Iterable[str],
    source: str = "external",
) -> list[str]:
    """批量包裹.None / 空保持原值."""
    out: list[str] = []
    for item in items:
        if not item:
            out.append(item)
        else:
            out.append(wrap_untrusted(item, source=source))
    return out
