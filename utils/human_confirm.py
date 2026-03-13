"""
OmniCore 人类确认中断机制
在执行高危操作前强制请求人类确认
"""
from typing import Optional, List
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from config.settings import settings
from utils.enhanced_input import EnhancedInput

console = Console()
_enhanced_input = EnhancedInput()


class HumanConfirm:
    """人类确认机制 - 意图防火墙的核心组件"""

    @staticmethod
    def is_high_risk_operation(operation: str) -> bool:
        """判断是否为高危操作"""
        for risk_op in settings.HIGH_RISK_OPERATIONS:
            if risk_op in operation.lower():
                return True
        return False

    @staticmethod
    def request_confirmation(
        operation: str,
        details: str,
        affected_items: Optional[List[str]] = None,
    ) -> bool:
        """
        请求人类确认

        Args:
            operation: 操作类型
            details: 操作详情
            affected_items: 受影响的项目列表

        Returns:
            bool: 用户是否确认执行
        """
        if not settings.REQUIRE_HUMAN_CONFIRM:
            return True

        # 构建确认面板
        console.print()
        console.print(Panel(
            f"[bold yellow]⚠️ 高危操作确认请求[/bold yellow]\n\n"
            f"[bold]操作类型:[/bold] {operation}\n"
            f"[bold]操作详情:[/bold] {details}",
            title="🛡️ OmniCore 安全防护",
            border_style="yellow",
        ))

        # 如果有受影响的项目，显示列表
        if affected_items:
            table = Table(title="受影响的项目", show_header=True)
            table.add_column("序号", style="cyan", width=6)
            table.add_column("项目", style="white")

            for idx, item in enumerate(affected_items, 1):
                table.add_row(str(idx), item)

            console.print(table)

        console.print()

        # 请求确认 - 使用 EnhancedInput 支持命令历史
        try:
            response = _enhanced_input.input(
                "[bold red]是否确认执行此操作? [y/n] (n): [/bold red]"
            ).strip().lower()

            confirmed = response in ['y', 'yes', '是']

            if not confirmed:
                console.print("[yellow]操作已取消[/yellow]")

            return confirmed
        except (KeyboardInterrupt, EOFError):
            console.print("\n[yellow]操作已取消[/yellow]")
            return False

    @staticmethod
    def request_file_write_confirmation(
        file_path: str,
        content_preview: str,
        is_overwrite: bool = False,
    ) -> bool:
        """文件写入专用确认"""
        operation = "覆盖文件" if is_overwrite else "创建文件"
        details = f"路径: {file_path}\n内容预览:\n{content_preview[:200]}..."

        return HumanConfirm.request_confirmation(
            operation=operation,
            details=details,
            affected_items=[file_path],
        )

    @staticmethod
    def request_web_action_confirmation(
        url: str,
        action: str,
        form_data: Optional[dict] = None,
    ) -> bool:
        """网页操作专用确认（如表单提交）"""
        details = f"目标URL: {url}\n操作: {action}"
        if form_data:
            details += f"\n表单数据: {form_data}"

        return HumanConfirm.request_confirmation(
            operation="网页交互操作",
            details=details,
        )

    @staticmethod
    def request_system_command_confirmation(
        command: str,
        working_dir: str,
    ) -> bool:
        """系统命令执行确认"""
        return HumanConfirm.request_confirmation(
            operation="执行系统命令",
            details=f"命令: {command}\n工作目录: {working_dir}",
        )

    @staticmethod
    def request_browser_action_confirmation(
        action: str,
        target: str,
        value: str = "",
        description: str = "",
    ) -> bool:
        """浏览器操作确认"""
        details = f"操作类型: {action}\n目标元素: {target}"
        if value:
            details += f"\n输入值: {value}"
        if description:
            details += f"\n描述: {description}"

        return HumanConfirm.request_confirmation(
            operation="浏览器操作",
            details=details,
        )
