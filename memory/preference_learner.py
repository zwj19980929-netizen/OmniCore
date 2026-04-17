"""
Offline preference learning (A5).

Scans recent ``task_result`` memories and infers behavioral preferences:

- **preferred_tool**: which tool(s) the user's tasks rely on most (weighted by
  success rate).
- **common_intent**: dominant router-intent over the window.
- **active_hours**: peak hour-of-day bucket for job creation.

Preferences come with a ``confidence`` (0–1) and ``evidence_ids`` pointing
back at the memories that support them, so the feature is auditable and
the user can always see *why* a preference was learned.

The module is **read-mostly**: writing is delegated to ``MemoryManager``
via ``persist_inferred_preferences``.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from config.settings import settings
from utils.logger import log_agent_action, log_warning
from utils.text import sanitize_text, sanitize_value

if TYPE_CHECKING:
    from memory.scoped_chroma_store import ChromaMemory


_DISTILL_PROMPT_PATH = Path(settings.PROJECT_ROOT) / "prompts" / "preference_distillation.txt"


@dataclass
class PreferenceCandidate:
    key: str
    value: str
    confidence: float
    source: str = "inferred"
    evidence_ids: List[str] = field(default_factory=list)
    notes: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "confidence": round(float(self.confidence), 4),
            "source": self.source,
            "evidence_ids": list(self.evidence_ids),
            "notes": self.notes,
        }


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _within_window(meta: Dict[str, Any], cutoff: datetime) -> bool:
    ts = _parse_iso(meta.get("updated_at") or meta.get("created_at"))
    return bool(ts and ts >= cutoff)


def _hour_bucket(dt: datetime) -> str:
    if 0 <= dt.hour < 6:
        return "late_night"
    if 6 <= dt.hour < 12:
        return "morning"
    if 12 <= dt.hour < 18:
        return "afternoon"
    return "evening"


def _collect_task_records(
    store: "ChromaMemory",
    *,
    window_days: int,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Pull every ``task_result`` record created within the window."""
    cutoff = (now or datetime.now()) - timedelta(days=max(int(window_days), 1))
    try:
        raw = store._collection.get()
    except Exception as exc:
        log_warning(f"PreferenceLearner: collection get failed: {exc}")
        return []
    ids = raw.get("ids") or []
    documents = raw.get("documents") or []
    metadatas = raw.get("metadatas") or []
    records: List[Dict[str, Any]] = []
    for idx, memory_id in enumerate(ids):
        meta = sanitize_value(metadatas[idx] if idx < len(metadatas) else {})
        if sanitize_text(str(meta.get("type", "") or "")) != "task_result":
            continue
        if not _within_window(meta, cutoff):
            continue
        records.append(
            {
                "id": sanitize_text(memory_id),
                "content": sanitize_text(str(documents[idx] if idx < len(documents) else "")),
                "metadata": meta,
            }
        )
    return records


