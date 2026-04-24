"""
Unit tests for utils/page_fingerprint.py (B3).
"""
from __future__ import annotations

from utils.page_fingerprint import (
    compute_page_hash,
    normalize_url_path,
)


# ----------------------------------------------------------------------
# normalize_url_path
# ----------------------------------------------------------------------


class TestNormalizeUrlPath:
    def test_strips_query_and_fragment(self):
        assert normalize_url_path("https://x.com/a/b?q=1#top") == "x.com/a/b"

    def test_numeric_segment_to_id(self):
        assert normalize_url_path("https://shop.x.com/product/12345") == "shop.x.com/product/:id"

    def test_uuid_segment(self):
        url = "https://x.com/u/550e8400-e29b-41d4-a716-446655440000"
        assert normalize_url_path(url) == "x.com/u/:uuid"

    def test_long_hex_to_hash(self):
        url = "https://x.com/r/abcdef0123456789abcdef0123456789"
        assert normalize_url_path(url) == "x.com/r/:hash"

    def test_short_alpha_segment_kept(self):
        # 3 chars, not numeric, not hex → kept as-is
        assert normalize_url_path("https://x.com/abc/def") == "x.com/abc/def"

    def test_lowercases_host_and_text(self):
        assert normalize_url_path("HTTPS://X.COM/Foo/Bar") == "x.com/foo/bar"

    def test_empty_returns_empty(self):
        assert normalize_url_path("") == ""

    def test_root_path(self):
        assert normalize_url_path("https://x.com/") == "x.com"

    def test_no_scheme_assumed_http(self):
        assert normalize_url_path("x.com/foo") == "x.com/foo"


# ----------------------------------------------------------------------
# compute_page_hash
# ----------------------------------------------------------------------


def _snapshot(*, page_type="list", page_stage="ready", n_elements=20, n_cards=8, n_collections=2,
              landmarks=("header", "nav", "main", "footer")):
    elements = []
    for landmark in landmarks:
        elements.append({"role": landmark, "tag": landmark})
    # filler elements (links/buttons)
    for i in range(max(n_elements - len(landmarks), 0)):
        elements.append({"role": "link", "tag": "a", "text": f"link {i}"})
    return {
        "page_type": page_type,
        "page_stage": page_stage,
        "elements": elements,
        "cards": [{"title": f"card {i}"} for i in range(n_cards)],
        "collections": [{"kind": "list"} for _ in range(n_collections)],
        "headings": [],
    }


