"""
Perception-gap detection: cross-check what the vision model claims to see
against what the DOM-distillation extractor produced.

The input is two independent perception channels:

* ``vision_controls``  — a structured list the vision model emits alongside
  its natural-language summary. Each entry is ``{"role", "label"}``.
* ``elements`` — the ``snapshot["elements"]`` list produced by the JS
  distillation / a11y tree merge.

A "gap" is a vision control that has no plausible match in ``elements``.
Gaps signal that DOM extraction missed something visible (hidden inputs
behind a label, custom ARIA widgets, iframes we didn't reach, etc.) and
should be reported upward so downstream layers can either trigger a
broader DOM scan or surface the gap to the decision LLM.

The matching is deliberately generic — role equivalence classes come from
the HTML / ARIA spec, not from any particular framework.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Iterable, List, Set, Tuple


# Role equivalence classes. Keys are canonical names; values are alternate
# role / tag / type strings that should match the canonical form.
# Derived from HTML + ARIA role spec — not framework-specific.
_ROLE_EQUIVALENCE: Dict[str, Set[str]] = {
    "button": {"button", "submit", "reset"},
    "link": {"link", "a", "anchor"},
    "input": {"input", "textbox", "searchbox", "textarea", "email", "password",
              "url", "tel", "number", "search", "text"},
    "checkbox": {"checkbox", "menuitemcheckbox"},
    "radio": {"radio", "menuitemradio"},
    "select": {"select", "combobox", "listbox", "dropdown"},
    "tab": {"tab"},
    "switch": {"switch", "toggle"},
    "slider": {"slider"},
    "other": {"other", "menuitem", "option", "spinbutton"},
}


def _canonical_role(role: str) -> str:
    """Collapse an arbitrary role/tag/type string to a canonical bucket.

    Unknown roles map to ``"other"``. Empty inputs also map to ``"other"``
    so a vision control with a missing role still participates in matching
    (label overlap alone can still connect it to a DOM element).
    """
    r = (role or "").strip().lower()
    if not r:
        return "other"
    for canon, variants in _ROLE_EQUIVALENCE.items():
        if r in variants or r == canon:
            return canon
    return "other"


_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def _tokenize(text: str) -> Set[str]:
    """Lowercase, unicode-normalize, split on non-word chars, drop length-1."""
    if not text:
        return set()
    normed = unicodedata.normalize("NFKC", str(text)).lower()
    return {t for t in _TOKEN_RE.findall(normed) if len(t) > 1}


def _element_label_fragments(element: Dict[str, Any]) -> Iterable[str]:
    """Every label-like string an element exposes.

    We try all of them because different frameworks put the human-readable
    affordance in different places (``text`` for buttons, ``placeholder``
    for inputs, ``label`` when an a11y linkage exists, etc.).
    """
    if not isinstance(element, dict):
        return
    for key in ("text", "label", "placeholder", "value", "aria_label", "title"):
        value = element.get(key)
        if isinstance(value, str) and value.strip():
            yield value


def _element_role_candidates(element: Dict[str, Any]) -> Set[str]:
    """Collect every role-ish identifier we might match against."""
    if not isinstance(element, dict):
        return set()
    raw = set()
    for key in ("role", "tag", "type"):
        value = element.get(key)
        if isinstance(value, str) and value.strip():
            raw.add(value.strip().lower())
    return {_canonical_role(v) for v in raw} or {"other"}


def _labels_overlap(vision_label: str, element_label: str) -> bool:
    """Return True if two labels plausibly refer to the same control.

    The rule is a token-overlap OR substring containment. Length-1 tokens
    are dropped because punctuation / single digits cause false positives.
    """
    v = (vision_label or "").strip().lower()
    e = (element_label or "").strip().lower()
    if not v or not e:
        return False
    # Substring either direction — vision often paraphrases slightly.
    if v in e or e in v:
        return True
    v_tokens = _tokenize(v)
    e_tokens = _tokenize(e)
    if not v_tokens or not e_tokens:
        return False
    return bool(v_tokens & e_tokens)


def _control_matches_element(
    control: Dict[str, str],
    element: Dict[str, Any],
) -> bool:
    """Decide whether a vision-reported control is the same thing as a
    DOM-extracted element.

    Match requires (a) compatible role bucket AND (b) label overlap.
    ``other`` / missing vision role acts as a wildcard for role — label
    overlap alone is enough in that case, since the vision model is
    allowed to be fuzzy about the exact ARIA role.
    """
    vision_role = _canonical_role(control.get("role") or "")
    vision_label = control.get("label") or ""
    if not vision_label:
        # Without a label we have no reliable signal — treat as matched to
        # avoid spurious gaps.
        return True

    element_roles = _element_role_candidates(element)
    role_compatible = (
        vision_role == "other"
        or vision_role in element_roles
        or "other" in element_roles  # element with unknown role — permissive
    )
    if not role_compatible:
        return False

    for frag in _element_label_fragments(element):
        if _labels_overlap(vision_label, frag):
            return True
    return False


def find_perception_gaps(
    vision_controls: List[Dict[str, str]],
    elements: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """Return the subset of ``vision_controls`` that couldn't be matched
    to any element in ``elements``.

    Both inputs are tolerated as arbitrary iterables — non-dict entries
    are silently skipped. Order of the returned list mirrors the input
    order so downstream logs stay deterministic.
    """
    if not vision_controls:
        return []
    elems = [e for e in (elements or []) if isinstance(e, dict)]
    gaps: List[Dict[str, str]] = []
    for control in vision_controls:
        if not isinstance(control, dict):
            continue
        role = str(control.get("role") or "").strip().lower()
        label = str(control.get("label") or "").strip()
        if not role and not label:
            continue
        if any(_control_matches_element(control, e) for e in elems):
            continue
        gaps.append({"role": role, "label": label})
    return gaps


def format_gaps_for_log(gaps: List[Dict[str, str]]) -> str:
    """Produce a single-line log-friendly representation of gaps."""
    if not gaps:
        return "none"
    parts = []
    for g in gaps[:8]:
        role = g.get("role") or "?"
        label = (g.get("label") or "").strip()
        if len(label) > 40:
            label = label[:37] + "..."
        parts.append(f"{role}:{label}")
    suffix = "" if len(gaps) <= 8 else f" (+{len(gaps) - 8} more)"
    return "; ".join(parts) + suffix


# Maximum gaps rendered into a prompt block. Anything more usually
# indicates a visual-description hallucination rather than real DOM holes;
# capping the block protects the prompt from runaway noise.
_MAX_GAPS_IN_PROMPT = 6

# Maximum label length per entry in the prompt block.
_MAX_LABEL_LEN_IN_PROMPT = 80


def format_gaps_for_prompt(gaps: List[Dict[str, str]]) -> str:
    """Render perception gaps as a neutral prompt block for decision LLMs.

    The block lists controls the vision model claims to see that have no
    counterpart in the interactive elements list. We deliberately do not
    tell the LLM what to do — we list *possible* explanations (scroll,
    iframe, lazy render, adjacent-label click) and let it choose. Emitting
    rules here would turn the agent into a rule engine, which is the
    opposite of what this codebase is trying to be.

    Returns an empty string when there are no gaps so prompt formatters
    can include the placeholder unconditionally without leaving a stray
    header.
    """
    if not gaps:
        return ""
    lines = [
        "VISION-ONLY CONTROLS — observed in the screenshot, NOT present in the interactive elements list:",
    ]
    has_multi_round = False
    for g in gaps[:_MAX_GAPS_IN_PROMPT]:
        if not isinstance(g, dict):
            continue
        role = str(g.get("role") or "?").strip().lower() or "?"
        label = str(g.get("label") or "").strip()
        if len(label) > _MAX_LABEL_LEN_IN_PROMPT:
            label = label[: _MAX_LABEL_LEN_IN_PROMPT - 3] + "..."
        # Streak suffix: only render when ≥ 2, so a fresh single-round gap
        # reads as "possibly noise" while a persistent one reads as "likely
        # real". Missing/invalid streak values render as single-round.
        try:
            streak = int(g.get("streak", 1) or 1)
        except (TypeError, ValueError):
            streak = 1
        if streak >= 2:
            has_multi_round = True
            suffix = f"  (seen {streak} rounds)"
        else:
            suffix = ""
        body = f"[{role}] {label}" if label else f"[{role}]"
        lines.append(f"- {body}{suffix}")
    extra = len(gaps) - _MAX_GAPS_IN_PROMPT
    if extra > 0:
        lines.append(f"- (+{extra} more)")
    lines.extend([
        "These controls may be off-screen (try scroll), inside an iframe or"
        " separate tab, or rendered after an action/animation. They may also"
        " be hidden inputs whose affordance is a wrapping label you can click",
        "directly. Use this as a hint — not a command.",
    ])
    if has_multi_round:
        lines.append(
            "Gaps marked with a round count have persisted across rounds — a"
            " stronger signal than single-round gaps, which can be vision noise."
        )
    return "\n".join(lines)
