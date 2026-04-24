"""
Page fingerprinting (B3).

Computes a deterministic ``page_hash`` for visually-similar pages so the
vision-description cache can recognise revisits to the same page template.

Strategy:

- ``normalize_url_path`` collapses dynamic identifiers (numeric segments,
  long hex/uuid tokens, base64-ish blobs) into placeholders such as
  ``:id``/``:hash``. The query string is dropped.
- ``compute_page_hash`` combines ``domain + normalized_path + dom_summary``
  where the DOM summary is reduced to its structural signature: the
  ``page_type`` hint plus counts of landmark roles/tags. This means two
  pages that look the same but list different items (different IDs,
  different content) collapse to the same fingerprint.

The module is dependency-free so it can be imported from anywhere.
"""
from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, Iterable
from urllib.parse import urlparse


_NUMERIC_RE = re.compile(r"^\d+$")
_HEX_RE = re.compile(r"^[0-9a-f]{16,}$", re.IGNORECASE)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_BASE64_RE = re.compile(r"^[A-Za-z0-9_\-]{24,}$")
_LANDMARK_ROLES = (
    "header",
    "nav",
    "main",
    "footer",
    "aside",
    "form",
    "search",
    "banner",
    "complementary",
    "contentinfo",
)


def normalize_url_path(url: str) -> str:
    """Return ``host + normalized_path``, stripping query/fragment.

    Each path segment is collapsed if it looks like an identifier:

    - all-digit → ``:id``
    - 36-char UUID → ``:uuid``
    - 16+ hex chars → ``:hash``
    - 24+ base64-ish chars → ``:token``

    Examples
    --------
    >>> normalize_url_path("https://shop.x.com/product/12345?ref=foo")
    'shop.x.com/product/:id'
    >>> normalize_url_path("https://x.com/u/abc-123-def-456")
    'x.com/u/abc-123-def-456'
    """
    if not url:
        return ""
    text = str(url).strip()
    if "://" not in text:
        text = "http://" + text
    try:
        parsed = urlparse(text)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower()
    raw_path = parsed.path or "/"
    segments = [seg for seg in raw_path.split("/") if seg]
    norm_segments = []
    for seg in segments:
        if _NUMERIC_RE.match(seg):
            norm_segments.append(":id")
        elif _UUID_RE.match(seg):
            norm_segments.append(":uuid")
        elif _HEX_RE.match(seg):
            norm_segments.append(":hash")
        elif _BASE64_RE.match(seg):
            norm_segments.append(":token")
        else:
            norm_segments.append(seg.lower())
    if not norm_segments:
        return host
    return host + "/" + "/".join(norm_segments)


def _structural_signature(dom_summary: Dict[str, Any]) -> str:
    """Reduce a snapshot dict to a structural signature string.

    Two kinds of inputs go into the signature:

    1. **Structural counts** — elements, cards, collections, headings. These
       are passed through :func:`_bucket`, which uses adaptive resolution:
       exact counts for small pages (so a login form changes fingerprint
       when a control appears/disappears), coarser buckets for larger pages
       (so a search result page with 50 vs 52 items still collides).

    2. **Generic form-state signals** — how many inputs are filled, how
       many controls are checked, whether any field is marked invalid,
       whether a modal is open. These signals come from standard HTML /
       ARIA attributes (``value``, ``aria-checked``, ``aria-invalid``,
       ``role=dialog``), so the signature reacts to the *state* of a page
       even when its structure is unchanged. This is what lets the vision
       cache notice that a login form is mid-fill vs freshly loaded
       without any page-type-specific rules.
    """
    if not isinstance(dom_summary, dict):
        return "empty"

    page_type = str(dom_summary.get("page_type", "") or "unknown").lower()
    page_stage = str(dom_summary.get("page_stage", "") or "").lower()

    elements = dom_summary.get("elements") or []
    cards = dom_summary.get("cards") or []
    collections = dom_summary.get("collections") or []
    headings = dom_summary.get("headings") or []

    elem_bucket = _bucket(len(elements))
    card_bucket = _bucket(len(cards))
    coll_bucket = _bucket(len(collections))
    head_bucket = _bucket(len(headings))

    role_counts = _count_landmark_roles(elements)
    role_sig = ",".join(f"{role}={role_counts.get(role, 0)}" for role in _LANDMARK_ROLES)

    state = _count_state_signals(elements)
    has_modal = bool(
        dom_summary.get("has_modal")
        or page_stage == "modal"
        or any(str(e.get("role", "") or "").lower() == "dialog" for e in elements if isinstance(e, dict))
    )

    return (
        f"pt={page_type}|ps={page_stage}|"
        f"e={elem_bucket}|c={card_bucket}|co={coll_bucket}|h={head_bucket}|"
        f"filled={state['filled']}|checked={state['checked']}|inv={state['invalid']}|"
        f"m={int(has_modal)}|"
        f"{role_sig}"
    )


def _bucket(value: int) -> str:
    """Adaptive-resolution bucket.

    Rationale: the previous scheme lumped 1-5 elements into a single bucket,
    which meant a login page with 5 controls and the same page with 6
    controls (after a validation hint appears) shared one fingerprint and
    the vision cache never refreshed. Now small pages are counted exactly;
    bucketing only kicks in once there are enough elements that one-or-two
    fluctuations are real noise rather than real state change.
    """
    if value <= 0:
        return "0"
    if value <= 10:
        return str(value)  # exact — small pages should be fingerprint-distinct
    if value <= 30:
        return f"{(value // 5) * 5}-{(value // 5) * 5 + 4}"  # 5-wide buckets
    if value <= 100:
        return f"{(value // 20) * 20}-{(value // 20) * 20 + 19}"  # 20-wide buckets
    return "100+"


def _count_state_signals(elements: Iterable[Any]) -> Dict[str, int]:
    """Aggregate generic DOM/ARIA state signals across elements.

    Only uses attributes defined by the HTML / ARIA specs — no framework
    or page-type specialisation. Callers use the counts as additional
    fingerprint dimensions so pages that share structure but differ in
    state (mid-form-fill, post-validation error, modal open) get distinct
    hashes.
    """
    filled = 0
    checked = 0
    invalid = 0
    for el in elements or ():
        if not isinstance(el, dict):
            continue
        # Filled input: any element exposing a non-empty ``value`` field.
        value = el.get("value")
        if isinstance(value, str) and value.strip():
            filled += 1
        aria_state = el.get("aria_state") or {}
        if isinstance(aria_state, dict):
            if aria_state.get("checked") is True:
                checked += 1
        form_state = el.get("form_state") or {}
        if isinstance(form_state, dict) and form_state.get("invalid"):
            invalid += 1
    return {"filled": filled, "checked": checked, "invalid": invalid}


def _count_landmark_roles(elements: Iterable[Any]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for el in elements or ():
        if not isinstance(el, dict):
            continue
        role = str(el.get("role", "") or "").lower()
        tag = str(el.get("tag", "") or "").lower()
        for landmark in _LANDMARK_ROLES:
            if role == landmark or tag == landmark:
                counts[landmark] = counts.get(landmark, 0) + 1
                break
    return counts


def compute_page_hash(url: str, dom_summary: Dict[str, Any]) -> str:
    """Return md5 hex digest of ``normalized_url + structural_signature``."""
    route = normalize_url_path(url)
    signature = _structural_signature(dom_summary or {})
    raw = f"{route}||{signature}"
    return hashlib.md5(raw.encode("utf-8", errors="ignore")).hexdigest()