class TestComputePageHash:
    def test_same_template_different_id_collide(self):
        snap = _snapshot()
        h1 = compute_page_hash("https://shop.x.com/product/123", snap)
        h2 = compute_page_hash("https://shop.x.com/product/456", snap)
        assert h1 == h2

    def test_different_domain_diverges(self):
        snap = _snapshot()
        h1 = compute_page_hash("https://shop.a.com/product/123", snap)
        h2 = compute_page_hash("https://shop.b.com/product/123", snap)
        assert h1 != h2

    def test_different_path_diverges(self):
        snap = _snapshot()
        h1 = compute_page_hash("https://x.com/product/1", snap)
        h2 = compute_page_hash("https://x.com/category/1", snap)
        assert h1 != h2

    def test_dom_structure_change_diverges(self):
        snap_a = _snapshot(landmarks=("header", "nav", "main", "footer"))
        snap_b = _snapshot(landmarks=("main",))  # stripped layout
        h1 = compute_page_hash("https://x.com/p/1", snap_a)
        h2 = compute_page_hash("https://x.com/p/1", snap_b)
        assert h1 != h2

    def test_page_type_change_diverges(self):
        snap_a = _snapshot(page_type="list")
        snap_b = _snapshot(page_type="detail")
        h1 = compute_page_hash("https://x.com/p/1", snap_a)
        h2 = compute_page_hash("https://x.com/p/1", snap_b)
        assert h1 != h2

    def test_medium_page_jitter_collides_within_bucket(self):
        # 22 vs 23 elements both fall in the 20-24 bucket under the
        # adaptive scheme, so a one-element jitter on a moderately sized
        # page should not invalidate the fingerprint.
        snap_a = _snapshot(n_elements=22)
        snap_b = _snapshot(n_elements=23)
        h1 = compute_page_hash("https://x.com/p/1", snap_a)
        h2 = compute_page_hash("https://x.com/p/1", snap_b)
        assert h1 == h2

    def test_large_page_jitter_collides(self):
        # Search-result-like page: 55 vs 58 elements both in the 40-59 bucket.
        snap_a = _snapshot(n_elements=55)
        snap_b = _snapshot(n_elements=58)
        h1 = compute_page_hash("https://x.com/search", snap_a)
        h2 = compute_page_hash("https://x.com/search", snap_b)
        assert h1 == h2

    def test_small_page_one_element_change_diverges(self):
        # 5 vs 6 elements under adaptive bucketing — small pages are
        # counted exactly so a login form gaining a validation hint
        # invalidates the vision cache.
        snap_a = _snapshot(n_elements=5, landmarks=("main",), n_cards=0, n_collections=0)
        snap_b = _snapshot(n_elements=6, landmarks=("main",), n_cards=0, n_collections=0)
        h1 = compute_page_hash("https://x.com/login", snap_a)
        h2 = compute_page_hash("https://x.com/login", snap_b)
        assert h1 != h2

    def test_large_element_jump_diverges(self):
        snap_small = _snapshot(n_elements=8)   # exact bucket
        snap_huge = _snapshot(n_elements=80)   # 80-99 bucket
        h1 = compute_page_hash("https://x.com/p/1", snap_small)
        h2 = compute_page_hash("https://x.com/p/1", snap_huge)
        assert h1 != h2

    def test_filling_input_changes_hash(self):
        # Same structural elements, but one input goes from blank to filled.
        base = _snapshot(n_elements=5, landmarks=("main",), n_cards=0, n_collections=0)
        base["elements"][-1] = {"role": "textbox", "tag": "input", "value": ""}
        filled = _snapshot(n_elements=5, landmarks=("main",), n_cards=0, n_collections=0)
        filled["elements"][-1] = {"role": "textbox", "tag": "input", "value": "user@example.com"}
        h1 = compute_page_hash("https://x.com/login", base)
        h2 = compute_page_hash("https://x.com/login", filled)
        assert h1 != h2

    def test_checking_checkbox_changes_hash(self):
        base = _snapshot(n_elements=4, landmarks=("main",), n_cards=0, n_collections=0)
        base["elements"][-1] = {
            "role": "checkbox", "tag": "input",
            "aria_state": {"checked": False},
        }
        checked = _snapshot(n_elements=4, landmarks=("main",), n_cards=0, n_collections=0)
        checked["elements"][-1] = {
            "role": "checkbox", "tag": "input",
            "aria_state": {"checked": True},
        }
        h1 = compute_page_hash("https://x.com/login", base)
        h2 = compute_page_hash("https://x.com/login", checked)
        assert h1 != h2

    def test_invalid_field_changes_hash(self):
        # Validation error pushes a field into invalid state — fingerprint
        # should diverge so vision re-describes the error.
        base = _snapshot(n_elements=5, landmarks=("main",), n_cards=0, n_collections=0)
        base["elements"][-1] = {"role": "textbox", "tag": "input"}
        invalid = _snapshot(n_elements=5, landmarks=("main",), n_cards=0, n_collections=0)
        invalid["elements"][-1] = {
            "role": "textbox", "tag": "input",
            "form_state": {"invalid": True},
        }
        h1 = compute_page_hash("https://x.com/form", base)
        h2 = compute_page_hash("https://x.com/form", invalid)
        assert h1 != h2

    def test_modal_presence_changes_hash(self):
        snap_no_modal = _snapshot()
        snap_with_modal = _snapshot()
        snap_with_modal["has_modal"] = True
        h1 = compute_page_hash("https://x.com/p/1", snap_no_modal)
        h2 = compute_page_hash("https://x.com/p/1", snap_with_modal)
        assert h1 != h2

    def test_modal_inferred_from_dialog_role(self):
        # Even without the ``has_modal`` key, a dialog role in elements
        # should surface as a modal.
        snap_base = _snapshot(n_elements=5, landmarks=("main",), n_cards=0, n_collections=0)
        snap_dialog = _snapshot(n_elements=5, landmarks=("main",), n_cards=0, n_collections=0)
        snap_dialog["elements"][-1] = {"role": "dialog", "tag": "div"}
        h1 = compute_page_hash("https://x.com/p/1", snap_base)
        h2 = compute_page_hash("https://x.com/p/1", snap_dialog)
        assert h1 != h2

    def test_query_string_does_not_affect_hash(self):
        snap = _snapshot()
        h1 = compute_page_hash("https://x.com/p/1?ref=foo", snap)
        h2 = compute_page_hash("https://x.com/p/1?ref=bar", snap)
        assert h1 == h2

    def test_empty_snapshot_returns_stable_hash(self):
        h1 = compute_page_hash("https://x.com/p/1", {})
        h2 = compute_page_hash("https://x.com/p/1", {})
        assert h1 == h2 and len(h1) == 32

    def test_text_content_does_not_affect_hash(self):
        snap_a = _snapshot()
        snap_b = _snapshot()
        snap_b["elements"][-1]["text"] = "completely different text"
        h1 = compute_page_hash("https://x.com/p/1", snap_a)
        h2 = compute_page_hash("https://x.com/p/1", snap_b)
        assert h1 == h2
