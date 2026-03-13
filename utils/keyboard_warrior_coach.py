"""
键盘侠教练 - 全局任务监督和引导系统
刀子嘴豆腐心，边喷边教，推进任务完成
"""
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from enum import Enum


class CoachTone(Enum):
    """教练语气类型"""
    HARSH = "harsh"  # 严厉批评（失败多次）
    SARCASTIC = "sarcastic"  # 讽刺挖苦（做得不好）
    ENCOURAGING = "encouraging"  # 鼓励引导（有进展）
    IMPATIENT = "impatient"  # 不耐烦（拖延）


@dataclass
class TaskStep:
    """任务步骤"""
    step_no: int
    action: str
    expected: str
    actual: str
    success: bool
    duration: float = 0.0


class KeyboardWarriorCoach:
    """
    键盘侠教练 - 全局监督者

    职责：
    1. 监督每一步操作
    2. 评估结果是否合理
    3. 给予引导性的批评
    4. 推进任务向前
    """

    def __init__(self):
        self.step_history: List[TaskStep] = []
        self.failure_count = 0
        self.success_count = 0
        self.last_progress_step = 0

    def evaluate_step(
        self,
        step_no: int,
        action: str,
        expected: str,
        actual_result: Dict[str, Any],
        task_context: str = ""
    ) -> str:
        """
        评估单个步骤的执行结果

        Args:
            step_no: 步骤编号
            action: 执行的动作
            expected: 期望的结果
            actual_result: 实际结果
            task_context: 任务上下文

        Returns:
            键盘侠教练的评论
        """
        success = actual_result.get("success", False)
        actual = str(actual_result.get("output", "") or actual_result.get("error", ""))

        step = TaskStep(
            step_no=step_no,
            action=action,
            expected=expected,
            actual=actual,
            success=success
        )
        self.step_history.append(step)

        if success:
            self.success_count += 1
            self.last_progress_step = step_no
            return self._generate_success_comment(step, task_context)
        else:
            self.failure_count += 1
            return self._generate_failure_comment(step, task_context)

    def _generate_success_comment(self, step: TaskStep, context: str) -> str:
        """生成成功时的评论（鼓励但不过分）"""
        comments = [
            f"🎯 **第 {step.step_no} 步：{step.action}**\n"
            f"✅ 行吧，这次总算做对了！\n"
            f"💡 期望：{step.expected}\n"
            f"📊 实际：成功\n"
            f"👉 **下一步该干嘛？** 别愣着，继续往下走！",

            f"🎯 **第 {step.step_no} 步：{step.action}**\n"
            f"✅ 嗯，还算有点进步，不过别骄傲！\n"
            f"💡 这次做对了：{step.expected}\n"
            f"👉 **接下来：** 保持这个水平，继续下一步！",

            f"🎯 **第 {step.step_no} 步：{step.action}**\n"
            f"✅ 可以，终于不用我操心了！\n"
            f"💡 完成了：{step.expected}\n"
            f"👉 **别停！** 趁热打铁，赶紧做下一步！",
        ]

        import random
        return random.choice(comments)

    def _generate_failure_comment(self, step: TaskStep, context: str) -> str:
        """生成失败时的评论（引导型批评）"""
        # 判断失败类型
        failure_type = self._classify_failure(step)

        if failure_type == "repeated_mistake":
            return self._comment_repeated_mistake(step, context)
        elif failure_type == "wrong_direction":
            return self._comment_wrong_direction(step, context)
        elif failure_type == "incomplete":
            return self._comment_incomplete(step, context)
        elif failure_type == "timeout":
            return self._comment_timeout(step, context)
        else:
            return self._comment_generic_failure(step, context)

    def _classify_failure(self, step: TaskStep) -> str:
        """分类失败类型"""
        actual_lower = step.actual.lower()

        # 检查是否重复错误
        if len(self.step_history) >= 2:
            prev_step = self.step_history[-2]
            if prev_step.action == step.action and not prev_step.success:
                return "repeated_mistake"

        # 检查是否方向错误
        if "wrong" in actual_lower or "incorrect" in actual_lower:
            return "wrong_direction"

        # 检查是否不完整
        if "empty" in actual_lower or "no data" in actual_lower or "not found" in actual_lower:
            return "incomplete"

        # 检查是否超时
        if "timeout" in actual_lower or "timed out" in actual_lower:
            return "timeout"

        return "generic"

    def _comment_repeated_mistake(self, step: TaskStep, context: str) -> str:
        """重复错误的评论"""
        return f"""
╔════════════════════════════════════════════════════════════════════════════╗
║  🤦 第 {step.step_no} 步：{step.action}
╚════════════════════════════════════════════════════════════════════════════╝

❌ **又失败了！而且是同样的错误！**

💩 **你是金鱼记忆吗？** 刚才这个操作就失败了，你还要再试一次？
😤 **定义：** 重复做同样的事情却期待不同的结果，这叫什么？这叫愚蠢！

📋 **你想要：** {step.expected}
💔 **实际结果：** {step.actual[:200]}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💡 **我教你怎么做（最后一次了）：**

1. **停止重复！** 这个方法已经证明不行了，换一个！
2. **分析原因：** 为什么失败？是参数不对？还是方法本身就错了？
3. **换个思路：**
   - 如果是网页操作失败 → 换个选择器或者直接用 URL 导航
   - 如果是数据提取失败 → 先看看页面结构，别瞎猜
   - 如果是超时 → 增加等待时间或者换个更快的方法

👉 **下一步建议：** {self._suggest_next_action(step, context)}

╔════════════════════════════════════════════════════════════════════════════╗
║  ⚠️ 警告：再重复同样的错误，我就不管你了！
╚════════════════════════════════════════════════════════════════════════════╝
"""

    def _comment_wrong_direction(self, step: TaskStep, context: str) -> str:
        """方向错误的评论"""
        return f"""
╔════════════════════════════════════════════════════════════════════════════╗
║  🧭 第 {step.step_no} 步：{step.action}
╚════════════════════════════════════════════════════════════════════════════╝

❌ **方向错了！你走偏了！**

🤔 **你这是要去哪？** 任务目标是 "{context[:100]}"，你现在做的事情根本不对路！
😑 **就像去北京却往南走** - 方向都错了，走得再快也没用！

📋 **你想要：** {step.expected}
💔 **实际结果：** {step.actual[:200]}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💡 **回到正轨（听好了）：**

1. **重新审视目标：** 用户到底要什么？
2. **检查当前位置：** 你现在在哪？离目标有多远？
3. **调整方向：**
   - 如果目标是抓取数据 → 先确保你在正确的页面上
   - 如果目标是操作网页 → 先确认你找对了元素
   - 如果目标是保存文件 → 先确保你有数据可以保存

👉 **正确的下一步：** {self._suggest_next_action(step, context)}

╔════════════════════════════════════════════════════════════════════════════╗
║  🎯 记住：方向比速度更重要！
╚════════════════════════════════════════════════════════════════════════════╝
"""

    def _comment_incomplete(self, step: TaskStep, context: str) -> str:
        """不完整的评论"""
        return f"""
╔════════════════════════════════════════════════════════════════════════════╗
║  📦 第 {step.step_no} 步：{step.action}
╚════════════════════════════════════════════════════════════════════════════╝

❌ **半途而废！结果不完整！**

😒 **就这？** 你这是在敷衍我吗？任务做了一半就停了？
🤨 **期望 vs 现实：**
   - 期望：{step.expected}
   - 实际：空的/不完整/没数据

💔 **实际结果：** {step.actual[:200]}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💡 **补救措施（赶紧的）：**

1. **检查为什么是空的：**
   - 页面还没加载完？→ 等一等再提取
   - 选择器不对？→ 用页面感知器重新分析
   - 数据根本不在这个页面？→ 换个页面

2. **确保数据完整：**
   - 如果是列表 → 检查是否需要翻页或滚动
   - 如果是详情 → 检查是否需要点击进入
   - 如果是表格 → 检查是否需要展开

3. **验证结果：**
   - 提取到的数据量够吗？
   - 数据格式对吗？
   - 关键字段都有吗？

👉 **下一步行动：** {self._suggest_next_action(step, context)}

╔════════════════════════════════════════════════════════════════════════════╗
║  💪 别半途而废，做就做完整！
╚════════════════════════════════════════════════════════════════════════════╝
"""

    def _comment_timeout(self, step: TaskStep, context: str) -> str:
        """超时的评论"""
        return f"""
╔════════════════════════════════════════════════════════════════════════════╗
║  ⏰ 第 {step.step_no} 步：{step.action}
╚════════════════════════════════════════════════════════════════════════════╝

❌ **超时了！你在等什么？**

😤 **效率呢？** 一个操作等这么久，是网速慢还是你的方法有问题？
⏳ **时间就是金钱！** 不能一直等下去！

📋 **你想要：** {step.expected}
💔 **实际结果：** 超时 - {step.actual[:200]}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💡 **提速方案（选一个）：**

1. **增加超时时间** - 但这是下策，治标不治本
2. **换个更快的方法：**
   - 如果是等页面加载 → 用更精确的等待条件
   - 如果是等元素出现 → 检查选择器是否正确
   - 如果是等数据返回 → 考虑用 API 而不是网页抓取

3. **优化策略：**
   - 减少不必要的等待
   - 并行处理多个任务
   - 使用更轻量的方法

👉 **快速解决方案：** {self._suggest_next_action(step, context)}

╔════════════════════════════════════════════════════════════════════════════╗
║  ⚡ 快点！别磨蹭！
╚════════════════════════════════════════════════════════════════════════════╝
"""

    def _comment_generic_failure(self, step: TaskStep, context: str) -> str:
        """通用失败评论"""
        return f"""
╔════════════════════════════════════════════════════════════════════════════╗
║  ❌ 第 {step.step_no} 步：{step.action}
╚════════════════════════════════════════════════════════════════════════════╝

❌ **失败了！**

😑 **又搞砸了！** 虽然我不确定具体哪里出问题，但失败就是失败！

📋 **你想要：** {step.expected}
💔 **实际结果：** {step.actual[:200]}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

💡 **排查步骤（一步步来）：**

1. **看日志** - 到底报了什么错？
2. **检查输入** - 参数对不对？
3. **验证环境** - 网络通不通？权限够不够？
4. **简化问题** - 能不能用更简单的方法达到目的？

👉 **建议：** {self._suggest_next_action(step, context)}

╔════════════════════════════════════════════════════════════════════════════╗
║  🔍 失败不可怕，可怕的是不知道为什么失败！
╚════════════════════════════════════════════════════════════════════════════╝
"""

    def _suggest_next_action(self, step: TaskStep, context: str) -> str:
        """根据失败情况建议下一步行动"""
        action_lower = step.action.lower()

        if "click" in action_lower or "navigate" in action_lower:
            return "换个选择器，或者直接用 URL 导航到目标页面"
        elif "extract" in action_lower or "scrape" in action_lower:
            return "先用页面感知器分析页面结构，再生成正确的选择器"
        elif "search" in action_lower:
            return "换个搜索引擎或者直接访问目标网站"
        elif "save" in action_lower or "write" in action_lower:
            return "检查数据是否存在，确保有内容可以保存"
        else:
            return "重新审视任务目标，换个完全不同的方法"

    def generate_progress_report(self, task_description: str) -> str:
        """生成进度报告"""
        total_steps = len(self.step_history)
        if total_steps == 0:
            return "还没开始呢，别催！"

        success_rate = (self.success_count / total_steps * 100) if total_steps > 0 else 0
        stagnant_steps = total_steps - self.last_progress_step

        report = f"""
╔════════════════════════════════════════════════════════════════════════════╗
║  📊 任务进度报告
╚════════════════════════════════════════════════════════════════════════════╝

🎯 **任务：** {task_description[:100]}

📈 **统计数据：**
   - 总步骤数：{total_steps}
   - 成功：{self.success_count} ✅
   - 失败：{self.failure_count} ❌
   - 成功率：{success_rate:.1f}%

"""

        if success_rate < 30:
            report += """
😤 **评价：太差了！**
   这成功率简直惨不忍睹！你是来搞笑的吗？

💡 **建议：** 完全换个策略吧，这条路明显走不通！
"""
        elif success_rate < 60:
            report += """
😑 **评价：勉强及格**
   成功率不到 60%，这水平还需要提高！

💡 **建议：** 分析一下失败的步骤，找出规律，别再犯同样的错！
"""
        else:
            report += """
😊 **评价：还不错**
   成功率过半了，继续保持！

💡 **建议：** 保持这个势头，争取一次性成功！
"""

        if stagnant_steps > 3:
            report += f"""
⚠️ **警告：** 已经 {stagnant_steps} 步没有进展了！
   你是在原地打转吗？赶紧换个方法！
"""

        report += """
╔════════════════════════════════════════════════════════════════════════════╗
║  💪 加油！别放弃！
╚════════════════════════════════════════════════════════════════════════════╝
"""

        return report


# 全局教练实例
_global_coach: Optional[KeyboardWarriorCoach] = None


def get_coach() -> KeyboardWarriorCoach:
    """获取全局教练实例"""
    global _global_coach
    if _global_coach is None:
        _global_coach = KeyboardWarriorCoach()
    return _global_coach


def reset_coach():
    """重置教练（新任务开始时调用）"""
    global _global_coach
    _global_coach = KeyboardWarriorCoach()
