"""
Skill Library 存储层 — 提炼、检索、实例化、反馈更新、管理。

Skill 存储在独立的 ChromaDB collection ``omnicore_skills`` 中。
每条 document 为 Skill 的语义描述（用于向量检索），metadata 携带完整定义。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from config.settings import settings
from core.llm import LLMClient
from utils.logger import log_agent_action, log_warning, logger
from utils.text import sanitize_text, sanitize_value


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class SkillMatch:
    """A skill hit together with its retrieval score and summary fields."""

    skill_id: str
    name: str
    description: str
    score: float            # 0..1, higher is better
    success_rate: float
    total_uses: int
    tool_sequence: List[str] = field(default_factory=list)
    source_intent: str = ""


@dataclass
class SkillDefinition:
    """一个可复用的任务技能。"""

    skill_id: str = ""
    name: str = ""
    description: str = ""
    version: int = 1

    # 任务流模板
    task_template: List[Dict[str, Any]] = field(default_factory=list)

    # 参数 schema
    parameters: Dict[str, Any] = field(default_factory=dict)

    # 来源
    source_job_id: str = ""
    source_intent: str = ""

    # 质量指标
    success_count: int = 0
    failure_count: int = 0
    last_used_at: str = ""
    deprecated: bool = False

    # 元数据
    tags: List[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    @property
    def total_uses(self) -> int:
        return self.success_count + self.failure_count

    @property
    def success_rate(self) -> float:
        if self.total_uses == 0:
            return 0.0
        return self.success_count / self.total_uses


# ---------------------------------------------------------------------------
# Prompt templates (loaded lazily from prompts/ directory)
# ---------------------------------------------------------------------------

def _load_prompt(filename: str) -> str:
    """Read a prompt template from ``prompts/`` directory."""
    import os
    path = os.path.join(settings.PROJECT_ROOT, "prompts", filename)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        logger.warning("Prompt file not found: %s", path)
        return ""


# ---------------------------------------------------------------------------
# SkillStore
# ---------------------------------------------------------------------------

class SkillStore:
    """Skill Library: extraction, matching, instantiation, feedback."""

    COLLECTION_NAME = "omnicore_skills"
    DEDUP_DISTANCE_THRESHOLD = 0.15
    name = "SkillStore"

    def __init__(self) -> None:
        self._chroma = self._get_chroma()

    # -- lazy ChromaMemory with skill-specific collection --
    @staticmethod
    def _get_chroma():
        from memory.scoped_chroma_store import ChromaMemory
        return ChromaMemory(collection_name="omnicore_skills", silent=True)

    # ------------------------------------------------------------------
    # 1. Skill 提炼 (finalize 阶段调用)
    # ------------------------------------------------------------------

    def extract_and_save(self, state: Dict[str, Any]) -> Optional[str]:
        """
        从已完成的任务 state 中提炼 Skill。

        Returns:
            提炼成功返回 skill_id，否则返回 None。
        """
        if not settings.SKILL_LIBRARY_ENABLED:
            return None

        task_queue = state.get("task_queue") or []
        if len(task_queue) < settings.SKILL_MIN_STEPS_TO_EXTRACT:
            return None

        # 只提炼全部成功的任务
        if not _all_tasks_succeeded(task_queue):
            return None

        user_input = str(state.get("user_input", "") or "").strip()
        intent = str(state.get("current_intent", "") or "").strip()
        if not user_input:
            return None

        # 构建任务摘要
        task_summary = _build_task_queue_summary(task_queue)

        # LLM 判断是否值得提炼
        prompt_template = _load_prompt("skill_extraction.txt")
        if not prompt_template:
            return None

        prompt = prompt_template.format(
            user_input=user_input,
            intent=intent,
            task_queue_summary=task_summary,
        )

        llm = LLMClient()
        try:
            resp = llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                json_mode=True,
            )
        except Exception as exc:
            log_warning(f"Skill extraction LLM call failed: {exc}")
            return None

        parsed = _parse_json_response(resp.content)
        if not parsed or not parsed.get("worth_extracting"):
            return None

        skill_name = str(parsed.get("skill_name", "") or "").strip()
        skill_desc = str(parsed.get("skill_description", "") or "").strip()
        if not skill_name or not skill_desc:
            return None

        # 去重检查
        if self._is_duplicate(skill_desc):
            log_agent_action(self.name, "Skill dedup", f"Similar skill exists for: {skill_name}")
            return None

        skill_id = f"skill_{uuid.uuid4().hex[:12]}"
        now = datetime.now().isoformat(timespec="seconds")

        skill = SkillDefinition(
            skill_id=skill_id,
            name=skill_name,
            description=skill_desc,
            task_template=parsed.get("task_template") or [],
            parameters=parsed.get("parameters") or {},
            source_job_id=str(state.get("job_id", "") or ""),
            source_intent=intent,
            tags=parsed.get("tags") or [],
            created_at=now,
            updated_at=now,
        )

        self._save_skill(skill)
        log_agent_action(self.name, "Skill extracted", f"{skill_id}: {skill_name}")
        return skill_id

    # ------------------------------------------------------------------
    # 2. Skill 匹配 (Router 阶段调用)
    # ------------------------------------------------------------------

    def match_top_k(
        self,
        user_input: str,
        *,
        k: int = 3,
        min_score: float = 0.0,
    ) -> List[SkillMatch]:
        """
        Retrieve top-k skill hints for planner context injection (A3).

        Unlike ``match``, this returns multiple candidates with scores so
        the caller can surface them to the LLM as reference hints even
        when no single skill is a confident full match.
        """
        if not settings.SKILL_LIBRARY_ENABLED:
            return []
        clean = sanitize_text(user_input or "")
        if not clean or k <= 0:
            return []

        raw = self._chroma.search_memory(
            query=clean,
            n_results=max(k * 2, k),
            memory_type="skill_definition",
        )
        matches: List[SkillMatch] = []
        for item in raw:
            meta = item.get("metadata") or {}
            if str(meta.get("deprecated", "false") or "false") == "true":
                continue
            succ = int(meta.get("success_count", 0) or 0)
            fail = int(meta.get("failure_count", 0) or 0)
            total = succ + fail
            # drop skills with established failure pattern
            if total >= 3 and succ / total < 0.3:
                continue
            skill = self._skill_from_metadata(meta)
            if skill is None:
                continue
            distance = item.get("distance")
            score = max(0.0, 1.0 - float(distance if distance is not None else 1.0))
            if score < float(min_score):
                continue
            tool_seq = [
                str(step.get("tool_name", "") or "")
                for step in (skill.task_template or [])
                if step.get("tool_name")
            ]
            matches.append(
                SkillMatch(
                    skill_id=skill.skill_id,
                    name=skill.name,
                    description=sanitize_text(str(item.get("content") or "")),
                    score=score,
                    success_rate=skill.success_rate,
                    total_uses=skill.total_uses,
                    tool_sequence=tool_seq,
                    source_intent=skill.source_intent,
                )
            )
            if len(matches) >= k:
                break
        return matches

    def match(self, user_input: str, top_k: int = 3) -> Optional[SkillDefinition]:
        """
        语义匹配最佳 Skill。

        Returns:
            匹配到的 SkillDefinition 或 None。
        """
        if not settings.SKILL_LIBRARY_ENABLED:
            return None

        clean_input = sanitize_text(user_input or "")
        if not clean_input:
            return None

        results = self._chroma.search_memory(
            query=clean_input,
            n_results=top_k,
            memory_type="skill_definition",
        )

        if not results:
            return None

        threshold = settings.SKILL_MATCH_THRESHOLD
        for item in results:
            metadata = item.get("metadata") or {}
            distance = item.get("distance", 1.0)

            # ChromaMemory.search_memory 返回的 item 没有 distance 字段，
            # 但 _query_collection 内部有。这里用 metadata 里的字段做筛选。
            if metadata.get("deprecated") == "true":
                continue

            # 检查失败率
            success_count = int(metadata.get("success_count", 0) or 0)
            failure_count = int(metadata.get("failure_count", 0) or 0)
            total = success_count + failure_count
            if total >= 3 and success_count / total < 0.3:
                continue

            skill = self._skill_from_metadata(metadata)
            if skill and skill.task_template:
                log_agent_action(self.name, "Skill matched", f"{skill.skill_id}: {skill.name}")
                return skill

        return None

    # ------------------------------------------------------------------
    # 3. Skill 实例化
    # ------------------------------------------------------------------

    def instantiate(
        self,
        skill: SkillDefinition,
        user_input: str,
    ) -> Optional[List[Dict[str, Any]]]:
        """
        将 Skill 模板实例化为 task_queue。

        Uses LLM to extract parameter values from user_input,
        then fills the task template.

        Returns:
            task_queue list, or None if param extraction fails.
        """
        if not skill.parameters:
            # 无参数模板直接实例化
            return self._fill_template(skill, {})

        prompt_template = _load_prompt("skill_param_extraction.txt")
        if not prompt_template:
            return None

        prompt = prompt_template.format(
            user_input=user_input,
            parameters_json=json.dumps(skill.parameters, ensure_ascii=False, indent=2),
            skill_name=skill.name,
            skill_description=skill.description,
        )

        llm = LLMClient()
        try:
            resp = llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                json_mode=True,
            )
        except Exception as exc:
            log_warning(f"Skill param extraction LLM call failed: {exc}")
            return None

        param_values = _parse_json_response(resp.content)
        if not param_values:
            return None

        return self._fill_template(skill, param_values)

    # ------------------------------------------------------------------
    # 4. 反馈更新
    # ------------------------------------------------------------------

    def update_feedback(self, skill_id: str, success: bool) -> None:
        """更新 Skill 使用统计，连续低成功率自动降级。"""
        if not skill_id:
            return

        results = self._chroma.search_memory(
            query=skill_id,
            n_results=5,
            memory_type="skill_definition",
        )

        target = None
        for item in results:
            meta = item.get("metadata") or {}
            if meta.get("skill_id") == skill_id:
                target = item
                break

        if not target:
            return

        metadata = target.get("metadata") or {}
        memory_id = target.get("id", "")

        if success:
            metadata["success_count"] = str(int(metadata.get("success_count", "0") or "0") + 1)
        else:
            metadata["failure_count"] = str(int(metadata.get("failure_count", "0") or "0") + 1)

        metadata["last_used_at"] = datetime.now().isoformat(timespec="seconds")
        metadata["updated_at"] = metadata["last_used_at"]

        # 自动降级检查
        total = int(metadata.get("success_count", "0") or "0") + int(metadata.get("failure_count", "0") or "0")
        if total >= settings.SKILL_AUTO_DEPRECATE_MIN_USES:
            rate = int(metadata.get("success_count", "0") or "0") / total
            if rate < settings.SKILL_AUTO_DEPRECATE_THRESHOLD:
                metadata["deprecated"] = "true"
                log_warning(f"Skill {skill_id} auto-deprecated: success_rate={rate:.0%}")

        # 写回
        content = target.get("content", "")
        if memory_id and content:
            self._chroma.add_memory(
                content=content,
                metadata=metadata,
                memory_type="skill_definition",
                fingerprint=skill_id,
                allow_update=True,
                skip_dedup=True,
            )
            log_agent_action(self.name, "Skill feedback", f"{skill_id} success={success}")

    # ------------------------------------------------------------------
    # 5. 管理接口
    # ------------------------------------------------------------------

    def list_skills(self, include_deprecated: bool = False) -> List[SkillDefinition]:
        """列出所有 Skill。"""
        records = self._chroma.get_recent_memories(
            limit=200,
            memory_type="skill_definition",
        )

        skills: List[SkillDefinition] = []
        for record in records:
            meta = record.get("metadata") or {}
            if not include_deprecated and meta.get("deprecated") == "true":
                continue
            skill = self._skill_from_metadata(meta)
            if skill:
                skills.append(skill)

        # 按成功次数降序
        skills.sort(key=lambda s: s.success_count, reverse=True)
        return skills

    def deprecate_skill(self, skill_id: str) -> bool:
        """手动废弃 Skill。"""
        results = self._chroma.search_memory(
            query=skill_id,
            n_results=5,
            memory_type="skill_definition",
        )
        for item in results:
            meta = item.get("metadata") or {}
            if meta.get("skill_id") == skill_id:
                meta["deprecated"] = "true"
                meta["updated_at"] = datetime.now().isoformat(timespec="seconds")
                content = item.get("content", "")
                memory_id = item.get("id", "")
                if memory_id and content:
                    self._chroma.add_memory(
                        content=content,
                        metadata=meta,
                        memory_type="skill_definition",
                        fingerprint=skill_id,
                        allow_update=True,
                        skip_dedup=True,
                    )
                    log_agent_action(self.name, "Skill deprecated", skill_id)
                    return True
        return False

    def delete_skill(self, skill_id: str) -> bool:
        """删除 Skill。"""
        results = self._chroma.search_memory(
            query=skill_id,
            n_results=5,
            memory_type="skill_definition",
        )
        for item in results:
            meta = item.get("metadata") or {}
            if meta.get("skill_id") == skill_id:
                memory_id = item.get("id", "")
                if memory_id:
                    self._chroma.delete_memory(memory_id)
                    log_agent_action(self.name, "Skill deleted", skill_id)
                    return True
        return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _save_skill(self, skill: SkillDefinition) -> str:
        """将 Skill 写入 ChromaDB。"""
        metadata: Dict[str, Any] = {
            "skill_id": skill.skill_id,
            "name": skill.name,
            "version": skill.version,
            "source_job_id": skill.source_job_id,
            "source_intent": skill.source_intent,
            "success_count": str(skill.success_count),
            "failure_count": str(skill.failure_count),
            "last_used_at": skill.last_used_at,
            "deprecated": "true" if skill.deprecated else "false",
            "tags": ",".join(skill.tags) if skill.tags else "",
            "task_template_json": json.dumps(skill.task_template, ensure_ascii=False),
            "parameters_json": json.dumps(skill.parameters, ensure_ascii=False),
        }

        return self._chroma.add_memory(
            content=skill.description,
            metadata=metadata,
            memory_type="skill_definition",
            fingerprint=skill.skill_id,
            allow_update=True,
            skip_dedup=True,
        )

    def _is_duplicate(self, description: str) -> bool:
        """检查是否已存在语义相似的 Skill。"""
        results = self._chroma.search_memory(
            query=description,
            n_results=1,
            memory_type="skill_definition",
        )
        if not results:
            return False
        # ChromaMemory 的 search_memory 不直接返回 distance，
        # 但通过 _query_collection 内部逻辑，distance < DEDUP 时
        # 已有 skill 被认为是重复的。这里用内容相似度做二次确认。
        existing_content = str(results[0].get("content", "") or "")
        if not existing_content:
            return False
        # 如果搜出来且 metadata 有 skill_id，则认为相似
        meta = results[0].get("metadata") or {}
        return bool(meta.get("skill_id"))

    def _skill_from_metadata(self, metadata: Dict[str, Any]) -> Optional[SkillDefinition]:
        """从 ChromaDB metadata 重建 SkillDefinition。"""
        skill_id = str(metadata.get("skill_id", "") or "").strip()
        if not skill_id:
            return None

        try:
            task_template = json.loads(metadata.get("task_template_json", "[]") or "[]")
        except (json.JSONDecodeError, TypeError):
            task_template = []

        try:
            parameters = json.loads(metadata.get("parameters_json", "{}") or "{}")
        except (json.JSONDecodeError, TypeError):
            parameters = {}

        tags_raw = str(metadata.get("tags", "") or "")
        tags = [t.strip() for t in tags_raw.split(",") if t.strip()] if tags_raw else []

        return SkillDefinition(
            skill_id=skill_id,
            name=str(metadata.get("name", "") or ""),
            description="",  # description is in document, not metadata
            version=int(metadata.get("version", 1) or 1),
            task_template=task_template,
            parameters=parameters,
            source_job_id=str(metadata.get("source_job_id", "") or ""),
            source_intent=str(metadata.get("source_intent", "") or ""),
            success_count=int(metadata.get("success_count", 0) or 0),
            failure_count=int(metadata.get("failure_count", 0) or 0),
            last_used_at=str(metadata.get("last_used_at", "") or ""),
            deprecated=str(metadata.get("deprecated", "false") or "false") == "true",
            tags=tags,
            created_at=str(metadata.get("created_at", "") or ""),
            updated_at=str(metadata.get("updated_at", "") or ""),
        )

    @staticmethod
    def _fill_template(
        skill: SkillDefinition,
        param_values: Dict[str, str],
    ) -> Optional[List[Dict[str, Any]]]:
        """用参数值填充任务模板，生成 task_queue。"""
        task_queue: List[Dict[str, Any]] = []
        for i, step in enumerate(skill.task_template):
            description_tpl = str(step.get("description_template", "") or "")
            params_tpl = step.get("params_template") or {}

            try:
                description = description_tpl.format(**param_values) if param_values else description_tpl
            except KeyError:
                description = description_tpl

            params: Dict[str, Any] = {}
            for k, v in params_tpl.items():
                if isinstance(v, str) and param_values:
                    try:
                        params[k] = v.format(**param_values)
                    except KeyError:
                        params[k] = v
                else:
                    params[k] = v

            task = {
                "task_id": f"skill_task_{i + 1}",
                "tool_name": step.get("tool_name", ""),
                "description": description,
                "params": params,
                "priority": step.get("priority", 10),
                "depends_on": [f"skill_task_{i}"] if i > 0 else [],
                "status": "pending",
                "skill_id": skill.skill_id,
            }
            task_queue.append(task)

        return task_queue if task_queue else None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _all_tasks_succeeded(task_queue: List[Dict[str, Any]]) -> bool:
    """Check whether all tasks in the queue completed successfully."""
    if not task_queue:
        return False
    for task in task_queue:
        status = str(task.get("status", "") or "").strip().lower()
        if status != "completed":
            return False
    return True


def _build_task_queue_summary(task_queue: List[Dict[str, Any]]) -> str:
    """Build a concise text summary of the executed task queue."""
    lines: List[str] = []
    for i, task in enumerate(task_queue, 1):
        tool = str(task.get("tool_name", "") or "")
        desc = str(task.get("description", "") or "")[:200]
        status = str(task.get("status", "") or "")
        lines.append(f"{i}. [{tool}] {desc} → {status}")
    return "\n".join(lines)


def _parse_json_response(content: str) -> Optional[Dict[str, Any]]:
    """Robustly parse JSON from LLM response (handles markdown fences)."""
    if not content:
        return None
    text = content.strip()
    # Remove markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first line (```json) and last line (```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        # Try to find JSON object in the text
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except (json.JSONDecodeError, TypeError):
                pass
        return None
