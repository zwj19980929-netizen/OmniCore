"""
页面感知器 - 为 Agent 提供结构化的网页理解能力
将原始 HTML 转换为 LLM 可理解的语义化描述
"""
import re
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field


@dataclass
class PageBlock:
    """页面内容块"""
    block_type: str  # heading, paragraph, list, table, form, nav, article
    content: str
    selector: str
    depth: int
    children: List['PageBlock'] = field(default_factory=list)
    attributes: Dict[str, str] = field(default_factory=dict)


@dataclass
class PageStructure:
    """页面结构化表示"""
    url: str
    title: str
    main_content_blocks: List[PageBlock]
    navigation_blocks: List[PageBlock]
    interactive_elements: List[Dict[str, Any]]
    metadata: Dict[str, Any]

    def to_llm_prompt(self) -> str:
        """转换为 LLM 友好的文本描述"""
        lines = [
            f"# 页面：{self.title}",
            f"URL: {self.url}",
            "",
            "## 页面结构概览",
        ]

        # 主要内容区域
        if self.main_content_blocks:
            lines.append("\n### 主要内容区域")
            for i, block in enumerate(self.main_content_blocks[:10], 1):
                lines.append(f"{i}. [{block.block_type}] {block.content[:200]}")
                lines.append(f"   选择器: {block.selector}")

        # 导航区域
        if self.navigation_blocks:
            lines.append("\n### 导航/菜单区域")
            for block in self.navigation_blocks[:5]:
                lines.append(f"- {block.content[:100]}")

        # 可交互元素
        if self.interactive_elements:
            lines.append("\n### 可交互元素")
            for elem in self.interactive_elements[:15]:
                elem_type = elem.get('type', 'unknown')
                text = elem.get('text', '')[:80]
                selector = elem.get('selector', '')
                lines.append(f"- [{elem_type}] {text} → {selector}")

        return "\n".join(lines)


