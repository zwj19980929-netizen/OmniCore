"""
AI 驱动的 PUA 教练 - 用大模型来骂，变着花样骂
不仅骂，还要反思，还要给出具体的改进方案
"""
import os
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from core.llm import LLMClient
from utils.logger import console


@dataclass
class TaskStep:
    """任务步骤"""
    step_no: int
    action: str
    expected: str
    actual: str
    success: bool
    error: str = ""


class AIPUACoach:
    """
    AI 驱动的 PUA 教练

    特点：
    1. 用大模型生成个性化的批评，不是固定模板
    2. 根据具体情况变着花样骂
    3. 不仅骂，还要反思失败原因
    4. 给出具体的改进方案
    """

    def __init__(self):
        self.llm = LLMClient()
        self.step_history: List[TaskStep] = []
        self.failure_count = 0
        self.success_count = 0
        self.prompt_template = self._load_prompt_template()

    def _load_prompt_template(self) -> str:
        """加载 PUA 教练的 prompt 模板"""
        prompt_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "prompts",
            "ai_pua_coach.txt"
        )
        try:
            with open(prompt_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            console.print(f"[yellow]警告：无法加载 PUA 教练 prompt 文件: {e}[/yellow]")
            # 返回一个简化的默认 prompt
            return """你是一个毒舌但有效的 AI 教练。批评这个失败的 AI Agent：

任务：{context}
步骤 {step_no}：{action}
期望：{expected}
实际：{actual}
错误：{error}

请生成批评，包含：开场白、失败分析、反思、建议、下一步行动。
要毒舌但有建设性，用 emoji 和 markdown 格式。"""

    def evaluate_step(
        self,
        step_no: int,
        action: str,
        expected: str,
        actual_result: Dict[str, Any],
        task_context: str = ""
    ) -> str:
        """
        评估单个步骤并生成 AI 驱动的批评

        Args:
            step_no: 步骤编号
            action: 执行的动作
            expected: 期望的结果
            actual_result: 实际结果（字典或字符串）
            task_context: 任务上下文

        Returns:
            AI 生成的评论
        """
        # 🔥 修复：处理 actual_result 可能是字符串的情况
        if isinstance(actual_result, str):
            # 如果是字符串，转换为字典格式
            actual_result = {
                "success": False,
                "error": actual_result,
                "output": actual_result
            }
        elif not isinstance(actual_result, dict):
            # 如果既不是字符串也不是字典，转换为字典
            actual_result = {
                "success": False,
                "error": str(actual_result),
                "output": str(actual_result)
            }

        success = actual_result.get("success", False)
        actual = str(actual_result.get("output", "") or actual_result.get("error", ""))
        error = str(actual_result.get("error", ""))

        # 🔥 检查数据相关性：即使 success=True，也要验证数据是否真的回答了问题
        data = actual_result.get("data", [])
        if success and data:
            # 如果有数据，检查数据是否相关
            # 简单启发式：检查数据样本是否包含任务关键词
            task_keywords = set(task_context.lower().split())
            data_sample_str = str(data[:3]).lower()

            # 如果数据样本中几乎没有任务关键词，可能是不相关的数据
            keyword_matches = sum(1 for kw in task_keywords if len(kw) > 2 and kw in data_sample_str)
            if keyword_matches < 2:
                # 数据可能不相关，标记为失败
                success = False
                error = f"数据质量可疑：抓取到 {len(data)} 条数据，但内容可能与任务不相关"
                actual = f"获取了 {len(data)} 条数据，但质量存疑"

        step = TaskStep(
            step_no=step_no,
            action=action,
            expected=expected,
            actual=actual,
            success=success,
            error=error
        )
        self.step_history.append(step)

        if success:
            self.success_count += 1
            return self._generate_success_comment(step, task_context)
        else:
            self.failure_count += 1
            return self._generate_ai_pua(step, task_context)

    def _generate_success_comment(self, step: TaskStep, context: str) -> str:
        """生成成功时的简短鼓励"""
        return f"""
🎯 **第 {step.step_no} 步：{step.action}**
✅ 可以，这次做对了！继续保持！
"""

    def _generate_ai_pua(self, step: TaskStep, context: str) -> str:
        """
        用大模型生成个性化的 PUA 批评

        关键：让 LLM 根据具体情况生成批评，而不是固定模板
        """
        # 收集历史失败信息
        recent_failures = [s for s in self.step_history[-5:] if not s.success]

        # 检查是否重复失败
        is_repeated = False
        if len(recent_failures) >= 2:
            if recent_failures[-1].action == recent_failures[-2].action:
                is_repeated = True

        # 计算成功率
        success_rate = (self.success_count / len(self.step_history) * 100) if self.step_history else 0

        # 使用模板构建 prompt
        prompt = self.prompt_template.format(
            context=context,
            step_no=step.step_no,
            action=step.action,
            expected=step.expected,
            actual=step.actual,
            error=step.error,
            total_steps=len(self.step_history),
            success_count=self.success_count,
            failure_count=self.failure_count,
            success_rate=f"{success_rate:.1f}",
            repeated_failure_note="是的！这个操作之前就失败过，它还在重复同样的错误！" if is_repeated else "不是重复失败",
            recent_failures=self._format_recent_failures(recent_failures)
        )

        try:
            # 🔥 修复：chat() 方法需要消息列表，不是字符串
            response = self.llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.8,  # 高温度，增加创造性
                max_tokens=2048
            )

            # 🔥 修复：从 LLMResponse 对象中提取 content
            response_content = response.content if hasattr(response, 'content') else str(response)

            return f"""
╔════════════════════════════════════════════════════════════════════════════╗
║  💥 第 {step.step_no} 步失败分析
╚════════════════════════════════════════════════════════════════════════════╝

{response_content}

╔════════════════════════════════════════════════════════════════════════════╗
║  📊 当前成功率：{success_rate:.1f}%
╚════════════════════════════════════════════════════════════════════════════╝
"""
        except Exception as e:
            # 如果 LLM 调用失败，返回简单的批评
            return f"""
╔════════════════════════════════════════════════════════════════════════════╗
║  💥 第 {step.step_no} 步失败
╚════════════════════════════════════════════════════════════════════════════╝

❌ **又失败了！**

📋 期望：{step.expected}
💔 实际：{step.actual}

💡 **建议：** 分析错误原因，换个方法试试！

╚════════════════════════════════════════════════════════════════════════════╝
"""

    def _format_recent_failures(self, failures: List[TaskStep]) -> str:
        """格式化最近的失败记录"""
        if not failures:
            return "无"

        lines = []
        for f in failures[-3:]:
            lines.append(f"- 第 {f.step_no} 步：{f.action} → {f.error[:100]}")
        return "\n".join(lines)

    def generate_progress_report(self, task_description: str) -> str:
        """生成 AI 驱动的进度报告"""
        if not self.step_history:
            return "还没开始呢，别催！"

        total_steps = len(self.step_history)
        success_rate = (self.success_count / total_steps * 100) if total_steps > 0 else 0

        # 用 LLM 生成个性化的进度评价
        prompt = f"""你是一个毒舌教练。根据以下数据，生成一段简短的进度评价（2-3 句话）：

任务：{task_description}
总步骤：{total_steps}
成功：{self.success_count}
失败：{self.failure_count}
成功率：{success_rate:.1f}%

要求：
- 根据成功率给出评价（<30% 很差，30-60% 一般，>60% 不错）
- 语气要毒舌但有建设性
- 给出简短的建议
- 用 emoji

直接输出评价，不要有前缀。
"""

        try:
            # 🔥 修复：chat() 方法需要消息列表，不是字符串
            response = self.llm.chat(
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=512
            )
            # 🔥 修复：从 LLMResponse 对象中提取 content
            evaluation = response.content if hasattr(response, 'content') else str(response)
        except Exception:
            evaluation = f"成功率 {success_rate:.1f}%，还需要努力！"

        return f"""
╔════════════════════════════════════════════════════════════════════════════╗
║  📊 任务进度报告
╚════════════════════════════════════════════════════════════════════════════╝

🎯 **任务：** {task_description[:100]}

📈 **统计数据：**
   - 总步骤数：{total_steps}
   - 成功：{self.success_count} ✅
   - 失败：{self.failure_count} ❌
   - 成功率：{success_rate:.1f}%

💬 **教练评价：**
{evaluation}

╔════════════════════════════════════════════════════════════════════════════╗
║  💪 继续努力！
╚════════════════════════════════════════════════════════════════════════════╝
"""


# 全局教练实例
_global_ai_coach: Optional[AIPUACoach] = None


def get_ai_coach() -> AIPUACoach:
    """获取全局 AI 教练实例"""
    global _global_ai_coach
    if _global_ai_coach is None:
        _global_ai_coach = AIPUACoach()
    return _global_ai_coach


def reset_ai_coach():
    """重置教练（新任务开始时调用）"""
    global _global_ai_coach
    _global_ai_coach = AIPUACoach()
