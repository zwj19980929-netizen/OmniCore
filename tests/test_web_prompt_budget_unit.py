from utils.web_prompt_budget import (
    BudgetSection,
    extract_anchor_terms,
    extract_relevant_html_fragments,
    render_budgeted_sections,
)


def test_render_budgeted_sections_respects_total_budget():
    rendered, report = render_budgeted_sections(
        [
            BudgetSection("a", "\n".join(f"line {index}" for index in range(50)), min_chars=120, max_chars=260),
            BudgetSection("b", "value " * 200, min_chars=120, max_chars=320, mode="text"),
            BudgetSection("c", "\n".join(f"row {index}" for index in range(40)), min_chars=120, max_chars=260),
        ],
        total_chars=600,
    )

    total_used = sum(int(item["used_chars"]) for item in report.values())
    assert total_used <= 600
    assert rendered["a"]
    assert rendered["b"]
    assert rendered["c"]


def test_extract_relevant_html_fragments_prefers_anchor_windows():
    html = "<html><body>" + ("noise " * 300) + "<div>Alpha target block</div>" + ("tail " * 300) + "</body></html>"
    fragments = extract_relevant_html_fragments(
        html,
        anchors=["Alpha target block"],
        total_chars=260,
        window_chars=120,
        max_fragments=2,
    )

    assert "Alpha target block" in fragments
    assert len(fragments) <= 260


def test_extract_anchor_terms_uses_snapshot_and_page_structure():
    anchors = extract_anchor_terms(
        task="find latest CNVD vulnerabilities",
        snapshot={
            "title": "CNVD vulnerability list",
            "cards": [{"title": "OpenClaw vulnerability", "snippet": "critical issue"}],
            "collections": [{"sample_items": ["CNVD-2026-0001 OpenClaw", "CNVD-2026-0002 Example"]}],
            "controls": [{"text": "Next page"}],
        },
        page_structure={
            "main_content_blocks": [{"content": "Latest vulnerability bulletin and disclosures"}],
        },
    )

    assert anchors
    assert any("OpenClaw" in item or "CNVD" in item for item in anchors)


def test_render_budgeted_sections_supports_token_mode():
    rendered, report = render_budgeted_sections(
        [
            BudgetSection("cards", "\n".join(f"card {index} " + ("x" * 80) for index in range(40)), min_tokens=40, max_tokens=120),
            BudgetSection("elements", "\n".join(f"element {index} " + ("y" * 60) for index in range(40)), min_tokens=40, max_tokens=120),
        ],
        total_tokens=150,
        model="openai/gpt-4o-mini",
    )

    total_used = sum(int(item["used_tokens"]) for item in report.values())
    assert total_used <= 150
    assert report["cards"]["budget_metric"] == "tokens"
    assert rendered["cards"]
