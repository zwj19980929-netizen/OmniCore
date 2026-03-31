"""
P0-2: Skill Library 经验复用 — 单元测试。

覆盖：
- SkillDefinition 数据模型
- SkillStore 提炼逻辑（extract_and_save）
- SkillStore 匹配逻辑（match）
- SkillStore 实例化逻辑（instantiate / _fill_template）
- SkillStore 反馈更新（update_feedback）+ 自动降级
- SkillStore 管理接口（list_skills / deprecate_skill / delete_skill）
- _parse_json_response 辅助函数
- _all_tasks_succeeded / _build_task_queue_summary 辅助函数
- 配置开关控制
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from memory.skill_store import (
    SkillDefinition,
    SkillStore,
    _all_tasks_succeeded,
    _build_task_queue_summary,
    _parse_json_response,
)


# ── Fixtures & Helpers ──────────────────────────────────────────


def _make_skill(**overrides) -> SkillDefinition:
    defaults = dict(
        skill_id="skill_test001",
        name="搜索商品价格",
        description="在搜索引擎搜索指定商品的价格信息",
        version=1,
        task_template=[
            {
                "tool_name": "browser.search_and_extract",
                "description_template": "搜索 {query} 的价格",
                "params_template": {"url": "https://google.com", "task": "{query}"},
                "priority": 10,
            },
            {
                "tool_name": "browser.interact",
                "description_template": "从搜索结果中提取 {query} 的价格数据",
                "params_template": {"task": "提取价格数据"},
                "priority": 8,
            },
        ],
        parameters={
            "query": {"type": "string", "description": "搜索关键词"},
        },
        source_job_id="job_abc",
        source_intent="web_scraping",
        tags=["搜索", "价格"],
        created_at="2026-03-28T10:00:00",
        updated_at="2026-03-28T10:00:00",
    )
    defaults.update(overrides)
    return SkillDefinition(**defaults)


def _make_completed_state(
    num_tasks: int = 3,
    user_input: str = "帮我搜索 iPhone 16 的价格",
    intent: str = "web_scraping",
    job_id: str = "job_123",
) -> Dict[str, Any]:
    """构建一个模拟成功完成的 state。"""
    task_queue = []
    for i in range(num_tasks):
        task_queue.append({
            "task_id": f"task_{i+1}",
            "tool_name": "browser.search_and_extract" if i == 0 else "browser.interact",
            "description": f"步骤 {i+1}: 执行操作",
            "status": "completed",
            "params": {},
            "priority": 10 - i,
        })
    return {
        "user_input": user_input,
        "current_intent": intent,
        "job_id": job_id,
        "task_queue": task_queue,
        "critic_approved": True,
        "matched_skill_id": "",
    }


class FakeChromaMemory:
    """Mock ChromaMemory for isolated testing."""

    def __init__(self):
        self._store: Dict[str, Dict[str, Any]] = {}

    def add_memory(self, content, metadata=None, memory_type="general",
                   fingerprint="", allow_update=False, skip_dedup=False, **kwargs) -> str:
        mem_id = fingerprint or f"mem_{len(self._store)}"
        self._store[mem_id] = {
            "id": mem_id,
            "content": content,
            "metadata": {**(metadata or {}), "type": memory_type},
        }
        return mem_id

    def search_memory(self, query, n_results=5, memory_type=None, **kwargs) -> List[Dict]:
        results = []
        for mem_id, record in self._store.items():
            meta = record.get("metadata", {})
            if memory_type and meta.get("type") != memory_type:
                continue
            results.append(record)
        return results[:n_results]

    def get_recent_memories(self, limit=10, memory_type=None, **kwargs) -> List[Dict]:
        return self.search_memory("", n_results=limit, memory_type=memory_type)

    def delete_memory(self, memory_id) -> bool:
        if memory_id in self._store:
            del self._store[memory_id]
            return True
        return False


class FakeLLMResponse:
    def __init__(self, content: str):
        self.content = content


# ── SkillDefinition Tests ───────────────────────────────────────


class TestSkillDefinition:
    def test_defaults(self):
        s = SkillDefinition()
        assert s.skill_id == ""
        assert s.version == 1
        assert s.deprecated is False
        assert s.total_uses == 0
        assert s.success_rate == 0.0

    def test_success_rate(self):
        s = _make_skill(success_count=7, failure_count=3)
        assert s.total_uses == 10
        assert s.success_rate == pytest.approx(0.7)

    def test_zero_division_rate(self):
        s = _make_skill(success_count=0, failure_count=0)
        assert s.success_rate == 0.0


# ── _parse_json_response Tests ──────────────────────────────────


class TestParseJsonResponse:
    def test_plain_json(self):
        result = _parse_json_response('{"worth_extracting": true, "skill_name": "test"}')
        assert result["worth_extracting"] is True

    def test_markdown_fenced(self):
        text = '```json\n{"worth_extracting": false, "reason": "too simple"}\n```'
        result = _parse_json_response(text)
        assert result["worth_extracting"] is False

    def test_json_embedded_in_text(self):
        text = 'Here is the result: {"key": "value"} end.'
        result = _parse_json_response(text)
        assert result["key"] == "value"

    def test_empty_string(self):
        assert _parse_json_response("") is None

    def test_none(self):
        assert _parse_json_response(None) is None

    def test_invalid_json(self):
        assert _parse_json_response("not json at all") is None


# ── _all_tasks_succeeded Tests ──────────────────────────────────


class TestAllTasksSucceeded:
    def test_all_completed(self):
        tasks = [{"status": "completed"}, {"status": "completed"}]
        assert _all_tasks_succeeded(tasks) is True

    def test_one_failed(self):
        tasks = [{"status": "completed"}, {"status": "failed"}]
        assert _all_tasks_succeeded(tasks) is False

    def test_empty(self):
        assert _all_tasks_succeeded([]) is False

    def test_pending(self):
        tasks = [{"status": "completed"}, {"status": "pending"}]
        assert _all_tasks_succeeded(tasks) is False


# ── _build_task_queue_summary Tests ─────────────────────────────


class TestBuildTaskQueueSummary:
    def test_summary_format(self):
        tasks = [
            {"tool_name": "browser.interact", "description": "搜索商品", "status": "completed"},
            {"tool_name": "file.write", "description": "保存结果", "status": "completed"},
        ]
        summary = _build_task_queue_summary(tasks)
        assert "1. [browser.interact]" in summary
        assert "2. [file.write]" in summary
        assert "completed" in summary


# ── SkillStore._fill_template Tests ─────────────────────────────


class TestFillTemplate:
    def test_basic_fill(self):
        skill = _make_skill()
        result = SkillStore._fill_template(skill, {"query": "iPhone 16 价格"})
        assert result is not None
        assert len(result) == 2
        assert result[0]["tool_name"] == "browser.search_and_extract"
        assert "iPhone 16 价格" in result[0]["description"]
        assert result[0]["params"]["task"] == "iPhone 16 价格"
        assert result[0]["skill_id"] == "skill_test001"

    def test_dependencies(self):
        skill = _make_skill()
        result = SkillStore._fill_template(skill, {"query": "test"})
        assert result[0]["depends_on"] == []
        assert result[1]["depends_on"] == ["skill_task_1"]

    def test_empty_params(self):
        skill = _make_skill(parameters={})
        result = SkillStore._fill_template(skill, {})
        assert result is not None
        assert len(result) == 2
        # Templates with {query} won't be substituted — that's OK for no-param skills
        assert result[0]["tool_name"] == "browser.search_and_extract"

    def test_empty_template(self):
        skill = _make_skill(task_template=[])
        result = SkillStore._fill_template(skill, {"query": "test"})
        assert result is None

    def test_missing_param_graceful(self):
        skill = _make_skill()
        # Missing 'query' param — should not crash, template kept as-is
        result = SkillStore._fill_template(skill, {"other_param": "value"})
        assert result is not None
        assert "{query}" in result[0]["description"]


# ── SkillStore.extract_and_save Tests ───────────────────────────


class TestSkillExtraction:
    def _make_store(self, chroma=None):
        store = SkillStore.__new__(SkillStore)
        store._chroma = chroma or FakeChromaMemory()
        return store

    @patch("memory.skill_store.settings")
    def test_disabled_returns_none(self, mock_settings):
        mock_settings.SKILL_LIBRARY_ENABLED = False
        store = self._make_store()
        assert store.extract_and_save(_make_completed_state()) is None

    @patch("memory.skill_store.settings")
    def test_single_step_skipped(self, mock_settings):
        mock_settings.SKILL_LIBRARY_ENABLED = True
        mock_settings.SKILL_MIN_STEPS_TO_EXTRACT = 2
        store = self._make_store()
        state = _make_completed_state(num_tasks=1)
        assert store.extract_and_save(state) is None

    @patch("memory.skill_store.settings")
    def test_failed_task_skipped(self, mock_settings):
        mock_settings.SKILL_LIBRARY_ENABLED = True
        mock_settings.SKILL_MIN_STEPS_TO_EXTRACT = 2
        store = self._make_store()
        state = _make_completed_state()
        state["task_queue"][1]["status"] = "failed"
        assert store.extract_and_save(state) is None

    @patch("memory.skill_store.settings")
    @patch("memory.skill_store.LLMClient")
    @patch("memory.skill_store._load_prompt")
    def test_successful_extraction(self, mock_load_prompt, mock_llm_cls, mock_settings):
        mock_settings.SKILL_LIBRARY_ENABLED = True
        mock_settings.SKILL_MIN_STEPS_TO_EXTRACT = 2
        mock_settings.PROJECT_ROOT = "."

        mock_load_prompt.return_value = "prompt: {user_input} {intent} {task_queue_summary}"

        llm_instance = MagicMock()
        llm_instance.chat.return_value = FakeLLMResponse(json.dumps({
            "worth_extracting": True,
            "skill_name": "搜索商品价格",
            "skill_description": "在搜索引擎搜索指定商品的价格信息",
            "parameters": {"query": {"type": "string", "description": "关键词"}},
            "task_template": [
                {
                    "tool_name": "browser.search_and_extract",
                    "description_template": "搜索 {query}",
                    "params_template": {"task": "{query}"},
                    "priority": 10,
                },
                {
                    "tool_name": "browser.interact",
                    "description_template": "提取 {query} 数据",
                    "params_template": {},
                    "priority": 8,
                },
            ],
            "tags": ["搜索", "价格"],
        }))
        mock_llm_cls.return_value = llm_instance

        chroma = FakeChromaMemory()
        store = self._make_store(chroma)
        skill_id = store.extract_and_save(_make_completed_state())
        assert skill_id is not None
        assert skill_id.startswith("skill_")
        assert len(chroma._store) == 1

    @patch("memory.skill_store.settings")
    @patch("memory.skill_store.LLMClient")
    @patch("memory.skill_store._load_prompt")
    def test_not_worth_extracting(self, mock_load_prompt, mock_llm_cls, mock_settings):
        mock_settings.SKILL_LIBRARY_ENABLED = True
        mock_settings.SKILL_MIN_STEPS_TO_EXTRACT = 2
        mock_settings.PROJECT_ROOT = "."

        mock_load_prompt.return_value = "prompt: {user_input} {intent} {task_queue_summary}"

        llm_instance = MagicMock()
        llm_instance.chat.return_value = FakeLLMResponse(json.dumps({
            "worth_extracting": False,
            "reason": "一次性操作",
        }))
        mock_llm_cls.return_value = llm_instance

        store = self._make_store()
        assert store.extract_and_save(_make_completed_state()) is None

    @patch("memory.skill_store.settings")
    @patch("memory.skill_store.LLMClient")
    @patch("memory.skill_store._load_prompt")
    def test_dedup_skips_similar(self, mock_load_prompt, mock_llm_cls, mock_settings):
        mock_settings.SKILL_LIBRARY_ENABLED = True
        mock_settings.SKILL_MIN_STEPS_TO_EXTRACT = 2
        mock_settings.PROJECT_ROOT = "."

        mock_load_prompt.return_value = "prompt: {user_input} {intent} {task_queue_summary}"

        llm_instance = MagicMock()
        llm_instance.chat.return_value = FakeLLMResponse(json.dumps({
            "worth_extracting": True,
            "skill_name": "搜索商品价格",
            "skill_description": "在搜索引擎搜索商品价格",
            "parameters": {"query": {"type": "string"}},
            "task_template": [
                {"tool_name": "browser.interact", "description_template": "搜索", "priority": 10},
                {"tool_name": "browser.interact", "description_template": "提取", "priority": 8},
            ],
            "tags": [],
        }))
        mock_llm_cls.return_value = llm_instance

        # Pre-populate a similar skill
        chroma = FakeChromaMemory()
        chroma.add_memory(
            "在搜索引擎搜索商品价格",
            metadata={"skill_id": "skill_existing"},
            memory_type="skill_definition",
            fingerprint="skill_existing",
        )

        store = self._make_store(chroma)
        # Should detect duplicate and return None
        assert store.extract_and_save(_make_completed_state()) is None


# ── SkillStore.match Tests ──────────────────────────────────────


class TestSkillMatching:
    def _make_store_with_skill(self, **skill_overrides):
        chroma = FakeChromaMemory()
        skill = _make_skill(**skill_overrides)
        metadata = {
            "skill_id": skill.skill_id,
            "name": skill.name,
            "version": str(skill.version),
            "source_intent": skill.source_intent,
            "success_count": str(skill.success_count),
            "failure_count": str(skill.failure_count),
            "deprecated": "true" if skill.deprecated else "false",
            "tags": ",".join(skill.tags),
            "task_template_json": json.dumps(skill.task_template, ensure_ascii=False),
            "parameters_json": json.dumps(skill.parameters, ensure_ascii=False),
            "created_at": skill.created_at,
            "updated_at": skill.updated_at,
        }
        chroma.add_memory(
            skill.description,
            metadata=metadata,
            memory_type="skill_definition",
            fingerprint=skill.skill_id,
        )
        store = SkillStore.__new__(SkillStore)
        store._chroma = chroma
        return store

    @patch("memory.skill_store.settings")
    def test_match_active_skill(self, mock_settings):
        mock_settings.SKILL_LIBRARY_ENABLED = True
        mock_settings.SKILL_MATCH_THRESHOLD = 0.3
        store = self._make_store_with_skill(success_count=5, failure_count=1)
        result = store.match("帮我查一下 MacBook 的价格")
        assert result is not None
        assert result.skill_id == "skill_test001"

    @patch("memory.skill_store.settings")
    def test_skip_deprecated(self, mock_settings):
        mock_settings.SKILL_LIBRARY_ENABLED = True
        mock_settings.SKILL_MATCH_THRESHOLD = 0.3
        store = self._make_store_with_skill(deprecated=True)
        result = store.match("帮我查一下价格")
        assert result is None

    @patch("memory.skill_store.settings")
    def test_skip_low_success_rate(self, mock_settings):
        mock_settings.SKILL_LIBRARY_ENABLED = True
        mock_settings.SKILL_MATCH_THRESHOLD = 0.3
        store = self._make_store_with_skill(success_count=1, failure_count=9)
        result = store.match("帮我查一下价格")
        assert result is None

    @patch("memory.skill_store.settings")
    def test_disabled_returns_none(self, mock_settings):
        mock_settings.SKILL_LIBRARY_ENABLED = False
        store = self._make_store_with_skill()
        assert store.match("任何输入") is None

    @patch("memory.skill_store.settings")
    def test_empty_input_returns_none(self, mock_settings):
        mock_settings.SKILL_LIBRARY_ENABLED = True
        store = self._make_store_with_skill()
        assert store.match("") is None


# ── SkillStore.instantiate Tests ────────────────────────────────


class TestSkillInstantiation:
    @patch("memory.skill_store.LLMClient")
    @patch("memory.skill_store._load_prompt")
    def test_params_correctly_filled(self, mock_load_prompt, mock_llm_cls):
        mock_load_prompt.return_value = "prompt: {user_input} {parameters_json} {skill_name} {skill_description}"

        llm_instance = MagicMock()
        llm_instance.chat.return_value = FakeLLMResponse(json.dumps({
            "query": "MacBook Pro 价格",
        }))
        mock_llm_cls.return_value = llm_instance

        store = SkillStore.__new__(SkillStore)
        store._chroma = FakeChromaMemory()
        skill = _make_skill()
        result = store.instantiate(skill, "帮我查 MacBook Pro 的价格")
        assert result is not None
        assert len(result) == 2
        assert "MacBook Pro 价格" in result[0]["description"]
        assert result[0]["params"]["task"] == "MacBook Pro 价格"

    @patch("memory.skill_store.LLMClient")
    @patch("memory.skill_store._load_prompt")
    def test_param_extraction_failure_returns_none(self, mock_load_prompt, mock_llm_cls):
        mock_load_prompt.return_value = "prompt: {user_input} {parameters_json} {skill_name} {skill_description}"

        llm_instance = MagicMock()
        llm_instance.chat.return_value = FakeLLMResponse("invalid json")
        mock_llm_cls.return_value = llm_instance

        store = SkillStore.__new__(SkillStore)
        store._chroma = FakeChromaMemory()
        skill = _make_skill()
        result = store.instantiate(skill, "模糊输入")
        assert result is None

    def test_no_params_skill(self):
        """无参数的 Skill 直接实例化，不调用 LLM。"""
        store = SkillStore.__new__(SkillStore)
        store._chroma = FakeChromaMemory()
        skill = _make_skill(
            parameters={},
            task_template=[
                {"tool_name": "system.status", "description_template": "检查系统状态", "priority": 10},
                {"tool_name": "file.write", "description_template": "保存报告", "priority": 8},
            ],
        )
        result = store.instantiate(skill, "检查系统状态")
        assert result is not None
        assert len(result) == 2
        assert result[0]["tool_name"] == "system.status"


# ── SkillStore.update_feedback Tests ────────────────────────────


class TestSkillFeedback:
    def _make_store_with_feedback_skill(self, success_count=2, failure_count=0):
        chroma = FakeChromaMemory()
        skill = _make_skill(success_count=success_count, failure_count=failure_count)
        metadata = {
            "skill_id": skill.skill_id,
            "name": skill.name,
            "success_count": str(skill.success_count),
            "failure_count": str(skill.failure_count),
            "deprecated": "false",
            "task_template_json": json.dumps(skill.task_template),
            "parameters_json": json.dumps(skill.parameters),
        }
        chroma.add_memory(
            skill.description,
            metadata=metadata,
            memory_type="skill_definition",
            fingerprint=skill.skill_id,
        )
        store = SkillStore.__new__(SkillStore)
        store._chroma = chroma
        return store, chroma

    @patch("memory.skill_store.settings")
    def test_success_increments(self, mock_settings):
        mock_settings.SKILL_AUTO_DEPRECATE_MIN_USES = 3
        mock_settings.SKILL_AUTO_DEPRECATE_THRESHOLD = 0.3
        store, chroma = self._make_store_with_feedback_skill(success_count=2, failure_count=0)
        store.update_feedback("skill_test001", success=True)
        updated = chroma._store.get("skill_test001", {}).get("metadata", {})
        assert int(updated.get("success_count", 0)) == 3

    @patch("memory.skill_store.settings")
    def test_failure_increments(self, mock_settings):
        mock_settings.SKILL_AUTO_DEPRECATE_MIN_USES = 3
        mock_settings.SKILL_AUTO_DEPRECATE_THRESHOLD = 0.3
        store, chroma = self._make_store_with_feedback_skill(success_count=2, failure_count=0)
        store.update_feedback("skill_test001", success=False)
        updated = chroma._store.get("skill_test001", {}).get("metadata", {})
        assert int(updated.get("failure_count", 0)) == 1

    @patch("memory.skill_store.settings")
    def test_auto_deprecation(self, mock_settings):
        mock_settings.SKILL_AUTO_DEPRECATE_MIN_USES = 3
        mock_settings.SKILL_AUTO_DEPRECATE_THRESHOLD = 0.3
        store, chroma = self._make_store_with_feedback_skill(success_count=0, failure_count=2)
        store.update_feedback("skill_test001", success=False)
        updated = chroma._store.get("skill_test001", {}).get("metadata", {})
        assert updated.get("deprecated") == "true"

    @patch("memory.skill_store.settings")
    def test_no_deprecation_above_threshold(self, mock_settings):
        mock_settings.SKILL_AUTO_DEPRECATE_MIN_USES = 3
        mock_settings.SKILL_AUTO_DEPRECATE_THRESHOLD = 0.3
        store, chroma = self._make_store_with_feedback_skill(success_count=2, failure_count=0)
        store.update_feedback("skill_test001", success=True)
        updated = chroma._store.get("skill_test001", {}).get("metadata", {})
        assert updated.get("deprecated") != "true"

    def test_empty_skill_id_noop(self):
        store = SkillStore.__new__(SkillStore)
        store._chroma = FakeChromaMemory()
        # Should not raise
        store.update_feedback("", success=True)


# ── SkillStore management Tests ─────────────────────────────────


class TestSkillManagement:
    def _make_populated_store(self):
        chroma = FakeChromaMemory()
        for i in range(3):
            deprecated = (i == 2)
            chroma.add_memory(
                f"技能描述 {i}",
                metadata={
                    "skill_id": f"skill_{i}",
                    "name": f"技能 {i}",
                    "success_count": str(5 - i),
                    "failure_count": str(i),
                    "deprecated": "true" if deprecated else "false",
                    "task_template_json": "[]",
                    "parameters_json": "{}",
                    "source_intent": "web_scraping",
                    "version": "1",
                },
                memory_type="skill_definition",
                fingerprint=f"skill_{i}",
            )
        store = SkillStore.__new__(SkillStore)
        store._chroma = chroma
        return store, chroma

    def test_list_active_only(self):
        store, _ = self._make_populated_store()
        skills = store.list_skills(include_deprecated=False)
        assert len(skills) == 2
        assert all(not s.deprecated for s in skills)

    def test_list_all(self):
        store, _ = self._make_populated_store()
        skills = store.list_skills(include_deprecated=True)
        assert len(skills) == 3

    def test_list_sorted_by_success(self):
        store, _ = self._make_populated_store()
        skills = store.list_skills(include_deprecated=True)
        success_counts = [s.success_count for s in skills]
        assert success_counts == sorted(success_counts, reverse=True)

    def test_deprecate_skill(self):
        store, chroma = self._make_populated_store()
        ok = store.deprecate_skill("skill_0")
        assert ok is True
        updated = chroma._store.get("skill_0", {}).get("metadata", {})
        assert updated.get("deprecated") == "true"

    def test_deprecate_nonexistent(self):
        store, _ = self._make_populated_store()
        ok = store.deprecate_skill("skill_nonexistent")
        assert ok is False

    def test_delete_skill(self):
        store, chroma = self._make_populated_store()
        ok = store.delete_skill("skill_1")
        assert ok is True
        assert "skill_1" not in chroma._store

    def test_delete_nonexistent(self):
        store, _ = self._make_populated_store()
        ok = store.delete_skill("skill_nonexistent")
        assert ok is False


# ── SkillStore._skill_from_metadata Tests ───────────────────────


class TestSkillFromMetadata:
    def test_valid_metadata(self):
        store = SkillStore.__new__(SkillStore)
        meta = {
            "skill_id": "skill_x",
            "name": "测试技能",
            "version": "2",
            "source_intent": "file_operation",
            "success_count": "10",
            "failure_count": "2",
            "deprecated": "false",
            "tags": "文件,操作",
            "task_template_json": json.dumps([{"tool_name": "file.write", "description_template": "写入"}]),
            "parameters_json": json.dumps({"path": {"type": "string"}}),
            "created_at": "2026-01-01",
            "updated_at": "2026-03-28",
        }
        skill = store._skill_from_metadata(meta)
        assert skill is not None
        assert skill.skill_id == "skill_x"
        assert skill.name == "测试技能"
        assert skill.version == 2
        assert skill.success_count == 10
        assert skill.failure_count == 2
        assert skill.deprecated is False
        assert skill.tags == ["文件", "操作"]
        assert len(skill.task_template) == 1
        assert "path" in skill.parameters

    def test_empty_skill_id(self):
        store = SkillStore.__new__(SkillStore)
        assert store._skill_from_metadata({}) is None
        assert store._skill_from_metadata({"skill_id": ""}) is None

    def test_corrupted_json_fields(self):
        store = SkillStore.__new__(SkillStore)
        meta = {
            "skill_id": "skill_bad",
            "name": "坏数据",
            "task_template_json": "not json",
            "parameters_json": "{invalid",
        }
        skill = store._skill_from_metadata(meta)
        assert skill is not None
        assert skill.task_template == []
        assert skill.parameters == {}
