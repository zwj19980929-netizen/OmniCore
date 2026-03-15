"""
测试 P0 修复：直接测试 WebWorker 层的改进
绕过 Router，直接调用 WebWorker 的搜索和感知功能
"""
import asyncio
import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from agents.web_worker import WebWorker
from utils.page_perceiver import PagePerceiver
from utils.browser_toolkit import BrowserToolkit


async def test_search_engine_fallback():
    """测试 P0-1: 搜索引擎三层降级"""
    print("\n" + "="*60)
    print("测试 P0-1: 搜索引擎三层降级")
    print("="*60)

    worker = WebWorker()

    # 测试搜索（应该会降级到直接 URL）
    query = "numpy github"
    print(f"\n查询: {query}")
    print("预期: API 搜索失败 → 原生搜索尝试 → 直接 URL 成功")

    try:
        cards = await worker.search_for_result_cards(query, max_results=3)

        if cards:
            print(f"\n✅ 搜索成功！找到 {len(cards)} 个结果")
            for i, card in enumerate(cards, 1):
                print(f"\n结果 {i}:")
                print(f"  标题: {card.get('title', 'N/A')[:60]}")
                print(f"  URL: {card.get('url', 'N/A')[:80]}")
        else:
            print("\n❌ 搜索失败：未找到结果")

    except Exception as e:
        print(f"\n❌ 搜索异常: {e}")
        import traceback
        traceback.print_exc()


async def test_page_perceiver_recovery():
    """测试 P0-2: 页面感知错误恢复"""
    print("\n" + "="*60)
    print("测试 P0-2: 页面感知错误恢复")
    print("="*60)

    print("\n测试场景: 访问 GitHub 首页并提取页面结构")
    print("预期: JS 提取成功或降级到 BeautifulSoup")

    try:
        async with BrowserToolkit(headless=True) as tk:
            # 访问 GitHub
            url = "https://github.com"
            print(f"\n访问: {url}")

            nav_result = await tk.goto(url)
            if not nav_result.success:
                print(f"❌ 导航失败: {nav_result.error}")
                return

            print("✅ 页面加载成功")

            # 测试页面感知
            perceiver = PagePerceiver()
            structure = await perceiver.perceive_page(tk)

            print(f"\n页面感知结果:")
            print(f"  URL: {structure.url}")
            print(f"  标题: {structure.title}")
            print(f"  主要内容块: {len(structure.main_content_blocks)}")
            print(f"  导航块: {len(structure.navigation_blocks)}")
            print(f"  交互元素: {len(structure.interactive_elements)}")
            print(f"  元数据: {structure.metadata}")

            # 检查是否使用了降级方案
            if structure.metadata.get('fallback'):
                print(f"\n⚠️  使用了降级方案: {structure.metadata['fallback']}")
            else:
                print(f"\n✅ JS 提取成功")

            # 显示部分内容
            if structure.main_content_blocks:
                print(f"\n前 3 个内容块:")
                for i, block in enumerate(structure.main_content_blocks[:3], 1):
                    print(f"  {i}. [{block.block_type}] {block.content[:60]}...")
                    print(f"     选择器: {block.selector}")

            if structure.interactive_elements:
                print(f"\n前 3 个交互元素:")
                for i, elem in enumerate(structure.interactive_elements[:3], 1):
                    print(f"  {i}. [{elem['type']}] {elem['text'][:40]}")
                    print(f"     选择器: {elem['selector']}")

            print("\n✅ 页面感知测试完成")

    except Exception as e:
        print(f"\n❌ 测试异常: {e}")
        import traceback
        traceback.print_exc()


async def test_selector_strategy():
    """测试 P0-3: 选择器生成策略"""
    print("\n" + "="*60)
    print("测试 P0-3: 选择器生成策略")
    print("="*60)

    print("\n测试场景: 访问 GitHub 并检查生成的选择器质量")
    print("预期: 使用多层策略生成稳定的选择器")

    try:
        async with BrowserToolkit(headless=True) as tk:
            url = "https://github.com"
            print(f"\n访问: {url}")

            nav_result = await tk.goto(url)
            if not nav_result.success:
                print(f"❌ 导航失败: {nav_result.error}")
                return

            print("✅ 页面加载成功")

            # 提取页面结构
            perceiver = PagePerceiver()
            structure = await perceiver.perceive_page(tk)

            # 分析选择器质量
            print(f"\n选择器质量分析:")

            selectors = []
            for block in structure.main_content_blocks[:10]:
                selectors.append(block.selector)
            for elem in structure.interactive_elements[:10]:
                selectors.append(elem['selector'])

            # 统计选择器类型
            data_attr_count = sum(1 for s in selectors if s.startswith('[data-'))
            id_count = sum(1 for s in selectors if s.startswith('#'))
            class_count = sum(1 for s in selectors if '.' in s and not s.startswith('#'))
            text_count = sum(1 for s in selectors if ':has-text(' in s)
            attr_count = sum(1 for s in selectors if s.startswith('[') and not s.startswith('[data-'))
            path_count = sum(1 for s in selectors if '>' in s or ':nth-child(' in s)

            total = len(selectors)
            print(f"\n  总选择器数: {total}")
            print(f"  data-* 属性: {data_attr_count} ({data_attr_count/total*100:.1f}%)")
            print(f"  ID 选择器: {id_count} ({id_count/total*100:.1f}%)")
            print(f"  Class 选择器: {class_count} ({class_count/total*100:.1f}%)")
            print(f"  文本选择器: {text_count} ({text_count/total*100:.1f}%)")
            print(f"  属性选择器: {attr_count} ({attr_count/total*100:.1f}%)")
            print(f"  路径选择器: {path_count} ({path_count/total*100:.1f}%)")

            # 显示示例选择器
            print(f"\n示例选择器:")
            for i, selector in enumerate(selectors[:5], 1):
                print(f"  {i}. {selector}")

            # 评估选择器质量
            quality_score = (
                data_attr_count * 5 +  # data-* 最稳定
                id_count * 4 +          # ID 次之
                class_count * 3 +       # class 一般
                text_count * 3 +        # 文本选择器一般
                attr_count * 2 +        # 属性选择器较差
                path_count * 1          # 路径选择器最差
            ) / total if total > 0 else 0

            print(f"\n选择器质量评分: {quality_score:.2f}/5.0")
            if quality_score >= 4.0:
                print("✅ 优秀 - 大部分使用稳定的选择器策略")
            elif quality_score >= 3.0:
                print("⚠️  良好 - 选择器质量可接受")
            else:
                print("❌ 较差 - 过多使用不稳定的选择器")

            print("\n✅ 选择器策略测试完成")

    except Exception as e:
        print(f"\n❌ 测试异常: {e}")
        import traceback
        traceback.print_exc()


async def main():
    """运行所有 P0 测试"""
    print("\n" + "="*60)
    print("P0 修复验证测试")
    print("="*60)
    print("\n本测试直接调用 WebWorker 层，绕过 Router")
    print("验证三个 P0 修复是否正常工作\n")

    # 测试 1: 搜索引擎降级
    await test_search_engine_fallback()

    # 等待一下
    await asyncio.sleep(2)

    # 测试 2: 页面感知恢复
    await test_page_perceiver_recovery()

    # 等待一下
    await asyncio.sleep(2)

    # 测试 3: 选择器策略
    await test_selector_strategy()

    print("\n" + "="*60)
    print("所有 P0 测试完成")
    print("="*60)


if __name__ == "__main__":
    asyncio.run(main())
