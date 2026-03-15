"""
页面感知器 - 为 Agent 提供结构化的网页理解能力
将原始 HTML 转换为 LLM 可理解的语义化描述
"""
import re
from html.parser import HTMLParser
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
            if len(self.main_content_blocks) > 10:
                lines.append(f"... 还有 {len(self.main_content_blocks) - 10} 个内容块未展示")

        # 导航区域
        if self.navigation_blocks:
            lines.append("\n### 导航/菜单区域")
            for block in self.navigation_blocks[:5]:
                lines.append(f"- {block.content[:100]}")
            if len(self.navigation_blocks) > 5:
                lines.append(f"- ... 还有 {len(self.navigation_blocks) - 5} 个导航块未展示")

        # 可交互元素
        if self.interactive_elements:
            lines.append("\n### 可交互元素")
            for elem in self.interactive_elements[:15]:
                elem_type = elem.get('type', 'unknown')
                text = elem.get('text', '')[:80]
                selector = elem.get('selector', '')
                lines.append(f"- [{elem_type}] {text} → {selector}")
            if len(self.interactive_elements) > 15:
                lines.append(f"- ... 还有 {len(self.interactive_elements) - 15} 个交互元素未展示")

        return "\n".join(lines)


def _css_escape_token(value: str) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    return re.sub(r'([ !"#$%&\'()*+,./:;<=>?@[\\\]^`{|}~])', r'\\\1', token)


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
        感知页面结构（带错误恢复机制）

        Args:
            toolkit: BrowserToolkit 实例
            task_description: 任务描述，用于过滤相关内容

        Returns:
            PageStructure: 结构化的页面表示
        """
        # 获取基础信息
        url_r = await toolkit.get_current_url()
        title_r = await toolkit.get_title()
        url = url_r.data or ""
        title = title_r.data or ""

        # 策略 1: 尝试 JS 提取（最优）
        structure_r = await self._try_js_extraction(toolkit)

        if structure_r.success:
            raw_data = structure_r.data or {}
            return PageStructure(
                url=url,
                title=title,
                main_content_blocks=self._parse_blocks(raw_data.get('main_content', [])),
                navigation_blocks=self._parse_blocks(raw_data.get('navigation', [])),
                interactive_elements=raw_data.get('interactive', []),
                metadata=raw_data.get('metadata', {})
            )

        # 策略 2: 降级到 DOM 解析（备用）
        from utils.logger import log_warning
        log_warning(f"JS 提取失败，降级到 DOM 解析: {structure_r.error}")

        dom_structure = await self._fallback_dom_parsing(toolkit, url, title)
        return dom_structure

    async def _try_js_extraction(self, toolkit, max_retries: int = 3) -> Any:
        """
        尝试 JS 提取，带重试机制

        Args:
            toolkit: BrowserToolkit 实例
            max_retries: 最大重试次数

        Returns:
            ToolkitResult: JS 执行结果
        """
        import asyncio
        from utils.logger import log_warning

        for attempt in range(max_retries):
            # 等待页面稳定（动态内容加载）
            if attempt > 0:
                wait_time = min(1000 * (2 ** attempt), 5000)  # 指数退避: 2s, 4s, 5s
                log_warning(f"JS 提取重试 {attempt + 1}/{max_retries}，等待 {wait_time}ms")
                await asyncio.sleep(wait_time / 1000)

            # 执行 JS 脚本
            result = await toolkit.evaluate_js(self._get_structure_extraction_script())

            if result.success and result.data:
                return result

        # 所有重试都失败
        return result

    async def _fallback_dom_parsing(self, toolkit, url: str, title: str) -> PageStructure:
        """
        降级到 DOM 解析（优先 BeautifulSoup，不可用时退回标准库解析器）

        Args:
            toolkit: BrowserToolkit 实例
            url: 页面 URL
            title: 页面标题

        Returns:
            PageStructure: 从 HTML 解析的页面结构
        """
        from utils.logger import logger, log_warning

        html_result = await toolkit.get_page_html()
        if not html_result.success or not html_result.data:
            return PageStructure(
                url=url,
                title=title,
                main_content_blocks=[],
                navigation_blocks=[],
                interactive_elements=[],
                metadata={"fallback": "dom_parsing_failed"}
            )

        try:
            from bs4 import BeautifulSoup
        except ModuleNotFoundError:
            log_warning("BeautifulSoup 不可用，使用标准库 html.parser 进行 DOM 解析")
            return self._fallback_stdlib_parsing(str(html_result.data or ""), url, title)

        logger.info("使用 BeautifulSoup 进行 DOM 解析")
        soup = BeautifulSoup(html_result.data, 'html.parser')

        # 移除噪音元素
        for selector in self.noise_selectors:
            for elem in soup.select(selector):
                elem.decompose()

        # 提取主要内容
        main_blocks = self._extract_main_content_bs4(soup)

        # 提取导航
        nav_blocks = self._extract_navigation_bs4(soup)

        # 提取交互元素
        interactive = self._extract_interactive_bs4(soup)

        # 提取元数据
        metadata = self._extract_metadata_bs4(soup)
        metadata["fallback"] = "beautifulsoup"
        page_title = title or ((soup.title.get_text(strip=True) if soup.title else "") or "")

        return PageStructure(
            url=url,
            title=page_title,
            main_content_blocks=main_blocks,
            navigation_blocks=nav_blocks,
            interactive_elements=interactive,
            metadata=metadata
        )

    def _fallback_stdlib_parsing(self, html: str, url: str, title: str) -> PageStructure:
        parser = _StdlibStructureParser()
        parser.feed(html or "")
        parser.close()

        return PageStructure(
            url=url,
            title=title or parser.page_title,
            main_content_blocks=parser.main_blocks[:30],
            navigation_blocks=parser.navigation_blocks[:10],
            interactive_elements=parser.interactive_elements[:50],
            metadata={
                "fallback": "html_parser",
                "content_block_count": len(parser.main_blocks),
                "interactive_count": len(parser.interactive_elements),
            },
        )

    def _extract_main_content_bs4(self, soup: Any) -> List[PageBlock]:
        """使用 BeautifulSoup 提取主要内容"""
        blocks = []

        # 查找主要内容容器
        main_containers = soup.select('main, article, [role="main"], .content, .main-content, #content, #main')
        if not main_containers:
            main_containers = [soup.body] if soup.body else [soup]

        for container in main_containers[:3]:  # 限制容器数量
            # 提取标题
            for heading in container.find_all(['h1', 'h2', 'h3', 'h4'], limit=20):
                text = heading.get_text(strip=True)
                if 3 <= len(text) <= 200:
                    blocks.append(PageBlock(
                        block_type='heading',
                        content=text,
                        selector=self._get_bs4_selector(heading),
                        depth=self._get_bs4_depth(heading),
                        attributes={'level': heading.name}
                    ))

            # 提取列表
            for list_elem in container.find_all(['ul', 'ol'], limit=15):
                items = [li.get_text(strip=True) for li in list_elem.find_all('li', limit=10)]
                items = [item for item in items if 0 < len(item) < 300]
                if len(items) >= 2:
                    blocks.append(PageBlock(
                        block_type='list',
                        content=' | '.join(items[:10]),
                        selector=self._get_bs4_selector(list_elem),
                        depth=self._get_bs4_depth(list_elem),
                        attributes={'itemCount': len(items)}
                    ))

            # 提取段落
            for para in container.find_all('p', limit=20):
                text = para.get_text(strip=True)
                if 20 <= len(text) <= 500:
                    blocks.append(PageBlock(
                        block_type='paragraph',
                        content=text[:200],
                        selector=self._get_bs4_selector(para),
                        depth=self._get_bs4_depth(para),
                        attributes={}
                    ))

        return blocks[:30]

    def _extract_navigation_bs4(self, soup: Any) -> List[PageBlock]:
        """使用 BeautifulSoup 提取导航"""
        blocks = []

        nav_elements = soup.select('nav, [role="navigation"], .nav, .menu, .navbar')
        for nav in nav_elements[:5]:
            links = [a.get_text(strip=True) for a in nav.find_all('a', limit=15)]
            links = [link for link in links if link]

            if links:
                blocks.append(PageBlock(
                    block_type='navigation',
                    content=' | '.join(links[:10]),
                    selector=self._get_bs4_selector(nav),
                    depth=self._get_bs4_depth(nav),
                    attributes={'linkCount': len(links)}
                ))

        return blocks

    def _extract_interactive_bs4(self, soup: Any) -> List[Dict[str, Any]]:
        """使用 BeautifulSoup 提取交互元素"""
        elements = []

        selectors = ['a', 'button', 'input', 'textarea', 'select']
        for selector in selectors:
            for elem in soup.find_all(selector, limit=50):
                text = elem.get_text(strip=True) or elem.get('value', '') or elem.get('placeholder', '')
                elem_type = elem.get('type', elem.name)

                elements.append({
                    'type': elem_type,
                    'text': text[:100],
                    'selector': self._get_bs4_selector(elem),
                    'attributes': {
                        'href': elem.get('href', ''),
                        'name': elem.get('name', ''),
                        'id': elem.get('id', ''),
                        'placeholder': elem.get('placeholder', '')
                    }
                })

                if len(elements) >= 50:
                    break

            if len(elements) >= 50:
                break

        return elements

    def _extract_metadata_bs4(self, soup: Any) -> Dict[str, Any]:
        """使用 BeautifulSoup 提取元数据"""
        return {
            'hasSearchBox': bool(soup.select('input[type="search"], input[name*="search"]')),
            'hasLoginForm': bool(soup.select('input[type="password"]')),
            'hasPagination': bool(soup.select('.pagination, .pager, [class*="page-"]')),
            'hasDataTable': bool(soup.select('table[class*="data"], table[class*="list"]')),
            'hasArticleList': bool(soup.select('article, .article, .post')),
            'language': soup.html.get('lang', 'unknown') if soup.html else 'unknown'
        }

    def _get_bs4_selector(self, elem: Any) -> str:
        """为 BeautifulSoup 元素生成选择器（多策略）"""
        # 策略 1: data-* 属性
        data_attrs = ['data-testid', 'data-id', 'data-element-id', 'data-cy']
        for attr in data_attrs:
            value = elem.get(attr)
            if value:
                return f"[{attr}='{value}']"

        # 策略 2: ID（如果不是动态生成的）
        elem_id = elem.get('id')
        if elem_id and not any(prefix in elem_id for prefix in ['ember', 'react', 'vue', 'ng']):
            return f"#{_css_escape_token(elem_id)}"

        # 策略 3: 唯一的 class 组合
        classes = elem.get('class', [])
        if classes:
            # 过滤动态类名
            stable_classes = [c for c in classes if not any(x in c for x in
                            ['active', 'selected', 'hover', 'focus', 'disabled', 'loading', 'open', 'closed'])]
            if stable_classes:
                escaped = [_css_escape_token(item) for item in stable_classes[:3] if _css_escape_token(item)]
                if escaped:
                    return f"{elem.name}.{'.'.join(escaped)}"

        # 策略 4: 属性组合
        attrs = []
        if elem.get('name'):
            attrs.append(f"[name='{_css_escape_token(elem['name'])}']")
        if elem.get('type'):
            attrs.append(f"[type='{_css_escape_token(elem['type'])}']")
        if elem.get('role'):
            attrs.append(f"[role='{_css_escape_token(elem['role'])}']")
        if attrs:
            return f"{elem.name}{''.join(attrs)}"

        # 策略 5: 标签名（最后手段）
        return elem.name

    def _get_bs4_depth(self, elem: Any) -> int:
        """计算 BeautifulSoup 元素的深度"""
        depth = 0
        current = elem.parent
        while current and current.name:
            depth += 1
            current = current.parent
        return depth

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
                // 策略 1: data-* 属性（最稳定）
                const dataAttrs = ['data-testid', 'data-id', 'data-element-id', 'data-cy'];
                for (const attr of dataAttrs) {
                    const value = el.getAttribute(attr);
                    if (value) {
                        return `[${attr}="${CSS.escape(value)}"]`;
                    }
                }

                // 策略 2: ID（如果唯一且稳定）
                if (el.id && !el.id.match(/^(ember|react|vue|ng)-\\d+/)) {
                    const idSelector = `#${CSS.escape(el.id)}`;
                    // 验证唯一性
                    if (document.querySelectorAll(idSelector).length === 1) {
                        return idSelector;
                    }
                }

                // 策略 3: 唯一的 class 组合
                const classes = Array.from(el.classList)
                    .filter(c => !c.match(/^(active|selected|hover|focus|disabled|loading|open|closed)/))
                    .slice(0, 3);

                if (classes.length > 0) {
                    const classSelector = `${el.tagName.toLowerCase()}.${classes.map(c => CSS.escape(c)).join('.')}`;
                    // 验证唯一性
                    const matches = document.querySelectorAll(classSelector);
                    if (matches.length === 1) {
                        return classSelector;
                    }
                    // 如果不唯一，添加父级上下文
                    if (matches.length > 1 && matches.length <= 10) {
                        const parent = el.parentElement;
                        if (parent && parent.id) {
                            return `#${CSS.escape(parent.id)} > ${classSelector}`;
                        }
                    }
                }

                // 策略 4: 文本内容（对于链接和按钮）
                if (['A', 'BUTTON'].includes(el.tagName)) {
                    const text = cleanText(el.textContent);
                    if (text.length > 0 && text.length <= 50) {
                        const textSelector = `${el.tagName.toLowerCase()}:has-text("${text.slice(0, 30)}")`;
                        // 注意：:has-text 是 Playwright 特有的，标准 CSS 不支持
                        // 这里生成的选择器主要用于 Playwright
                        return textSelector;
                    }
                }

                // 策略 5: 属性组合（name, type, role 等）
                const attrs = [];
                if (el.name) attrs.push(`[name="${CSS.escape(el.name)}"]`);
                if (el.type) attrs.push(`[type="${CSS.escape(el.type)}"]`);
                if (el.getAttribute('role')) attrs.push(`[role="${CSS.escape(el.getAttribute('role'))}"]`);
                if (attrs.length > 0) {
                    const attrSelector = `${el.tagName.toLowerCase()}${attrs.join('')}`;
                    const matches = document.querySelectorAll(attrSelector);
                    if (matches.length === 1) {
                        return attrSelector;
                    }
                }

                // 策略 6: nth-child 路径选择器（最后手段，但更稳定）
                const path = [];
                let current = el;
                let depth = 0;
                while (current && current.nodeType === 1 && depth < 5) {
                    let part = current.tagName.toLowerCase();
                    const parent = current.parentElement;
                    if (parent) {
                        // 使用 nth-child 而不是 nth-of-type，更精确
                        const siblings = Array.from(parent.children);
                        const index = siblings.indexOf(current) + 1;
                        part += `:nth-child(${index})`;
                    }
                    path.unshift(part);
                    current = parent;
                    depth++;
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


class _StdlibStructureParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.page_title = ""
        self.main_blocks: List[PageBlock] = []
        self.navigation_blocks: List[PageBlock] = []
        self.interactive_elements: List[Dict[str, Any]] = []
        self._stack: List[Dict[str, Any]] = []
        self._nav_depth = 0

    def handle_starttag(self, tag: str, attrs: List[tuple[str, Optional[str]]]) -> None:
        attr_map = {key: value or "" for key, value in attrs}
        node = {
            "tag": tag,
            "attrs": attr_map,
            "text": [],
            "depth": len(self._stack),
        }
        self._stack.append(node)
        if tag == "nav" or self._is_nav_like(attr_map):
            self._nav_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if not self._stack:
            return
        node = self._stack.pop()
        text = re.sub(r"\s+", " ", "".join(node.get("text", []))).strip()
        attrs = node.get("attrs", {})
        depth = int(node.get("depth", 0) or 0)

        if tag == "title" and text and not self.page_title:
            self.page_title = text

        if tag in {"h1", "h2", "h3", "h4"} and 3 <= len(text) <= 200:
            self.main_blocks.append(
                PageBlock(
                    block_type="heading",
                    content=text,
                    selector=self._selector_for(tag, attrs),
                    depth=depth,
                    attributes={"level": tag},
                )
            )
        elif tag == "p" and 20 <= len(text) <= 500:
            self.main_blocks.append(
                PageBlock(
                    block_type="paragraph",
                    content=text[:200],
                    selector=self._selector_for(tag, attrs),
                    depth=depth,
                    attributes={},
                )
            )
        elif tag in {"ul", "ol"} and 10 <= len(text) <= 500:
            self.main_blocks.append(
                PageBlock(
                    block_type="list",
                    content=text[:240],
                    selector=self._selector_for(tag, attrs),
                    depth=depth,
                    attributes={},
                )
            )

        if self._nav_depth > 0 and text and 2 <= len(text) <= 120:
            self.navigation_blocks.append(
                PageBlock(
                    block_type="navigation",
                    content=text[:120],
                    selector=self._selector_for(tag, attrs),
                    depth=depth,
                    attributes={},
                )
            )

        if tag in {"a", "button", "input", "textarea", "select"}:
            entry = {
                "type": attrs.get("type") or tag,
                "text": text[:120],
                "selector": self._selector_for(tag, attrs),
                "attributes": {
                    "href": attrs.get("href", ""),
                    "name": attrs.get("name", ""),
                    "id": attrs.get("id", ""),
                    "placeholder": attrs.get("placeholder", ""),
                },
            }
            self.interactive_elements.append(entry)

        if tag == "nav" or self._is_nav_like(attrs):
            self._nav_depth = max(0, self._nav_depth - 1)

    def handle_data(self, data: str) -> None:
        if not data.strip():
            return
        for node in self._stack:
            if node.get("tag") in {"script", "style", "noscript"}:
                continue
            node.setdefault("text", []).append(data)

    def _selector_for(self, tag: str, attrs: Dict[str, str]) -> str:
        element_id = attrs.get("id", "").strip()
        if element_id:
            return f"#{_css_escape_token(element_id)}"
        classes = [part for part in attrs.get("class", "").split() if part][:2]
        if classes:
            escaped = [_css_escape_token(part) for part in classes if _css_escape_token(part)]
            if escaped:
                return tag + "".join(f".{part}" for part in escaped)
        name = attrs.get("name", "").strip()
        if name:
            return f'{tag}[name="{_css_escape_token(name)}"]'
        return tag

    @staticmethod
    def _is_nav_like(attrs: Dict[str, str]) -> bool:
        haystack = " ".join([attrs.get("class", ""), attrs.get("id", ""), attrs.get("role", "")]).lower()
        return any(token in haystack for token in ("nav", "menu", "navbar", "navigation"))


async def get_page_understanding(toolkit, task_description: str = "") -> str:
    """
    获取页面的语义化理解描述

    这是给 LLM 看的"页面说明书"，而不是原始 HTML
    """
    perceiver = PagePerceiver()
    structure = await perceiver.perceive_page(toolkit, task_description)
    return structure.to_llm_prompt()
