"""
测试阶段2和阶段3的修复
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.tool_registry import get_builtin_tool_registry
from core.tool_adapters import get_tool_adapter_registry
from utils.logger import console


def test_stage2_browser_agent():
    """测试阶段2：browser_agent 上下文提取"""
    console.print("\n[bold cyan]阶段2测试：browser_agent 上下文提取[/bold cyan]\n")

    # 检查 browser_agent.py 是否有 context_before 和 context_after
    from agents.browser_agent import PageElement

    # 创建测试元素
    element = PageElement(
        index=0,
        tag="button",
        text="Submit",
        element_type="button",
        selector="#submit-btn",
        context_before="Please review your information",
        context_after="or cancel"
    )

    if element.context_before and element.context_after:
        console.print("[green]✅ PageElement 支持上下文字段[/green]")
        console.print(f"  context_before: {element.context_before}")
        console.print(f"  context_after: {element.context_after}")
        return True
    else:
        console.print("[red]❌ PageElement 缺少上下文字段[/red]")
        return False


def test_stage3_enhanced_web_worker():
    """测试阶段3：EnhancedWebWorker 工具注册"""
    console.print("\n[bold cyan]阶段3测试：EnhancedWebWorker 工具注册[/bold cyan]\n")

    # 检查工具注册表
    registry = get_builtin_tool_registry()
    tool = registry.get("web.smart_extract")

    if tool:
        console.print("[green]✅ web.smart_extract 工具已注册[/green]")
        console.print(f"  adapter_name: {tool.adapter_name}")
        console.print(f"  task_type: {tool.spec.task_type}")
        console.print(f"  description: {tool.spec.description}")
    else:
        console.print("[red]❌ web.smart_extract 工具未注册[/red]")
        return False

    # 检查 adapter 注册
    adapter_registry = get_tool_adapter_registry()
    adapter = adapter_registry.get("enhanced_web_worker")

    if adapter:
        console.print("[green]✅ enhanced_web_worker adapter 已注册[/green]")
        console.print(f"  adapter 类型: {type(adapter).__name__}")
        return True
    else:
        console.print("[red]❌ enhanced_web_worker adapter 未注册[/red]")
        return False


def test_enhanced_web_worker_import():
    """测试 EnhancedWebWorker 是否可以导入"""
    console.print("\n[bold cyan]测试 EnhancedWebWorker 导入[/bold cyan]\n")

    try:
        from agents.enhanced_web_worker import EnhancedWebWorker
        console.print("[green]✅ EnhancedWebWorker 导入成功[/green]")

        # 检查方法
        worker = EnhancedWebWorker()
        if hasattr(worker, 'smart_extract'):
            console.print("[green]✅ smart_extract 方法存在[/green]")
            return True
        else:
            console.print("[red]❌ smart_extract 方法不存在[/red]")
            return False
    except Exception as e:
        console.print(f"[red]❌ EnhancedWebWorker 导入失败: {e}[/red]")
        return False


def main():
    console.print("\n[bold magenta]" + "=" * 80 + "[/bold magenta]")
    console.print("[bold magenta]阶段2和阶段3修复验证[/bold magenta]")
    console.print("[bold magenta]" + "=" * 80 + "[/bold magenta]\n")

    results = []

    # 测试阶段2
    try:
        result1 = test_stage2_browser_agent()
        results.append(("阶段2: browser_agent 上下文", result1))
    except Exception as e:
        console.print(f"[red]阶段2测试异常: {e}[/red]")
        results.append(("阶段2: browser_agent 上下文", False))

    # 测试 EnhancedWebWorker 导入
    try:
        result2 = test_enhanced_web_worker_import()
        results.append(("EnhancedWebWorker 导入", result2))
    except Exception as e:
        console.print(f"[red]EnhancedWebWorker 导入测试异常: {e}[/red]")
        results.append(("EnhancedWebWorker 导入", False))

    # 测试阶段3
    try:
        result3 = test_stage3_enhanced_web_worker()
        results.append(("阶段3: EnhancedWebWorker 注册", result3))
    except Exception as e:
        console.print(f"[red]阶段3测试异常: {e}[/red]")
        results.append(("阶段3: EnhancedWebWorker 注册", False))

    # 总结
    console.print("\n" + "=" * 80)
    console.print("[bold magenta]测试总结[/bold magenta]")
    console.print("=" * 80 + "\n")

    passed = sum(1 for _, result in results if result)
    total = len(results)

    for name, result in results:
        status = "[green]✅ 通过[/green]" if result else "[red]❌ 失败[/red]"
        console.print(f"  {name}: {status}")

    console.print(f"\n[bold]总计: {passed}/{total} 通过[/bold]")

    if passed == total:
        console.print("\n[bold green]🎉 所有测试通过！阶段2和阶段3修复成功！[/bold green]")
    else:
        console.print("\n[bold yellow]⚠️  部分测试失败，需要进一步调试[/bold yellow]")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]测试被用户中断[/yellow]")
    except Exception as e:
        console.print(f"\n[bold red]测试执行失败: {e}[/bold red]")
        import traceback
        traceback.print_exc()