def _analyze_tool_usage(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Rule layer: per-tool success-weighted counts."""
    success_counts: Dict[str, int] = defaultdict(int)
    total_counts: Dict[str, int] = defaultdict(int)
    tool_to_ids: Dict[str, List[str]] = defaultdict(list)
    for rec in records:
        meta = rec.get("metadata") or {}
        succ = bool(meta.get("success", False))
        seq_raw = sanitize_text(str(meta.get("tool_sequence", "") or ""))
        tools = [t for t in seq_raw.split(",") if t]
        for tool in tools:
            total_counts[tool] += 1
            if succ:
                success_counts[tool] += 1
            if len(tool_to_ids[tool]) < 10:
                tool_to_ids[tool].append(rec["id"])
    ranking = []
    for tool, total in total_counts.items():
        rate = success_counts[tool] / total if total else 0.0
        score = success_counts[tool] + 0.5 * (total - success_counts[tool])
        ranking.append(
            {
                "tool": tool,
                "uses": total,
                "success": success_counts[tool],
                "rate": rate,
                "score": score,
                "evidence_ids": tool_to_ids[tool],
            }
        )
    ranking.sort(key=lambda item: item["score"], reverse=True)
    return {"ranking": ranking, "sample_size": len(records)}


def _analyze_intents(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    counter: Counter = Counter()
    intent_to_ids: Dict[str, List[str]] = defaultdict(list)
    for rec in records:
        meta = rec.get("metadata") or {}
        intent = sanitize_text(str(meta.get("intent", "") or ""))
        if not intent:
            continue
        counter[intent] += 1
        if len(intent_to_ids[intent]) < 10:
            intent_to_ids[intent].append(rec["id"])
    ranking = [
        {"intent": intent, "uses": count, "evidence_ids": intent_to_ids[intent]}
        for intent, count in counter.most_common()
    ]
    return {"ranking": ranking, "sample_size": len(records)}


def _analyze_hours(records: List[Dict[str, Any]]) -> Dict[str, Any]:
    buckets: Counter = Counter()
    bucket_to_ids: Dict[str, List[str]] = defaultdict(list)
    for rec in records:
        meta = rec.get("metadata") or {}
        ts = _parse_iso(meta.get("created_at"))
        if ts is None:
            continue
        bucket = _hour_bucket(ts)
        buckets[bucket] += 1
        if len(bucket_to_ids[bucket]) < 10:
            bucket_to_ids[bucket].append(rec["id"])
    ranking = [
        {"bucket": bucket, "uses": count, "evidence_ids": bucket_to_ids[bucket]}
        for bucket, count in buckets.most_common()
    ]
    return {"ranking": ranking, "sample_size": sum(buckets.values())}


def _load_distill_prompt() -> str:
    try:
        return _DISTILL_PROMPT_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""


def _parse_llm_json(content: str) -> Optional[Dict[str, Any]]:
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        lines = [l for l in text.split("\n") if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except (json.JSONDecodeError, TypeError):
                return None
    return None


def _distill_with_llm(
    rule_candidates: List[PreferenceCandidate],
    rule_stats: Dict[str, Any],
    sample_records: List[Dict[str, Any]],
    *,
    min_confidence: float,
    model: str = "",
) -> List[PreferenceCandidate]:
    """LLM layer (A5 final): ask a cheap model to name non-obvious preferences.

    Rule-layer output is already precise — this step only adds what pure
    counting cannot capture (e.g. "the user prefers markdown summaries over
    raw tables"). Runs only when ``PREFERENCE_LEARNING_MODEL`` is set.
    Failures are swallowed; the rule layer result is authoritative.
    """
    prompt = _load_distill_prompt()
    if not prompt:
        return []
    existing_keys = {cand.key for cand in rule_candidates}
    payload = {
        "rule_candidates": [cand.as_dict() for cand in rule_candidates],
        "rule_stats": {
            "tools": rule_stats.get("tools", {}).get("ranking", [])[:5],
            "intents": rule_stats.get("intents", {}).get("ranking", [])[:5],
            "hours": rule_stats.get("hours", {}).get("ranking", []),
        },
        "sample_records": [
            {
                "id": rec["id"],
                "input": sanitize_text(
                    str(
                        (rec.get("metadata") or {}).get("task_description")
                        or rec.get("content", "")
                    )
                )[:240],
                "intent": sanitize_text(str((rec.get("metadata") or {}).get("intent", "") or "")),
                "success": bool((rec.get("metadata") or {}).get("success", False)),
            }
            for rec in sample_records[:10]
        ],
    }
    user_message = (
        "请基于规则层结果和原始样本，归纳出规则层遗漏的偏好 (若无则输出空数组):\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
    )
    try:
        from core.llm import LLMClient
        llm = LLMClient(model=model) if model else LLMClient()
        response = llm.chat_with_system(
            system_prompt=prompt,
            user_message=user_message,
            temperature=0.2,
            json_mode=True,
        )
    except Exception as exc:
        log_warning(f"PreferenceLearner LLM distill failed: {exc}")
        return []

    parsed = _parse_llm_json(response.content if response else "")
    if not parsed:
        return []

    extras: List[PreferenceCandidate] = []
    for entry in parsed.get("preferences") or []:
        if not isinstance(entry, dict):
            continue
        key = sanitize_text(str(entry.get("key", "") or ""))
        value = sanitize_text(str(entry.get("value", "") or ""))
        try:
            confidence = float(entry.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        if not key or not value:
            continue
        if key in existing_keys:
            continue  # never overwrite rule-layer findings
        if confidence < min_confidence:
            continue
        extras.append(
            PreferenceCandidate(
                key=key,
                value=value,
                confidence=min(1.0, confidence),
                source="llm_inferred",
                evidence_ids=[],
                notes=sanitize_text(str(entry.get("notes", "") or ""))[:140],
            )
        )
    if extras:
        log_agent_action(
            "PreferenceLearner",
            "LLM distilled",
            f"{len(extras)} extra candidates",
        )
    return extras


def infer_preferences(
    store: "ChromaMemory",
    *,
    window_days: Optional[int] = None,
    min_samples: Optional[int] = None,
    min_confidence: Optional[float] = None,
    now: Optional[datetime] = None,
    use_llm: Optional[bool] = None,
) -> List[PreferenceCandidate]:
    """Derive preferences from the window's task outcomes.

    Returns an empty list when samples are below ``min_samples`` — we'd
    rather learn nothing than learn something wrong.
    """
    window = int(window_days if window_days is not None else settings.PREFERENCE_LEARNING_WINDOW_DAYS)
    min_samples_eff = int(
        min_samples if min_samples is not None else settings.PREFERENCE_LEARNING_MIN_SAMPLES
    )
    min_conf = float(
        min_confidence if min_confidence is not None else settings.PREFERENCE_LEARNING_MIN_CONFIDENCE
    )

    records = _collect_task_records(store, window_days=window, now=now)
    if len(records) < min_samples_eff:
        return []

    candidates: List[PreferenceCandidate] = []

    tool_stats = _analyze_tool_usage(records)
    if tool_stats["ranking"]:
        top = tool_stats["ranking"][0]
        if top["uses"] >= min_samples_eff:
            confidence = min(1.0, top["uses"] / max(len(records), 1))
            if confidence >= min_conf:
                candidates.append(
                    PreferenceCandidate(
                        key="preferred_tool",
                        value=top["tool"],
                        confidence=confidence,
                        evidence_ids=top["evidence_ids"],
                        notes=f"{top['uses']} uses / {len(records)} jobs, success_rate={top['rate']:.0%}",
                    )
                )

    intent_stats = _analyze_intents(records)
    if intent_stats["ranking"]:
        top_intent = intent_stats["ranking"][0]
        if top_intent["uses"] >= min_samples_eff:
            share = top_intent["uses"] / max(len(records), 1)
            if share >= min_conf:
                candidates.append(
                    PreferenceCandidate(
                        key="common_intent",
                        value=top_intent["intent"],
                        confidence=min(1.0, share),
                        evidence_ids=top_intent["evidence_ids"],
                        notes=f"{top_intent['uses']} / {len(records)} jobs",
                    )
                )

    hour_stats = _analyze_hours(records)
    if hour_stats["ranking"]:
        top_bucket = hour_stats["ranking"][0]
        total = max(hour_stats["sample_size"], 1)
        share = top_bucket["uses"] / total
        if share >= min_conf:
            candidates.append(
                PreferenceCandidate(
                    key="active_hours",
                    value=top_bucket["bucket"],
                    confidence=min(1.0, share),
                    evidence_ids=top_bucket["evidence_ids"],
                    notes=f"{top_bucket['uses']}/{total} jobs in {top_bucket['bucket']}",
                )
            )

    # Optional LLM layer: only when explicitly requested via argument or via
    # ``PREFERENCE_LEARNING_MODEL`` being set in settings. The rule layer is
    # authoritative; this step only adds non-obvious patterns.
    want_llm = use_llm if use_llm is not None else bool(settings.PREFERENCE_LEARNING_MODEL)
    if want_llm and records:
        extras = _distill_with_llm(
            candidates,
            rule_stats={"tools": tool_stats, "intents": intent_stats, "hours": hour_stats},
            sample_records=records,
            min_confidence=min_conf,
            model=str(settings.PREFERENCE_LEARNING_MODEL or ""),
        )
        candidates.extend(extras)

    log_agent_action(
        "PreferenceLearner",
        "Inferred",
        f"{len(candidates)} candidates from {len(records)} records",
    )
    return candidates


# ----------------------------------------------------------------------
# Auto-trigger gating (A5 enablement)
# ----------------------------------------------------------------------

def _gate_state_path() -> Path:
    return Path(settings.DATA_DIR) / "preference_learn_state.json"


def _read_last_run_at(path: Optional[Path] = None) -> Optional[datetime]:
    p = path or _gate_state_path()
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
        ts = data.get("last_run_at")
        if not ts:
            return None
        return datetime.fromisoformat(str(ts))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def _write_last_run_at(dt: datetime, path: Optional[Path] = None) -> None:
    p = path or _gate_state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"last_run_at": dt.isoformat(timespec="seconds")}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        log_warning(f"PreferenceLearner gate write failed: {exc}")


def should_run_now(
    *,
    min_interval_hours: Optional[int] = None,
    now: Optional[datetime] = None,
    state_path: Optional[Path] = None,
) -> bool:
    """Gate check: has enough time passed since the last auto-run?

    Returns True iff the interval has elapsed (or no prior run exists).
    Used by ``maybe_run_learner`` so callers can short-circuit cheaply
    without instantiating the store.
    """
    if not settings.PREFERENCE_LEARNING_ENABLED:
        return False
    reference = now or datetime.now()
    interval = int(
        min_interval_hours
        if min_interval_hours is not None
        else settings.PREFERENCE_LEARNING_MIN_INTERVAL_HOURS
    )
    last_run = _read_last_run_at(state_path)
    if last_run is None:
        return True
    return reference - last_run >= timedelta(hours=max(interval, 1))


def maybe_run_learner(
    manager: Any,
    *,
    now: Optional[datetime] = None,
    state_path: Optional[Path] = None,
) -> int:
    """Auto-trigger the learner when gating conditions allow.

    Safe to call from hot paths: if disabled, not yet due, or missing the
    manager/chroma, it silently returns 0 and never raises.

    Returns the number of persisted preferences (0 when skipped).
    """
    if manager is None or getattr(manager, "chroma_memory", None) is None:
        return 0
    if not should_run_now(now=now, state_path=state_path):
        return 0
    reference = now or datetime.now()
    try:
        candidates = infer_preferences(manager.chroma_memory, now=reference)
    except Exception as exc:
        log_warning(f"PreferenceLearner auto-run failed: {exc}")
        _write_last_run_at(reference, state_path)  # still mark to avoid hot-loop
        return 0
    written = 0
    if candidates:
        try:
            ids = manager.persist_inferred_preferences(candidates)
            written = len(ids)
        except Exception as exc:
            log_warning(f"PreferenceLearner persist failed: {exc}")
    _write_last_run_at(reference, state_path)
    if written:
        log_agent_action("PreferenceLearner", "Auto-run", f"persisted {written}")
    return written
