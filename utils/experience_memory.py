"""
经验记忆系统 - 从成功和失败中学习

不是硬编码领域知识，而是让Agent从实际执行中积累经验
类似人类的学习过程
"""

import json
import os
from datetime import datetime
from typing import List, Dict, Any, Optional
from urllib.parse import urlparse
from dataclasses import dataclass, asdict
import hashlib


@dataclass
class Experience:
    """一次任务执行的经验记录"""
    task: str  # 任务描述
    url: str  # 起始URL
    domain: str  # 域名
    success: bool  # 是否成功
    steps_taken: int  # 执行步数
    action_sequence: List[Dict]  # 动作序列
    pattern: str  # 提取的模式
    timestamp: str  # 时间戳
    error: str = ""  # 错误信息（如果失败）
    extracted_data_sample: str = ""  # 提取的数据样本

    def to_dict(self) -> Dict:
        return asdict(self)


class ExperienceMemory:
    """
    经验记忆系统

    功能：
    1. 记录每次任务执行的结果
    2. 从成功经验中提取模式
    3. 为新任务提供相似经验的提示
    4. 避免重复失败的策略
    """

    def __init__(self, storage_path: str = "data/agent_experiences.json"):
        self.storage_path = storage_path
        self.experiences: List[Experience] = []
        self._ensure_storage_dir()
        self._load_experiences()

    def _ensure_storage_dir(self):
        """确保存储目录存在"""
        os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)

    def _load_experiences(self):
        """从磁盘加载经验"""
        if not os.path.exists(self.storage_path):
            self.experiences = []
            return

        try:
            with open(self.storage_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
                self.experiences = [
                    Experience(**exp) for exp in data
                ]
            print(f"✓ 加载了 {len(self.experiences)} 条历史经验")
        except Exception as e:
            print(f"⚠️ 加载经验失败: {e}")
            self.experiences = []

    def _save_to_disk(self):
        """保存经验到磁盘"""
        try:
            with open(self.storage_path, 'w', encoding='utf-8') as f:
                data = [exp.to_dict() for exp in self.experiences]
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"⚠️ 保存经验失败: {e}")

    def save_experience(
        self,
        task: str,
        url: str,
        action_history: List[Dict],
        result: Dict[str, Any]
    ):
        """
        保存一次任务执行的经验

        Args:
            task: 任务描述
            url: 起始URL
            action_history: 执行的动作序列
            result: 最终结果
        """
        domain = urlparse(url).netloc
        success = result.get("success", False)

        # 提取模式
        pattern = self._extract_pattern(action_history)

        # 提取数据样本
        data_sample = ""
        if success and "data" in result:
            extracted = result["data"].get("extracted_data", [])
            if extracted:
                # 取前3条作为样本
                data_sample = json.dumps(extracted[:3], ensure_ascii=False)[:200]

        experience = Experience(
            task=task,
            url=url,
            domain=domain,
            success=success,
            steps_taken=len(action_history),
            action_sequence=action_history,
            pattern=pattern,
            timestamp=datetime.now().isoformat(),
            error=result.get("error", ""),
            extracted_data_sample=data_sample
        )

        self.experiences.append(experience)
        self._save_to_disk()

        status = "✓ 成功" if success else "✗ 失败"
        print(f"📝 记录经验: {status} | {domain} | {pattern}")

    def find_similar_experience(
        self,
        task: str,
        url: str,
        only_successful: bool = True
    ) -> Optional[str]:
        """
        查找类似任务的经验

        Args:
            task: 当前任务
            url: 当前URL
            only_successful: 是否只查找成功的经验

        Returns:
            经验提示文本（如果找到）
        """
        domain = urlparse(url).netloc

        # 过滤：同域名的经验
        candidates = [
            exp for exp in self.experiences
            if exp.domain == domain
        ]

        if only_successful:
            candidates = [exp for exp in candidates if exp.success]

        if not candidates:
            return None

        # 找最相似的
        best_match = max(
            candidates,
            key=lambda x: self._calculate_similarity(task, x.task)
        )

        # 如果相似度太低，不返回
        similarity = self._calculate_similarity(task, best_match.task)
        if similarity < 0.3:
            return None

        # 生成提示
        hint = self._generate_hint(best_match, similarity)
        return hint

    def get_domain_statistics(self, domain: str) -> Dict[str, Any]:
        """
        获取某个域名的统计信息

        Args:
            domain: 域名

        Returns:
            统计信息
        """
        domain_exps = [exp for exp in self.experiences if exp.domain == domain]

        if not domain_exps:
            return {
                "total": 0,
                "success_rate": 0,
                "common_patterns": []
            }

        total = len(domain_exps)
        successful = sum(1 for exp in domain_exps if exp.success)
        success_rate = successful / total if total > 0 else 0

        # 统计常见模式
        patterns = [exp.pattern for exp in domain_exps if exp.success]
        pattern_counts = {}
        for p in patterns:
            pattern_counts[p] = pattern_counts.get(p, 0) + 1

        common_patterns = sorted(
            pattern_counts.items(),
            key=lambda x: x[1],
            reverse=True
        )[:3]

        return {
            "total": total,
            "success_rate": success_rate,
            "successful": successful,
            "failed": total - successful,
            "common_patterns": [p[0] for p in common_patterns]
        }

    def _extract_pattern(self, action_history: List[Dict]) -> str:
        """
        从动作序列中提取模式

        例如：
        - "导航 → 点击链接 → 列表页 → 提取"
        - "搜索 → 输入 → 点击搜索 → 结果页 → 提取"
        """
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

        # 简化：合并连续的相同操作
        simplified = []
        prev = None
        count = 0

        for step in pattern_steps:
            if step == prev:
                count += 1
            else:
                if prev:
                    if count > 1:
                        simplified.append(f"{prev}×{count}")
                    else:
                        simplified.append(prev)
                prev = step
                count = 1

        # 添加最后一个
        if prev:
            if count > 1:
                simplified.append(f"{prev}×{count}")
            else:
                simplified.append(prev)

        return " → ".join(simplified) if simplified else "无操作"

    def _calculate_similarity(self, task1: str, task2: str) -> float:
        """
        计算两个任务的相似度（简单的关键词匹配）

        Returns:
            相似度 0-1
        """
        # 转小写
        t1 = task1.lower()
        t2 = task2.lower()

        # 提取关键词（简单分词）
        words1 = set(t1.split())
        words2 = set(t2.split())

        # Jaccard相似度
        intersection = words1 & words2
        union = words1 | words2

        if not union:
            return 0.0

        return len(intersection) / len(union)

    def _generate_hint(self, experience: Experience, similarity: float) -> str:
        """
        根据经验生成提示

        Args:
            experience: 历史经验
            similarity: 相似度

        Returns:
            提示文本
        """
        hint = f"""
💡 **发现相似的成功经验** (相似度: {similarity:.0%})

之前的任务: {experience.task}
成功的导航模式: {experience.pattern}
执行步数: {experience.steps_taken}

关键步骤：
"""

        # 列出关键步骤
        for i, action in enumerate(experience.action_sequence[:5], 1):
            action_type = action.get("action", "unknown")
            reasoning = action.get("reasoning", "")[:60]
            hint += f"{i}. {action_type}: {reasoning}\n"

        if len(experience.action_sequence) > 5:
            hint += f"... 还有 {len(experience.action_sequence) - 5} 个步骤\n"

        hint += f"""
提取的数据样本:
{experience.extracted_data_sample}

💡 建议: 你可以参考这个模式，但要根据当前页面的实际情况灵活调整。
"""

        return hint

    def get_failed_patterns(self, domain: str) -> List[str]:
        """
        获取某个域名上失败的模式（用于避免重复错误）

        Args:
            domain: 域名

        Returns:
            失败的模式列表
        """
        failed_exps = [
            exp for exp in self.experiences
            if exp.domain == domain and not exp.success
        ]

        patterns = [exp.pattern for exp in failed_exps]

        # 统计频率
        pattern_counts = {}
        for p in patterns:
            pattern_counts[p] = pattern_counts.get(p, 0) + 1

        # 返回出现2次以上的失败模式
        return [
            p for p, count in pattern_counts.items()
            if count >= 2
        ]

    def clear_old_experiences(self, days: int = 30):
        """
        清理旧的经验记录

        Args:
            days: 保留最近多少天的记录
        """
        from datetime import timedelta

        cutoff = datetime.now() - timedelta(days=days)

        original_count = len(self.experiences)

        self.experiences = [
            exp for exp in self.experiences
            if datetime.fromisoformat(exp.timestamp) > cutoff
        ]

        removed = original_count - len(self.experiences)

        if removed > 0:
            self._save_to_disk()
            print(f"🗑️ 清理了 {removed} 条旧经验记录")

    def export_summary(self, output_path: str = "data/experience_summary.json"):
        """
        导出经验摘要（用于分析）

        Args:
            output_path: 输出文件路径
        """
        # 按域名分组统计
        domains = set(exp.domain for exp in self.experiences)

        summary = {
            "total_experiences": len(self.experiences),
            "total_domains": len(domains),
            "overall_success_rate": sum(1 for e in self.experiences if e.success) / len(self.experiences) if self.experiences else 0,
            "domains": {}
        }

        for domain in domains:
            summary["domains"][domain] = self.get_domain_statistics(domain)

        # 保存
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

        print(f"📊 经验摘要已导出到: {output_path}")
