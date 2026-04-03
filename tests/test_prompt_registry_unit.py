"""
Unit tests for core/prompt_registry.py (S1: Prompt Section Registry)
"""
import pytest

from core.prompt_registry import (
    DYNAMIC_BOUNDARY,
    PromptRegistry,
    PromptSection,
    _count_tokens,
    build_single_section_prompt,
)


# ---------------------------------------------------------------------------
# PromptSection basics
# ---------------------------------------------------------------------------

class TestPromptSection:
    def test_defaults(self):
        s = PromptSection(name="test", content="hello")
        assert s.cacheable is True
        assert s.enabled is True
        assert s.priority == 50
        assert s.max_tokens is None

    def test_token_count_positive(self):
        s = PromptSection(name="t", content="Hello world, this is a test.")
        assert s.token_count > 0

    def test_invalidate_cache(self):
        s = PromptSection(name="t", content="aaa")
        first = s.token_count
        s.content = "aaa bbb ccc ddd eee fff"
        s.invalidate_cache()
        assert s.token_count >= first


# ---------------------------------------------------------------------------
# PromptRegistry: registration & query
# ---------------------------------------------------------------------------

class TestRegistryBasics:
    def test_register_and_get(self):
        reg = PromptRegistry()
        s = PromptSection(name="identity", content="I am X")
        reg.register(s)
        assert reg.get_section("identity") is s
        assert reg.get_section("nonexistent") is None

    def test_register_many(self):
        reg = PromptRegistry()
        sections = [
            PromptSection(name="a", content="A"),
            PromptSection(name="b", content="B"),
        ]
        reg.register_many(sections)
        assert len(reg.get_sections()) == 2

    def test_get_sections_filters(self):
        reg = PromptRegistry()
        reg.register(PromptSection(name="static", content="S", cacheable=True))
        reg.register(PromptSection(name="dynamic", content="D", cacheable=False))
        reg.register(PromptSection(name="off", content="X", enabled=False))

        assert len(reg.get_sections(enabled_only=True)) == 2
        assert len(reg.get_sections(enabled_only=False)) == 3
        assert len(reg.get_sections(cacheable=True)) == 1
        assert len(reg.get_sections(cacheable=False)) == 1


# ---------------------------------------------------------------------------
# PromptRegistry: toggle
# ---------------------------------------------------------------------------

class TestToggle:
    def test_enable_disable(self):
        reg = PromptRegistry()
        reg.register(PromptSection(name="x", content="content"))
        reg.disable("x")
        assert reg.get_section("x").enabled is False
        reg.enable("x")
        assert reg.get_section("x").enabled is True

    def test_disable_nonexistent_no_error(self):
        reg = PromptRegistry()
        reg.disable("nope")  # should not raise


# ---------------------------------------------------------------------------
# PromptRegistry: render
# ---------------------------------------------------------------------------

class TestRender:
    def test_static_only(self):
        reg = PromptRegistry()
        reg.register(PromptSection(name="a", content="AAA", cacheable=True))
        reg.register(PromptSection(name="b", content="BBB", cacheable=True))
        result = reg.render()
        assert "AAA" in result
        assert "BBB" in result
        assert DYNAMIC_BOUNDARY not in result

    def test_static_and_dynamic_boundary(self):
        reg = PromptRegistry()
        reg.register(PromptSection(name="s", content="STATIC", cacheable=True))
        reg.register(PromptSection(name="d", content="DYNAMIC", cacheable=False))
        result = reg.render()
        assert "STATIC" in result
        assert DYNAMIC_BOUNDARY in result
        assert "DYNAMIC" in result
        # Static should come before dynamic
        assert result.index("STATIC") < result.index("DYNAMIC")

    def test_no_boundary_when_flag_off(self):
        reg = PromptRegistry()
        reg.register(PromptSection(name="s", content="S", cacheable=True))
        reg.register(PromptSection(name="d", content="D", cacheable=False))
        result = reg.render(include_boundary=False)
        assert DYNAMIC_BOUNDARY not in result

    def test_disabled_sections_excluded(self):
        reg = PromptRegistry()
        reg.register(PromptSection(name="on", content="VISIBLE"))
        reg.register(PromptSection(name="off", content="HIDDEN", enabled=False))
        result = reg.render()
        assert "VISIBLE" in result
        assert "HIDDEN" not in result

    def test_empty_registry(self):
        reg = PromptRegistry()
        assert reg.render() == ""


