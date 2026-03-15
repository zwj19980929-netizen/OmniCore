"""
测试 Router JSON 解析修复
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core.router import RouterAgent
from core.state import create_initial_state


async def test_router_json_parsing():
    """测试 Router 是否能正确返回 JSON"""
    print("\n" + "="*60)
    print("测试 Router JSON 解析修复")
    print("="*60)

    router = RouterAgent()

    # 测试用例
    test_cases = [
        "帮我找到 numpy 的 GitHub 地址",
        "搜索 pytorch 官网",
        "今天天气怎么样",
    ]

    for i, user_input in enumerate(test_cases, 1):
        print(f"\n测试 {i}: {user_input}")
        print("-" * 60)

        try:
            # 创建初始状态
            state = create_initial_state(user_input)

            # 调用 Router
            result_state = router.route(state)

            # 检查结果
            intent = result_state.get("current_intent", "unknown")
            confidence = result_state.get("intent_confidence", 0.0)
            task_queue = result_state.get("task_queue", [])

            print(f"✅ 解析成功")
            print(f"  意图: {intent}")
            print(f"  置信度: {confidence:.2f}")
            print(f"  任务数: {len(task_queue)}")

            if task_queue:
                print(f"\n  任务列表:")
                for j, task in enumerate(task_queue, 1):
                    print(f"    {j}. [{task['task_type']}] {task['description'][:60]}")

        except Exception as e:
            print(f"❌ 解析失败: {e}")
            import traceback
            traceback.print_exc()

    print("\n" + "="*60)
    print("测试完成")
    print("="*60)


if __name__ == "__main__":
    asyncio.run(test_router_json_parsing())
