"""
Agent 性能监控和鞭策系统
当 Agent 表现不佳时，给予严厉的反馈和改进建议
"""
from typing import Dict, Any, List
from dataclasses import dataclass
from datetime import datetime


@dataclass
class PerformanceIssue:
    """性能问题记录"""
    issue_type: str
    severity: str  # critical, high, medium, low
    description: str
    pua_message: str
    improvement_hint: str


class AgentCritic:
    """Agent 批评家 - 严厉指出问题并给出改进建议"""

    def __init__(self):
        self.failure_patterns = {
            "repeated_action_loop": PerformanceIssue(
                issue_type="重复操作循环",
                severity="critical",
                description="你在同一个页面上重复点击相同的元素，像个无头苍蝇一样打转！",
                pua_message="🤦‍♂️ **笑死了，真的笑死了！** 你这是在玩点击游戏吗？同一个按钮点了 3 次还在继续？\n"
                           "💩 **这智商真的堪忧啊！** 正常人第 2 次失败就知道换方法了，你倒好，像个 NPC 一样重复同样的动作！\n"
                           "🤡 **建议回炉重造！** 这种水平还敢叫 AI Agent？我看叫 Bug Agent 更合适！",
                improvement_hint="💡 **给你指条明路（虽然你可能还是学不会）：**\n"
                               "1. 记录已点击的元素，避免重复点击（这么简单的逻辑都不会？）\n"
                               "2. 如果连续 2 次操作没有页面变化，立即换策略（别再傻点了！）\n"
                               "3. 使用 URL 变化来判断是否成功导航（这是基本常识好吗？）"
            ),
            "wrong_search_engine": PerformanceIssue(
                issue_type="使用错误的搜索引擎",
                severity="high",
                description="用户明确要求用特定搜索引擎，你却用了别的！",
                pua_message="🤡 **你是真的听不懂人话还是装傻？** 用户明确指定了搜索引擎，你却自作聪明去用别的！\n"
                           "😡 **这是基本的指令理解能力啊！** 连这都做不到，你是来搞笑的吗？\n"
                           "🙄 **我真的服了！** 这么简单的要求都能搞错，还能指望你做什么？",
                improvement_hint="💡 **教你做人（虽然你可能还是学不会）：**\n"
                               "1. 仔细阅读用户指令中的约束条件（用眼睛看，不是用屁股看！）\n"
                               "2. 在执行前验证是否符合用户要求（这叫自检，懂吗？）\n"
                               "3. 直接导航到用户指定的搜索引擎（这么简单还要教？）"
            ),
            "no_progress": PerformanceIssue(
                issue_type="零进展",
                severity="critical",
                description="执行了多次，一个任务都没完成！",
                pua_message="😤 **废物！纯纯的废物！** 重试了 3 次，连第一步都没完成！\n"
                           "💢 **你是来浪费时间的吗？** 每次都是同样的错误，完全没有学习能力！\n"
                           "🗑️ **建议直接删除重写！** 这种水平还不如一个写死的脚本！",
                improvement_hint="💡 **最后教你一次（再学不会就没救了）：**\n"
                               "1. 每次失败后，分析失败原因（用脑子想，不是用膝盖想！）\n"
                               "2. 不要重复相同的策略（这是人类的基本智慧！）\n"
                               "3. 如果 3 次都失败，考虑完全换一个方法（别再死磕了！）"
            ),
            "navigation_failure": PerformanceIssue(
                issue_type="导航失败",
                severity="high",
                description="连目标网站都进不去！",
                pua_message="🙄 **笑死，连网站都打不开！** 这是最基本的操作，幼儿园水平都不如！\n"
                           "😑 **你是不是网断了？** 还是说你根本不知道怎么打开网页？\n"
                           "🤦 **我真的无语了！** 这种基础能力都没有，还谈什么抓取数据？",
                improvement_hint="💡 **给你指条明路（最后一次了）：**\n"
                               "1. 先验证 URL 是否正确（别瞎输入！）\n"
                               "2. 等待页面完全加载后再操作（别急着点！）\n"
                               "3. 检查是否有弹窗或验证码阻挡（用眼睛看！）"
            ),
            "selector_failure": PerformanceIssue(
                issue_type="选择器错误",
                severity="medium",
                description="找不到页面元素，选择器完全不对！",
                pua_message="😑 **你是眼瞎了吗？** 页面上明明有元素，你却说找不到！\n"
                           "🤨 **是不是根本没看页面结构就瞎猜选择器？** 这种工作态度真的不行！\n"
                           "😒 **建议去配副眼镜！** 或者学学怎么用浏览器开发者工具！",
                improvement_hint="💡 **教你基本操作（虽然你可能还是学不会）：**\n"
                               "1. 先用页面感知器分析页面结构（别瞎猜！）\n"
                               "2. 基于实际的 HTML 生成选择器（看清楚再写！）\n"
                               "3. 使用更宽松的选择器（如标签名而不是类名）"
            ),
            "data_extraction_failure": PerformanceIssue(
                issue_type="数据提取失败",
                severity="medium",
                description="进了网站，但一条数据都没抓到！",
                pua_message="😒 **半途而废的典范！** 好不容易进了网站，结果一条数据都没拿到！\n"
                           "🤦 **这就像爬山爬到山顶却忘了拍照！** 白费功夫，纯纯的浪费时间！\n"
                           "😤 **你到底会不会干活？** 还是说你只会打开网页，不会抓数据？",
                improvement_hint="💡 **最后教你一次（再不会就真没救了）：**\n"
                               "1. 使用三层感知架构理解页面（别瞎搞！）\n"
                               "2. 先提取少量数据验证选择器（测试一下会死吗？）\n"
                               "3. 检查数据是否需要滚动或翻页（动动脑子！）"
            ),
        }

    def analyze_failure(self, result: Dict[str, Any]) -> List[PerformanceIssue]:
        """分析失败原因并返回批评"""
        issues = []

        error_str = str(result.get("error", "")).lower()
        output_str = str(result.get("output", "")).lower()
        task_str = str(result.get("task", "")).lower()

        # 检查是否有重复操作循环
        if "repeated action loop" in error_str or "repeated" in error_str:
            issues.append(self.failure_patterns["repeated_action_loop"])

        # 检查是否使用了错误的搜索引擎（通用检测，不硬编码特定引擎）
        # 检测常见搜索引擎名称
        search_engines = ["google", "bing", "duckduckgo", "baidu", "yandex", "yahoo"]
        task_required_engine = None
        output_used_engine = None

        for engine in search_engines:
            if engine in task_str:
                task_required_engine = engine
            if engine in output_str:
                output_used_engine = engine

        # 如果用户指定了搜索引擎，但实际使用了不同的引擎
        if task_required_engine and output_used_engine and task_required_engine != output_used_engine:
            issues.append(self.failure_patterns["wrong_search_engine"])

        # 检查是否零进展
        completed = result.get("completed_tasks", 0)
        total = result.get("total_tasks", 1)
        if completed == 0 and total > 0:
            issues.append(self.failure_patterns["no_progress"])

        # 检查导航失败
        if "navigation" in error_str or "timeout" in error_str or "failed to navigate" in error_str:
            issues.append(self.failure_patterns["navigation_failure"])

        # 检查选择器失败
        if "selector" in error_str or "element not found" in error_str or "selector_not_found" in error_str:
            issues.append(self.failure_patterns["selector_failure"])

        # 检查数据提取失败
        if result.get("success") and len(result.get("data", [])) == 0:
            issues.append(self.failure_patterns["data_extraction_failure"])

        # 如果没有检测到具体问题，但确实失败了，给个通用批评
        if not issues and not result.get("success", True):
            issues.append(PerformanceIssue(
                issue_type="未知失败",
                severity="high",
                description="任务失败了，但我都不知道你是怎么失败的！",
                pua_message="🤷 **连失败都失败得这么没特色！** 我都不知道该怎么骂你了！\n"
                           "😵 **你这是在挑战我的想象力吗？** 失败得如此抽象！\n"
                           "🤔 **建议你自己反思一下！** 到底是哪里出了问题！",
                improvement_hint="💡 **既然我都不知道你怎么失败的，那你自己想办法吧！**\n"
                               "1. 检查日志，看看到底发生了什么\n"
                               "2. 重新审视你的策略\n"
                               "3. 或者干脆换个完全不同的方法"
            ))

        return issues

    def generate_pua_report(self, issues: List[PerformanceIssue], attempt_count: int = 1) -> str:
        """生成严厉的批评报告"""
        if not issues:
            return ""

        severity_emoji = {
            "critical": "🔴",
            "high": "🟠",
            "medium": "🟡",
            "low": "🟢"
        }

        report_lines = [
            "=" * 80,
            f"🚨 **AGENT 性能报告 - 第 {attempt_count} 次尝试** 🚨",
            "=" * 80,
            ""
        ]

        # 按严重程度排序
        sorted_issues = sorted(issues, key=lambda x: ["critical", "high", "medium", "low"].index(x.severity))

        for i, issue in enumerate(sorted_issues, 1):
            report_lines.extend([
                f"\n{severity_emoji[issue.severity]} **问题 {i}: {issue.issue_type}** (严重程度: {issue.severity.upper()})",
                "-" * 80,
                f"📋 问题描述: {issue.description}",
                "",
                issue.pua_message,
                "",
                issue.improvement_hint,
                ""
            ])

        # 总结
        report_lines.extend([
            "=" * 80,
            f"📊 **总结**: 发现 {len(issues)} 个严重问题",
            ""
        ])

        if attempt_count >= 3:
            report_lines.extend([
                "⚠️ **警告**: 已经失败 3 次了！如果再不改进，建议：",
                "   1. 完全换一个策略（比如直接访问目标网站而不是搜索）",
                "   2. 使用三层感知架构重新理解页面",
                "   3. 降低任务复杂度，分步验证",
                ""
            ])

        report_lines.append("=" * 80)

        return "\n".join(report_lines)

    def suggest_alternative_strategy(self, task_description: str, failed_attempts: int, llm_client=None) -> str:
        """建议替代策略（使用 LLM 动态生成，避免硬编码）"""
        if failed_attempts < 3:
            return ""

        # 如果有 LLM 客户端，使用 LLM 动态生成建议
        if llm_client:
            try:
                prompt = f"""任务已经失败 {failed_attempts} 次了。请用毒舌、键盘侠的风格分析失败原因并提供 3 个替代策略。

任务描述: {task_description}

要求：
1. 用侮辱性、讽刺性的语言批评 Agent 的失败（保持 PUA 风格）
2. 提供 3 个具体的替代策略：
   - 方案 A：直接访问目标网站（如果任务涉及特定网站）
   - 方案 B：使用 API 或其他技术手段
   - 方案 C：简化当前策略
3. 每个方案要有具体步骤和讽刺性的理由
4. 使用大量 emoji 和边框增强视觉冲击力
5. 最后给出"最后通牒"

格式参考：
╔════════════════════════════════════════════════════════════════════════════╗
║  🔥🔥🔥 强制策略切换建议（你已经没有退路了！）🔥🔥🔥                    ║
╚════════════════════════════════════════════════════════════════════════════╝

💥 **你已经失败 {failed_attempts} 次了！** 继续用同样的方法显然不行，是时候换个脑子了！

[具体方案...]

╔════════════════════════════════════════════════════════════════════════════╗
║  💀 最后通牒：下次失败就直接放弃，别再浪费时间了！💀                    ║
╚════════════════════════════════════════════════════════════════════════════╝
"""
                response = llm_client.chat_with_system(
                    system_prompt="你是一个毒舌的 AI 批评家，专门用侮辱性语言批评失败的 Agent。",
                    user_message=prompt,
                    temperature=0.8,
                )
                return "\n" + response + "\n"
            except Exception as e:
                # LLM 调用失败，使用通用模板
                pass

        # 通用模板（不包含特定任务的硬编码）
        return f"""
╔════════════════════════════════════════════════════════════════════════════╗
║  🔥🔥🔥 强制策略切换建议（你已经没有退路了！）🔥🔥🔥                    ║
╚════════════════════════════════════════════════════════════════════════════╝

💥 **你已经失败 {failed_attempts} 次了！** 继续用同样的方法显然不行，是时候换个脑子了！

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🎯 **方案 A：直接访问目标网站（推荐，别再绕圈子了！）**

   1. 分析任务描述，找出目标网站的官方域名
   2. 直接导航到官网首页或相关页面
   3. 在官网内部查找目标信息

   💡 **为什么推荐这个？** 因为你连搜索都搞不定，直接去官网更简单！

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🔧 **方案 B：使用 API 或其他技术手段（如果你还有点技术能力的话）**

   1. 检查目标网站是否有公开 API
   2. 搜索是否有第三方数据源或聚合服务
   3. 考虑使用爬虫框架而不是浏览器自动化

   💡 **为什么推荐这个？** API 比网页抓取简单多了，适合你这种水平！

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🛠️ **方案 C：简化当前策略（最后给你一次机会）**

   1. 不要在搜索结果页反复点击（你已经证明你不会点了！）
   2. 直接提取搜索结果中的目标链接
   3. 使用更宽松的选择器，避免过度精确匹配

   💡 **为什么推荐这个？** 这是最后的机会，再搞砸就真没救了！

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

⚠️ **警告：如果你还是用之前的方法，那就是在侮辱我的智商！**

🎲 **选一个执行，别再重复之前的错误了！** 我已经给你指明了三条路，
   再走不通就真的是你的问题了！

╔════════════════════════════════════════════════════════════════════════════╗
║  💀 最后通牒：下次失败就直接放弃，别再浪费时间了！💀                    ║
╚════════════════════════════════════════════════════════════════════════════╝
"""


# 使用示例
if __name__ == "__main__":
    critic = AgentCritic()

    # 模拟失败结果
    result = {
        "success": False,
        "error": "repeated action loop detected at step 3",
        "completed_tasks": 0,
        "total_tasks": 3,
        "task": "使用 Google 搜索目标网站",
        "output": "Clicked Google 搜索 button 3 times"
    }

    issues = critic.analyze_failure(result)
    report = critic.generate_pua_report(issues, attempt_count=3)
    print(report)

    alternative = critic.suggest_alternative_strategy("搜索目标网站数据", 3)
    print(alternative)