# ---------------------------------------------------------------------------
# PromptRegistry: per-section truncation
# ---------------------------------------------------------------------------

class TestTruncation:
    def test_section_truncated_when_over_max_tokens(self):
        long_content = "word " * 5000  # ~5000 tokens
        reg = PromptRegistry()
        reg.register(PromptSection(
            name="big", content=long_content, max_tokens=100,
        ))
        result = reg.render()
        assert "[…truncated]" in result
        assert len(result) < len(long_content)

    def test_section_not_truncated_when_within_budget(self):
        reg = PromptRegistry()
        reg.register(PromptSection(
            name="small", content="short text", max_tokens=1000,
        ))
        result = reg.render()
        assert "[…truncated]" not in result
        assert "short text" in result


# ---------------------------------------------------------------------------
# PromptRegistry: total budget enforcement
# ---------------------------------------------------------------------------

class TestTotalBudget:
    def test_low_priority_disabled_when_over_budget(self):
        reg = PromptRegistry(total_budget=50)
        # High-priority section: small
        reg.register(PromptSection(
            name="important", content="A " * 20, priority=100,
        ))
        # Low-priority section: large
        reg.register(PromptSection(
            name="filler", content="B " * 500, priority=10,
        ))
        result = reg.render()
        assert "A" in result
        # Filler should be disabled
        assert reg.get_section("filler").enabled is False

    def test_no_budget_means_no_limit(self):
        reg = PromptRegistry(total_budget=0)
        reg.register(PromptSection(name="a", content="X " * 5000))
        result = reg.render()
        assert "X" in result


# ---------------------------------------------------------------------------
# PromptRegistry: token_report
# ---------------------------------------------------------------------------

class TestTokenReport:
    def test_report_structure(self):
        reg = PromptRegistry()
        reg.register(PromptSection(name="a", content="Hello", cacheable=True, priority=90))
        reg.register(PromptSection(name="b", content="World", cacheable=False, priority=50))
        report = reg.token_report()
        assert len(report) == 2
        assert report[0]["name"] == "a"
        assert report[1]["name"] == "b"
        for r in report:
            assert "token_count" in r
            assert "cacheable" in r
            assert "enabled" in r
            assert "pct" in r

    def test_report_pct_sums_to_100(self):
        reg = PromptRegistry()
        reg.register(PromptSection(name="a", content="Hello world"))
        reg.register(PromptSection(name="b", content="Foo bar baz"))
        report = reg.token_report()
        total_pct = sum(r["pct"] for r in report)
        assert 99.0 <= total_pct <= 101.0  # allow rounding

    def test_disabled_section_pct_is_zero(self):
        reg = PromptRegistry()
        reg.register(PromptSection(name="off", content="X", enabled=False))
        report = reg.token_report()
        assert report[0]["pct"] == 0.0


# ---------------------------------------------------------------------------
# build_single_section_prompt convenience
# ---------------------------------------------------------------------------

class TestBuildSingleSectionPrompt:
    def test_returns_content(self):
        result = build_single_section_prompt("test", "Hello World")
        assert result == "Hello World"

    def test_no_boundary_marker(self):
        result = build_single_section_prompt("test", "Content")
        assert DYNAMIC_BOUNDARY not in result


# ---------------------------------------------------------------------------
# _count_tokens
# ---------------------------------------------------------------------------

class TestCountTokens:
    def test_non_empty(self):
        assert _count_tokens("Hello world") > 0

    def test_empty(self):
        assert _count_tokens("") >= 0

    def test_chinese_text(self):
        assert _count_tokens("你好世界") > 0