class PagePerceiver:
    """页面感知器 - 将 HTML 转换为结构化的页面理解"""

    def __init__(self):
        self.noise_selectors = [
            'script', 'style', 'noscript', 'iframe[src*="ad"]',
            '.advertisement', '.ad-banner', '#cookie-notice',
            '.social-share', '.related-posts', '.comments'
        ]

    async def perceive_page(self, toolkit, task_description: str = "") -> PageStructure:
        """
        感知页面结构

        Args:
            toolkit: BrowserToolkit 实例
            task_description: 任务描述，用于过滤相关内容

        Returns:
            PageStructure: 结构化的页面表示
        """
        # 获取基础信息
        url_r = await toolkit.get_current_url()
        title_r = await toolkit.get_title()

        # 执行 JS 提取结构化信息
        structure_r = await toolkit.evaluate_js(self._get_structure_extraction_script())

        if not structure_r.success:
            return PageStructure(
                url=url_r.data or "",
                title=title_r.data or "",
                main_content_blocks=[],
                navigation_blocks=[],
                interactive_elements=[],
                metadata={}
            )

        raw_data = structure_r.data or {}

        # 构建结构化表示
        return PageStructure(
            url=url_r.data or "",
            title=title_r.data or "",
            main_content_blocks=self._parse_blocks(raw_data.get('main_content', [])),
            navigation_blocks=self._parse_blocks(raw_data.get('navigation', [])),
            interactive_elements=raw_data.get('interactive', []),
            metadata=raw_data.get('metadata', {})
        )

    def _parse_blocks(self, raw_blocks: List[Dict]) -> List[PageBlock]:
        """解析原始块数据为 PageBlock 对象"""
        blocks = []
        for raw in raw_blocks:
            block = PageBlock(
                block_type=raw.get('type', 'unknown'),
                content=raw.get('content', ''),
                selector=raw.get('selector', ''),
                depth=raw.get('depth', 0),
                attributes=raw.get('attributes', {})
            )
            blocks.append(block)
        return blocks

    def _get_structure_extraction_script(self) -> str:
        """返回在浏览器中执行的结构提取脚本"""
        return """
        () => {
            // 工具函数
            function cleanText(text) {
                return (text || '').replace(/\\s+/g, ' ').trim();
            }

            function isVisible(el) {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                return style.display !== 'none' &&
                       style.visibility !== 'hidden' &&
                       style.opacity !== '0' &&
                       el.offsetWidth > 0 &&
                       el.offsetHeight > 0;
            }

            function getSelector(el) {
                if (el.id) return `#${CSS.escape(el.id)}`;

                const classes = Array.from(el.classList)
                    .filter(c => !c.match(/^(active|selected|hover|focus)/))
                    .slice(0, 2);

                if (classes.length > 0) {
                    return `${el.tagName.toLowerCase()}.${classes.join('.')}`;
                }

                // 生成路径选择器
                const path = [];
                let current = el;
                while (current && current.nodeType === 1 && path.length < 4) {
                    let part = current.tagName.toLowerCase();
                    const parent = current.parentElement;
                    if (parent) {
                        const siblings = Array.from(parent.children)
                            .filter(s => s.tagName === current.tagName);
                        if (siblings.length > 1) {
                            part += `:nth-of-type(${siblings.indexOf(current) + 1})`;
                        }
                    }
                    path.unshift(part);
                    current = parent;
                }
                return path.join(' > ');
            }

            function getDepth(el) {
                let depth = 0;
                let current = el;
                while (current && current !== document.body) {
                    depth++;
                    current = current.parentElement;
                }
                return depth;
            }

            // 提取主要内容区域
            function extractMainContent() {
                const candidates = [
                    ...document.querySelectorAll('main, article, [role="main"], .content, .main-content, #content, #main'),
                    ...document.querySelectorAll('div[class*="content"], div[id*="content"]')
                ];

                const blocks = [];
                const seen = new Set();

                for (const container of candidates) {
                    if (!isVisible(container) || seen.has(container)) continue;
                    seen.add(container);

                    // 提取标题
                    const headings = container.querySelectorAll('h1, h2, h3, h4');
                    for (const h of headings) {
                        if (!isVisible(h)) continue;
                        const text = cleanText(h.textContent);
                        if (text.length < 3 || text.length > 200) continue;

                        blocks.push({
                            type: 'heading',
                            content: text,
                            selector: getSelector(h),
                            depth: getDepth(h),
                            attributes: {
                                level: h.tagName.toLowerCase()
                            }
                        });
                    }

                    // 提取列表
                    const lists = container.querySelectorAll('ul, ol');
                    for (const list of lists) {
                        if (!isVisible(list)) continue;
                        const items = Array.from(list.querySelectorAll('li'))
                            .filter(isVisible)
                            .map(li => cleanText(li.textContent))
                            .filter(t => t.length > 0 && t.length < 300);

                        if (items.length >= 2) {
                            blocks.push({
                                type: 'list',
                                content: items.slice(0, 10).join(' | '),
                                selector: getSelector(list),
                                depth: getDepth(list),
                                attributes: {
                                    itemCount: items.length,
                                    itemSelector: getSelector(list.querySelector('li'))
                                }
                            });
                        }
                    }

                    // 提取表格
                    const tables = container.querySelectorAll('table');
                    for (const table of tables) {
                        if (!isVisible(table)) continue;
                        const rows = table.querySelectorAll('tr');
                        if (rows.length < 2) continue;

                        const headers = Array.from(table.querySelectorAll('th'))
                            .map(th => cleanText(th.textContent))
                            .filter(Boolean);

                        blocks.push({
                            type: 'table',
                            content: `表格 (${rows.length} 行): ${headers.join(', ')}`,
                            selector: getSelector(table),
                            depth: getDepth(table),
                            attributes: {
                                rowCount: rows.length,
                                headers: headers
                            }
                        });
                    }

                    // 提取段落
                    const paragraphs = container.querySelectorAll('p');
                    for (const p of paragraphs) {
                        if (!isVisible(p)) continue;
                        const text = cleanText(p.textContent);
                        if (text.length < 20 || text.length > 500) continue;

                        blocks.push({
                            type: 'paragraph',
                            content: text.slice(0, 200),
                            selector: getSelector(p),
                            depth: getDepth(p),
                            attributes: {}
                        });
                    }
                }

                return blocks.slice(0, 30);
            }

            // 提取导航区域
            function extractNavigation() {
                const navs = document.querySelectorAll('nav, [role="navigation"], .nav, .menu, .navbar');
                const blocks = [];

                for (const nav of navs) {
                    if (!isVisible(nav)) continue;

                    const links = Array.from(nav.querySelectorAll('a'))
                        .filter(isVisible)
                        .map(a => cleanText(a.textContent))
                        .filter(Boolean);

                    if (links.length > 0) {
                        blocks.push({
                            type: 'navigation',
                            content: links.slice(0, 10).join(' | '),
                            selector: getSelector(nav),
                            depth: getDepth(nav),
                            attributes: {
                                linkCount: links.length
                            }
                        });
                    }
                }

                return blocks;
            }

            // 提取可交互元素
            function extractInteractive() {
                const elements = [];
                const selectors = 'a, button, input, textarea, select, [role="button"], [contenteditable="true"]';
                const nodes = document.querySelectorAll(selectors);

                for (const el of nodes) {
                    if (!isVisible(el)) continue;

                    const text = cleanText(el.textContent || el.value || el.placeholder || '');
                    const tag = el.tagName.toLowerCase();
                    const type = el.getAttribute('type') || tag;

                    elements.push({
                        type: type,
                        text: text.slice(0, 100),
                        selector: getSelector(el),
                        attributes: {
                            href: el.getAttribute('href') || '',
                            name: el.getAttribute('name') || '',
                            id: el.getAttribute('id') || '',
                            placeholder: el.getAttribute('placeholder') || ''
                        }
                    });

                    if (elements.length >= 50) break;
                }

                return elements;
            }

            // 提取元数据
            function extractMetadata() {
                return {
                    hasSearchBox: !!document.querySelector('input[type="search"], input[name*="search"], input[placeholder*="搜索"], input[placeholder*="search"]'),
                    hasLoginForm: !!document.querySelector('input[type="password"]'),
                    hasPagination: !!document.querySelector('.pagination, .pager, [class*="page-"], a[href*="page="]'),
                    hasDataTable: !!document.querySelector('table[class*="data"], table[class*="list"]'),
                    hasArticleList: !!document.querySelector('article, .article, .post, [class*="item-"]'),
                    language: document.documentElement.lang || 'unknown'
                };
            }

            // 执行提取
            return {
                main_content: extractMainContent(),
                navigation: extractNavigation(),
                interactive: extractInteractive(),
                metadata: extractMetadata()
            };
        }
        """


async def get_page_understanding(toolkit, task_description: str = "") -> str:
    """
    获取页面的语义化理解描述

    这是给 LLM 看的"页面说明书"，而不是原始 HTML
    """
    perceiver = PagePerceiver()
    structure = await perceiver.perceive_page(toolkit, task_description)
    return structure.to_llm_prompt()
