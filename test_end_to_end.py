"""
端到端测试：模拟真实用户任务
测试完整的流程：Router → WebWorker → 结果返回
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.orchestrator import OmniCoreOrchestrator


async def test_end_to_end():
    """端到端测试：完成一个真实任务"""
    print("\n" + "="*70)
    print("端到端测试：完整任务流程验证")
    print("="*70)

    # 创建 Orchestrator
    orchestrator = OmniCoreOrchestrator()

    # 测试任务
    test_query = "帮我找到 numpy 的 GitHub 地址"

    print(f"\n📝 用户输入: {test_query}")
    print("-" * 70)

    try:
        # 执行任务
        print("\n🚀 开始执行任务...")
        result = await orchestrator.run(test_query)

        # 检查结果
        print("\n" + "="*70)
        print("📊 执行结果")
        print("="*70)

        if result:
            print(f"\n✅ 任务执行成功！")
            print(f"\n最终答案:")
            print("-" * 70)
            print(result)
            print("-" * 70)
        else:
            print(f"\n❌ 任务执行失败：未获得结果")

    except Exception as e:
        print(f"\n❌ 任务执行异常: {e}")
        import traceback
        traceback.print_exc()

    print("\n" + "="*70)
    print("测试完成")
    print("="*70)


if __name__ == "__main__":
    asyncio.run(test_end_to_end())
