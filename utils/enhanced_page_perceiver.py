"""
增强的页面感知器 - 为LLM提供充足的页面信息

问题：当前只提供元素列表，LLM看不到页面内容
解决：提取页面的语义结构 + 可交互元素 + 文本内容
"""

from typing import List, Dict, Any, Optional
from dataclasses import dataclass
from utils.accessibility_tree_extractor import AccessibilityTreeExtractor, AccessibleElement


@dataclass
class PageContent:
    """页面内容的结构化表示"""
    url: str
    title: str
    main_headings: List[str]  # 主要标题
    text_blocks: List[str]  # 文本块
    interactive_elements: List[AccessibleElement]  # 可交互元素
    page_summary: str  # 页面摘要


class EnhancedPagePerceiver:
    """
    增强的页面感知器

    为LLM提供充足的页面信息：
    1. 页面结构（标题、段落）
    2. 文本内容（主要内容）
    3. 可交互元素（按钮、链接）
    """

    def __init__(self):
        self.a11y_extractor = AccessibilityTreeExtractor()

    async def perceive_page(self, page) -> PageContent:
        """
        全面感知页面

        Returns:
            包含结构、内容、元素的完整页面表示
        """
        # 1. 获取基础信息
        url = page.url
        title = await page.title()

        # 2. 提取可交互元素（accessibility tree）
        interactive_elements = await self.a11y_extractor.extract_tree(page)

        # 3. 提取页面结构和内容
        page_structure = await page.evaluate("""
            () => {
                // 提取标题
                const headings = [];
                document.querySelectorAll('h1, h2, h3').forEach(h => {
                    const text = h.textContent.trim();
                    if (text && text.length < 200) {
                        headings.push({
                            level: h.tagName.toLowerCase(),
                            text: text
                        });
                    }
                });

                // 提取主要文本块
                const textBlocks = [];

                // 优先提取main区域的内容
                const mainContent = document.querySelector('main, [role="main"], #main, .main-content');
                if (mainContent) {
                    const paragraphs = mainContent.querySelectorAll('p, li, td, div.content');
                    paragraphs.forEach(p => {
                        const text = p.textContent.trim();
                        if (text && text.length > 20 && text.length < 500) {
                            textBlocks.push(text);
                        }
                    });
                }

                // 如果main区域没有内容，提取body的内容
                if (textBlocks.length === 0) {
                    const paragraphs = document.querySelectorAll('p, li');
                    paragraphs.forEach(p => {
                        const text = p.textContent.trim();
                        if (text && text.length > 20 && text.length < 500) {
                            textBlocks.push(text);
                        }
                    });
                }

                // 提取表格内容（如果有）
                const tables = document.querySelectorAll('table');
                const tableData = [];
                tables.forEach((table, idx) => {
                    if (idx < 2) {  // 只提取前2个表格
                        const rows = table.querySelectorAll('tr');
                        const tableRows = [];
                        rows.forEach((row, rowIdx) => {
                            if (rowIdx < 5) {  // 每个表格只提取前5行
                                const cells = row.querySelectorAll('th, td');
                                const cellTexts = Array.from(cells).map(c => c.textContent.trim());
                                if (cellTexts.some(t => t)) {
                                    tableRows.push(cellTexts.join(' | '));
                                }
                            }
                        });
                        if (tableRows.length > 0) {
                            tableData.push('表格 ' + (idx + 1) + ':\\n' + tableRows.join('\\n'));
                        }
                    }
                });

                return {
                    headings: headings,
                    textBlocks: textBlocks.slice(0, 10),  // 最多10个文本块
                    tables: tableData
                };
            }
        """)

        # 4. 生成页面摘要
        main_headings = [h['text'] for h in page_structure['headings'][:5]]
        text_blocks = page_structure['textBlocks'][:5]

        # 组合表格数据
        if page_structure['tables']:
            text_blocks.extend(page_structure['tables'])

        # 生成摘要
        summary_parts = []
        if main_headings:
            summary_parts.append(f"主要标题: {', '.join(main_headings)}")
        if text_blocks:
            summary_parts.append(f"页面包含 {len(text_blocks)} 个内容块")

        page_summary = "; ".join(summary_parts) if summary_parts else "页面内容较少"

        return PageContent(
            url=url,
            title=title,
            main_headings=main_headings,
            text_blocks=text_blocks,
            interactive_elements=interactive_elements,
            page_summary=page_summary
        )

    def to_llm_context(self, page_content: PageContent, max_text_blocks: int = 5) -> str:
        """
        转换为LLM友好的完整上下文

        包含：
        1. 页面基本信息
        2. 页面结构（标题）
        3. 页面内容（文本块）
        4. 可交互元素
        """
        lines = []

        # 1. 基本信息
        lines.append(f"# 页面信息")
        lines.append(f"URL: {page_content.url}")
        lines.append(f"标题: {page_content.title}")
        lines.append(f"摘要: {page_content.page_summary}")
        lines.append("")

        # 2. 页面结构
        if page_content.main_headings:
            lines.append("## 页面结构（主要标题）")
            for i, heading in enumerate(page_content.main_headings[:5], 1):
                lines.append(f"{i}. {heading}")
            lines.append("")

        # 3. 页面内容
        if page_content.text_blocks:
            lines.append("## 页面内容（文本摘要）")
            for i, block in enumerate(page_content.text_blocks[:max_text_blocks], 1):
                # 截断过长的文本
                text = block[:200] + "..." if len(block) > 200 else block
                lines.append(f"{i}. {text}")
            if len(page_content.text_blocks) > max_text_blocks:
                lines.append(f"... 还有 {len(page_content.text_blocks) - max_text_blocks} 个内容块")
            lines.append("")

        # 4. 可交互元素
        lines.append("## 可交互元素")
        elements_context = self.a11y_extractor.to_llm_context(
            page_content.interactive_elements,
            max_elements=50
        )
        lines.append(elements_context)

        return "\n".join(lines)


# 便捷函数
async def get_rich_page_context(page) -> str:
    """
    获取丰富的页面上下文（一站式函数）

    Args:
        page: Playwright page对象

    Returns:
        包含页面结构、内容、元素的完整上下文
    """
    perceiver = EnhancedPagePerceiver()
    page_content = await perceiver.perceive_page(page)
    return perceiver.to_llm_context(page_content)
