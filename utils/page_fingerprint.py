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
    """Reduce a snapshot dict to a stable structural signature string.

    We intentionally avoid hashing element text — two pages that share
    layout but differ in content should collide.
    """
    if not isinstance(dom_summary, dict):
        return "empty"

    page_type = str(dom_summary.get("page_type", "") or "unknown").lower()
    page_stage = str(dom_summary.get("page_stage", "") or "").lower()

    elements = dom_summary.get("elements") or []
    cards = dom_summary.get("cards") or []
    collections = dom_summary.get("collections") or []
    headings = dom_summary.get("headings") or []

    # Bucket element counts so small fluctuations (1-2 elements) don't
    # invalidate the fingerprint.
    elem_bucket = _bucket(len(elements))
    card_bucket = _bucket(len(cards))
    coll_bucket = _bucket(len(collections))
    head_bucket = _bucket(len(headings))

    role_counts = _count_landmark_roles(elements)
    role_sig = ",".join(f"{role}={role_counts.get(role, 0)}" for role in _LANDMARK_ROLES)

    return (
        f"pt={page_type}|ps={page_stage}|"
        f"e={elem_bucket}|c={card_bucket}|co={coll_bucket}|h={head_bucket}|"
        f"{role_sig}"
    )


def _bucket(value: int) -> str:
    """Coarse bucket — keeps fingerprint stable under small DOM jitter."""
    if value <= 0:
        return "0"
    if value <= 5:
        return "1-5"
    if value <= 15:
        return "6-15"
    if value <= 40:
        return "16-40"
    if value <= 100:
        return "41-100"
    return "100+"


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
