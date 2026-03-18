"""
增强的页面感知器 - 为LLM提供充足的页面信息

统一使用 perception_scripts 的 semantic_snapshot 作为感知数据来源，
不再独立执行 JS 提取，避免两套不一致的提取系统。
"""

from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
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
    # 来自 semantic_snapshot 的完整数据
    snapshot: Dict[str, Any] = field(default_factory=dict)


class EnhancedPagePerceiver:
    """
    增强的页面感知器

    统一从 semantic_snapshot 获取所有感知数据：
    1. 页面结构（标题、段落、区域）
    2. 文本内容（主要内容、文本块）
    3. 可交互元素（按钮、链接）
    4. 搜索结果卡片、集合
    5. 页面控件和功能可见性
    """

    def __init__(self):
        self.a11y_extractor = AccessibilityTreeExtractor()

    async def perceive_page(
        self,
        page,
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> PageContent:
        """
        全面感知页面

        Args:
            page: Playwright page 对象
            snapshot: 预先获取的 semantic_snapshot 结果。
                      如果为 None，回退到 a11y 提取 + 基础信息。

        Returns:
            包含结构、内容、元素的完整页面表示
        """
        url = page.url
        title = await page.title()

        # 提取可交互元素（accessibility tree，作为 snapshot 元素的补充）
        interactive_elements = await self.a11y_extractor.extract_tree(page)

        if snapshot:
            # 从 snapshot 获取所有感知数据（统一来源）
            headings_raw = snapshot.get("headings") or []
            main_headings = [
                h.get("text", "") for h in headings_raw
                if h.get("text")
            ]

            # 文本块：优先用 visible_text_blocks，回退到 main_text 分段
            text_blocks = []
            vtb = snapshot.get("visible_text_blocks") or []
            for block in vtb:
                text = block.get("text", "") if isinstance(block, dict) else str(block)
                if text and len(text.strip()) > 10:
                    text_blocks.append(text.strip())

            if not text_blocks:
                main_text = snapshot.get("main_text") or ""
                if main_text:
                    # 按句子/段落粗切
                    for chunk in main_text.split("\n"):
                        chunk = chunk.strip()
                        if len(chunk) > 20:
                            text_blocks.append(chunk[:500])
                        if len(text_blocks) >= 20:
                            break

            # 卡片信息也加入文本块，确保 LLM 能看到
            cards = snapshot.get("cards") or []
            for card in cards[:8]:
                card_text = card.get("title", "")
                snippet = card.get("snippet", "")
                if snippet and snippet != card_text:
                    card_text += f" — {snippet}"
                if card_text:
                    text_blocks.append(card_text[:500])

            # 集合信息
            collections = snapshot.get("collections") or []
            for col in collections[:4]:
                samples = col.get("sample_items") or []
                if samples:
                    col_text = f"[{col.get('kind', 'collection')}] " + " | ".join(
                        str(s)[:100] for s in samples[:5]
                    )
                    text_blocks.append(col_text)
        else:
            # 回退：没有 snapshot 时用最基础的提取
            main_headings = []
            text_blocks = []

        # 生成摘要
        summary_parts = []
        if main_headings:
            summary_parts.append(f"主要标题: {', '.join(main_headings[:5])}")
        if text_blocks:
            summary_parts.append(f"页面包含 {len(text_blocks)} 个内容块")
        page_summary = "; ".join(summary_parts) if summary_parts else "页面内容较少"

        return PageContent(
            url=url,
            title=title,
            main_headings=main_headings,
            text_blocks=text_blocks,
            interactive_elements=interactive_elements,
            page_summary=page_summary,
            snapshot=snapshot or {},
        )

    def to_llm_context(self, page_content: PageContent, max_text_blocks: int = 10) -> str:
        """
        转换为LLM友好的完整上下文
        """
        lines = []

        # 1. 基本信息
        lines.append("# 页面信息")
        lines.append(f"URL: {page_content.url}")
        lines.append(f"标题: {page_content.title}")
        lines.append(f"摘要: {page_content.page_summary}")
        lines.append("")

        # 2. 页面结构
        if page_content.main_headings:
            lines.append("## 页面结构（主要标题）")
            for i, heading in enumerate(page_content.main_headings[:8], 1):
                lines.append(f"{i}. {heading}")
            lines.append("")

        # 3. 页面内容
        if page_content.text_blocks:
            lines.append("## 页面内容（文本摘要）")
            for i, block in enumerate(page_content.text_blocks[:max_text_blocks], 1):
                text = block[:300] + "..." if len(block) > 300 else block
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
async def get_rich_page_context(page, snapshot: Optional[Dict[str, Any]] = None) -> str:
    """
    获取丰富的页面上下文（一站式函数）

    Args:
        page: Playwright page对象
        snapshot: 可选的预获取 semantic_snapshot

    Returns:
        包含页面结构、内容、元素的完整上下文
    """
    perceiver = EnhancedPagePerceiver()
    page_content = await perceiver.perceive_page(page, snapshot=snapshot)
    return perceiver.to_llm_context(page_content)
