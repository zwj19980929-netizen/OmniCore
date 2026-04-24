"""
Cross-round tracking for perception gaps.

A single observe() produces a per-round ``perception_gaps`` list: controls
the vision model claims to see that don't appear in the DOM element list.
That list has no memory — a hallucinated gap on round 1 looks identical
to a real gap that's been there for the whole task.

``GapTracker`` accumulates gap signals across rounds:

* Each call to :meth:`update` with the current round's gaps bumps the
  consecutive-rounds counter for each gap that persisted and drops
  counters for gaps that disappeared.
* Navigation to a different page (URL template changes) resets the state
  — gaps only have meaning within a single page context.
* :meth:`annotate` returns the input gaps with a ``streak`` field added,
  which downstream prompt formatters use to highlight gaps that have
  appeared multiple rounds in a row (stronger signal than one-shot
  gaps that are likely vision hallucinations).

The tracker is intentionally unaware of what should happen when streaks
grow — it only produces the counter. Whether a run-3 gap means "scroll
more aggressively" or "give up on this control" is for the decision LLM
to judge.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Tuple

from utils.page_fingerprint import normalize_url_path
from utils.perception_gap import _canonical_role, _tokenize


GapKey = Tuple[str, str]


def _normalize_label(label: str) -> str:
    """Collapse a label to a whitespace-joined sorted-token form.

    Rationale: the default-tier vision model might emit
    ``"I agree to the privacy policy"`` and the HIGH-tier model might
    emit ``"I have read and agree to the Privacy Policy"`` — token-level
    overlap identifies both as the same control. Sorting removes word-
    order sensitivity across tiers / languages.

    Tokens of length ≤ 1 are dropped because they don't identify anything
    and would collide across unrelated gaps.
    """
    tokens = sorted(_tokenize(label))
    return " ".join(tokens)


def make_gap_key(gap: Dict[str, Any]) -> GapKey:
    """Canonical key for a gap dict: ``(role_bucket, token_set_string)``.

    Returns ``("", "")`` for malformed gaps — callers that see the empty
    key should skip the entry rather than record it. "Malformed" means
    the raw gap has neither a role nor a label; canonical-role and
    normalized-label transforms happen *after* that check so truly empty
    gaps don't get promoted to ``("other", "")`` and recorded forever.
    """
    if not isinstance(gap, dict):
        return ("", "")
    raw_role = str(gap.get("role") or "").strip()
    raw_label = str(gap.get("label") or "").strip()
    if not raw_role and not raw_label:
        return ("", "")
    return (_canonical_role(raw_role), _normalize_label(raw_label))


class GapTracker:
    """Tracks consecutive-round counts for perception gaps on a page."""

    def __init__(self) -> None:
        self._streaks: Dict[GapKey, int] = {}
        self._last_url_template: str = ""

    def reset(self) -> None:
        """Drop all state. Call at the start of a new task."""
        self._streaks.clear()
        self._last_url_template = ""

    def update(self, gaps: Iterable[Dict[str, Any]], url: str) -> None:
        """Advance one round.

        * Drops gaps that did NOT reappear this round (streak broken).
        * Bumps every gap that appeared this round — new ones start at 1,
          returning ones accumulate off their last value.
        * Clears everything when the URL template changes: gap meaning is
          page-scoped, so cross-page carryover would be misleading.
        """
        template = normalize_url_path(url or "")
        if template != self._last_url_template:
            self._streaks.clear()
            self._last_url_template = template

        current_keys = set()
        for gap in gaps or ():
            key = make_gap_key(gap)
            if key == ("", ""):
                continue
            current_keys.add(key)

        # Drop gaps that didn't reappear.
        for stale_key in list(self._streaks.keys()):
            if stale_key not in current_keys:
                del self._streaks[stale_key]

        # Bump surviving + new gaps.
        for key in current_keys:
            self._streaks[key] = self._streaks.get(key, 0) + 1

    def annotate(self, gaps: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Return a copy of ``gaps`` with a ``streak`` integer on each entry.

        Gaps not in the tracker (never seen via ``update``) receive
        ``streak=1`` so the caller can treat them uniformly — calling
        ``annotate`` without a prior ``update`` is a programming error but
        shouldn't crash.
        """
        out: List[Dict[str, Any]] = []
        for gap in gaps or ():
            if not isinstance(gap, dict):
                continue
            key = make_gap_key(gap)
            streak = self._streaks.get(key, 1)
            annotated = dict(gap)
            annotated["streak"] = int(streak)
            out.append(annotated)
        return out

    def streak_for(self, gap: Dict[str, Any]) -> int:
        """Read-only accessor for a single gap's current streak (0 if absent)."""
        return self._streaks.get(make_gap_key(gap), 0)

    def snapshot(self) -> Dict[GapKey, int]:
        """Debug view of the internal counter map."""
        return dict(self._streaks)
