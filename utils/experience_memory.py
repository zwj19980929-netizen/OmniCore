"""
经验记忆系统 - 从成功和失败中学习

基于 ChromaMemory 的语义检索后端，取代旧版 JSON 文件 + Jaccard 方案。
对外 API 保持兼容。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class Experience:
    """一次任务执行的经验记录"""
    task: str
    url: str
    domain: str
    success: bool
    steps_taken: int
    action_sequence: List[Dict]
    pattern: str
    timestamp: str
    error: str = ""
    extracted_data_sample: str = ""

    def to_dict(self) -> Dict:
        return asdict(self)


def _get_chroma_memory():
    """Lazy-import to avoid circular dependencies."""
    from memory.scoped_chroma_store import ChromaMemory
    return ChromaMemory(collection_name="omnicore_experience")


class ExperienceMemory:
    """
    经验记忆系统 — 使用 ChromaMemory 做语义检索。

    功能：
    1. 记录每次任务执行的结果
    2. 从成功经验中提取模式（语义相似度）
    3. 为新任务提供相似经验的提示
    4. 避免重复失败的策略
    """

    def __init__(self, storage_path: str = "data/agent_experiences.json"):
        # storage_path kept for backward-compat signature but no longer used
        self._storage_path = storage_path
        self._chroma = None

    @property
    def chroma(self):
        if self._chroma is None:
            try:
                self._chroma = _get_chroma_memory()
            except Exception as e:
                logger.warning("ExperienceMemory: ChromaMemory init failed: %s", e)
        return self._chroma

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def save_experience(
        self,
        task: str,
        url: str,
        action_history: List[Dict],
        result: Dict[str, Any],
    ) -> None:
        domain = urlparse(url).netloc
        success = result.get("success", False)
        pattern = self._extract_pattern(action_history)

        data_sample = ""
        if success and "data" in result:
            extracted = result["data"].get("extracted_data", [])
            if extracted:
                data_sample = json.dumps(extracted[:3], ensure_ascii=False)[:200]

        # Build a rich text document for semantic search
        content = (
            f"Task: {task}\n"
            f"URL: {url}\n"
            f"Domain: {domain}\n"
            f"Pattern: {pattern}\n"
            f"Success: {success}\n"
            f"Steps: {len(action_history)}\n"
        )
        if data_sample:
            content += f"Data sample: {data_sample}\n"
        if not success and result.get("error"):
            content += f"Error: {result['error']}\n"

        # Serialize action_sequence compactly
        actions_json = json.dumps(action_history[:10], ensure_ascii=False, default=str)[:1000]

        if self.chroma is not None:
            self.chroma.add_memory(
                content=content,
                metadata={
                    "domain": domain,
                    "success": success,
                    "pattern": pattern,
                    "steps_taken": len(action_history),
                    "url": url,
                    "task": task[:500],
                    "error": (result.get("error") or "")[:300],
                    "extracted_data_sample": data_sample[:200],
                    "action_sequence": actions_json,
                },
                memory_type="experience",
                fingerprint=f"exp:{domain}:{task[:200]}:{success}",
                allow_update=True,
                skip_dedup=True,
            )

        status = "success" if success else "failure"
        logger.info("ExperienceMemory: recorded %s | %s | %s", status, domain, pattern)

    # ------------------------------------------------------------------
    # Read — semantic search replaces Jaccard
    # ------------------------------------------------------------------

    def find_similar_experience(
        self,
        task: str,
        url: str,
        only_successful: bool = True,
    ) -> Optional[str]:
        if self.chroma is None:
            return None

        domain = urlparse(url).netloc
        query = f"Task: {task} Domain: {domain}"

        try:
            results = self.chroma.search_memory(
                query,
                n_results=3,
                memory_type="experience",
                include_global_fallback=True,
            )
        except Exception:
            return None

        # Filter by domain and success
        for item in results:
            meta = item.get("metadata") or {}
            if meta.get("domain") != domain:
                continue
            if only_successful and not meta.get("success"):
                continue
            # Build hint from stored metadata
            return self._generate_hint_from_meta(meta, item.get("distance"))

        return None

    def get_domain_statistics(self, domain: str) -> Dict[str, Any]:
        if self.chroma is None:
            return {"total": 0, "success_rate": 0, "common_patterns": []}

        try:
            results = self.chroma._collection.get(
                where={"$and": [{"type": "experience"}, {"domain": domain}]},
            )
        except Exception:
            return {"total": 0, "success_rate": 0, "common_patterns": []}

        metadatas = results.get("metadatas") or []
        if not metadatas:
            return {"total": 0, "success_rate": 0, "common_patterns": []}

        total = len(metadatas)
        successful = sum(1 for m in metadatas if m.get("success"))
        success_rate = successful / total if total else 0

        pattern_counts: Dict[str, int] = {}
        for m in metadatas:
            p = m.get("pattern") or ""
            if p and m.get("success"):
                pattern_counts[p] = pattern_counts.get(p, 0) + 1

        common_patterns = sorted(pattern_counts.items(), key=lambda x: x[1], reverse=True)[:3]

        return {
            "total": total,
            "success_rate": success_rate,
            "successful": successful,
            "failed": total - successful,
            "common_patterns": [p[0] for p in common_patterns],
        }

    def get_failed_patterns(self, domain: str) -> List[str]:
        if self.chroma is None:
            return []

        try:
            results = self.chroma._collection.get(
                where={"$and": [{"type": "experience"}, {"domain": domain}, {"success": False}]},
            )
        except Exception:
            return []

        metadatas = results.get("metadatas") or []
        pattern_counts: Dict[str, int] = {}
        for m in metadatas:
            p = m.get("pattern") or ""
            if p:
                pattern_counts[p] = pattern_counts.get(p, 0) + 1

        return [p for p, count in pattern_counts.items() if count >= 2]

    def clear_old_experiences(self, days: int = 30) -> None:
        if self.chroma is not None:
            removed = self.chroma.evict_stale(max_age_days=days)
            logger.info("ExperienceMemory: evicted %s", removed)

    def export_summary(self, output_path: str = "data/experience_summary.json") -> None:
        if self.chroma is None:
            return

        stats = self.chroma.get_stats()
        try:
            all_results = self.chroma._collection.get(
                where={"type": "experience"},
            )
        except Exception:
            return

        metadatas = all_results.get("metadatas") or []
        domains = set(m.get("domain", "") for m in metadatas if m.get("domain"))

        summary = {
            "total_experiences": len(metadatas),
            "total_domains": len(domains),
            "overall_success_rate": (
                sum(1 for m in metadatas if m.get("success")) / len(metadatas)
                if metadatas else 0
            ),
            "domains": {},
        }

        for domain in domains:
            summary["domains"][domain] = self.get_domain_statistics(domain)

        import os
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

        logger.info("ExperienceMemory: summary exported to %s", output_path)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_pattern(action_history: List[Dict]) -> str:
        if not action_history:
            return "无操作"

        pattern_steps = []
        for action in action_history:
            action_type = action.get("action", "unknown")
            if action_type == "click":
                pattern_steps.append("点击")
            elif action_type == "input":
                pattern_steps.append("输入")
            elif action_type == "scroll":
                pattern_steps.append("滚动")
            elif action_type == "extract":
                pattern_steps.append("提取")

        simplified = []
        prev = None
        count = 0
        for step in pattern_steps:
            if step == prev:
                count += 1
            else:
                if prev:
                    simplified.append(f"{prev}x{count}" if count > 1 else prev)
                prev = step
                count = 1
        if prev:
            simplified.append(f"{prev}x{count}" if count > 1 else prev)

        return " → ".join(simplified) if simplified else "无操作"

    @staticmethod
    def _generate_hint_from_meta(meta: Dict[str, Any], distance: Optional[float] = None) -> str:
        similarity_pct = f"{max(0, 1 - (distance or 0)):.0%}" if distance is not None else "N/A"
        hint = (
            f"发现相似的成功经验 (相似度: {similarity_pct})\n\n"
            f"之前的任务: {meta.get('task', '')}\n"
            f"成功的导航模式: {meta.get('pattern', '')}\n"
            f"执行步数: {meta.get('steps_taken', '?')}\n"
        )

        # Reconstruct action sequence from stored JSON
        actions_raw = meta.get("action_sequence", "")
        if actions_raw:
            try:
                actions = json.loads(actions_raw) if isinstance(actions_raw, str) else actions_raw
                hint += "\n关键步骤:\n"
                for i, action in enumerate(actions[:5], 1):
                    if isinstance(action, dict):
                        action_type = action.get("action", "unknown")
                        reasoning = str(action.get("reasoning", ""))[:60]
                        hint += f"{i}. {action_type}: {reasoning}\n"
                if len(actions) > 5:
                    hint += f"... 还有 {len(actions) - 5} 个步骤\n"
            except (json.JSONDecodeError, TypeError):
                pass

        sample = meta.get("extracted_data_sample", "")
        if sample:
            hint += f"\n提取的数据样本:\n{sample}\n"

        hint += "\n建议: 你可以参考这个模式，但要根据当前页面的实际情况灵活调整。\n"
        return hint
