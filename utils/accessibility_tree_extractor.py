"""
Accessibility Tree提取器 - 2026现代化方法
基于OpenClaw和Web标准的accessibility API

核心思想：
1. 使用浏览器原生的accessibility tree（比手动解析DOM更准确）
2. 为每个可交互元素分配唯一ref（类似OpenClaw）
3. 提供紧凑的语义化描述（减少LLM上下文）
"""

from typing import List, Dict, Any, Optional
from dataclasses import dataclass, field
import json


@dataclass
class AccessibleElement:
    """
    可访问性元素（基于ARIA标准）

    ref系统：每个元素有唯一的引用ID，LLM只需要说"点击btn-5"
    而不是生成复杂的CSS选择器
    """
    ref: str  # 唯一引用ID，如 "btn-1", "link-5", "input-3"
    role: str  # ARIA role: button, link, textbox, searchbox, etc.
    name: str  # 可访问名称（来自label/aria-label/text content）
    tag: str  # HTML标签名
    selector: str  # CSS选择器（用于实际操作）
    xpath: str = ""  # XPath（备用定位方式）
    bbox: Dict[str, float] = field(default_factory=dict)  # {x, y, width, height}
    is_visible: bool = True
    is_interactive: bool = True
    is_focusable: bool = True
    context_before: str = ""  # 元素前的文本上下文
    context_after: str = ""   # 元素后的文本上下文
    parent_ref: str = ""  # 父元素ref
    children_refs: List[str] = field(default_factory=list)  # 子元素refs
    attributes: Dict[str, str] = field(default_factory=dict)  # 其他属性
    region: str = ""  # 所属区域：header, nav, main, footer, aside

    def to_compact_string(self) -> str:
        """转换为紧凑的字符串表示（用于LLM上下文）"""
        parts = [f"[{self.ref}]", f"{self.role}:"]

        # 名称（截断）
        name = self.name[:60] if self.name else "(无文本)"
        parts.append(name)

        # 上下文（如果有）
        if self.context_before or self.context_after:
            ctx = f"{self.context_before[:20]}...{self.context_after[:20]}"
            parts.append(f"(上下文: {ctx})")

        # 区域
        if self.region:
            parts.append(f"[{self.region}]")

        return " ".join(parts)


class AccessibilityTreeExtractor:
    """
    Accessibility Tree提取器

    使用Playwright的accessibility API提取页面的可访问性树
    这比手动解析DOM更准确，因为：
    1. 浏览器已经计算好了可访问性信息
    2. 自动处理了ARIA属性
    3. 过滤了不可见和不可交互的元素
    """

    # 我们关心的可交互角色
    INTERACTIVE_ROLES = {
        'button', 'link', 'textbox', 'searchbox', 'combobox',
        'checkbox', 'radio', 'tab', 'menuitem', 'menuitemcheckbox',
        'menuitemradio', 'option', 'switch', 'slider', 'spinbutton',
        'listbox', 'tree', 'grid', 'treegrid', 'tablist'
    }

    # 容器角色（用于识别区域）
    CONTAINER_ROLES = {
        'banner': 'header',
        'navigation': 'nav',
        'main': 'main',
        'contentinfo': 'footer',
        'complementary': 'aside',
        'region': 'section',
        'form': 'form',
        'search': 'search'
    }

    def __init__(self):
        self.ref_counter = 0
        self.elements: List[AccessibleElement] = []
        self.ref_to_element: Dict[str, AccessibleElement] = {}

    async def extract_tree(self, page) -> List[AccessibleElement]:
        """
        提取accessibility tree

        Args:
            page: Playwright page对象

        Returns:
            可交互元素列表
        """
        self.ref_counter = 0
        self.elements = []
        self.ref_to_element = {}

        # 方法1: 使用Playwright的accessibility API（推荐）
        try:
            snapshot = await page.accessibility.snapshot()
            if snapshot:
                self._traverse_a11y_tree(snapshot)
                # Resolve selectors for a11y-sourced elements (they have empty tag/selector)
                if self.elements:
                    await self._resolve_a11y_selectors(page)
        except Exception:
            pass

        # 方法2: 如果方法1失败，使用JavaScript提取
        if not self.elements:
            await self._extract_via_javascript(page)

        # 增强：添加上下文信息
        await self._enrich_with_context(page)

        return self.elements

    def _traverse_a11y_tree(self, node: Dict, parent_ref: str = "",
                           current_region: str = "", depth: int = 0):
        """
        遍历accessibility tree节点

        Args:
            node: accessibility tree节点
            parent_ref: 父元素的ref
            current_region: 当前所在区域
            depth: 当前深度
        """
        if not node or depth > 15:  # 限制深度防止无限递归
            return

        role = node.get('role', '')
        name = node.get('name', '')

        # 更新当前区域
        if role in self.CONTAINER_ROLES:
            current_region = self.CONTAINER_ROLES[role]

        # 如果是可交互元素，记录它
        if role in self.INTERACTIVE_ROLES:
            self.ref_counter += 1

            # 生成ref（格式：role前缀-序号）
            role_prefix = role[:3] if len(role) >= 3 else role
            ref = f"{role_prefix}-{self.ref_counter}"

            element = AccessibleElement(
                ref=ref,
                role=role,
                name=name,
                tag='',  # 稍后从DOM获取
                selector='',  # 稍后从DOM获取
                is_visible=True,  # a11y tree中的都是可见的
                is_interactive=True,
                parent_ref=parent_ref,
                region=current_region
            )

            self.elements.append(element)
            self.ref_to_element[ref] = element

            # 递归处理子节点
            for child in node.get('children', []):
                self._traverse_a11y_tree(child, ref, current_region, depth + 1)
        else:
            # 非交互元素，继续遍历子节点（不改变parent_ref）
            for child in node.get('children', []):
                self._traverse_a11y_tree(child, parent_ref, current_region, depth + 1)

    async def _resolve_a11y_selectors(self, page):
        """
        For elements sourced from the a11y tree, resolve their tag and CSS selector
        by matching role+name against DOM elements via JavaScript.
        """
        queries = [{"role": e.role, "name": e.name} for e in self.elements if not e.selector]
        if not queries:
            return
        try:
            results = await page.evaluate("""
                (queries) => {
                    const normalize = (v) => String(v || '').replace(/\\s+/g, ' ').trim();
                    const selectorOf = (el) => {
                        if (!el) return '';
                        const stableAttrs = ['data-testid', 'data-id', 'data-cy', 'data-qa', 'data-test'];
                        for (const attr of stableAttrs) { const v = el.getAttribute(attr); if (v) return `[${attr}="${CSS.escape(v)}"]`; }
                        if (el.id) return `#${CSS.escape(el.id)}`;
                        const name = el.getAttribute('name');
                        if (name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
                        const ph = el.getAttribute('placeholder');
                        if (ph) return `${el.tagName.toLowerCase()}[placeholder="${CSS.escape(ph)}"]`;
                        const href = el.getAttribute('href');
                        if (href && href.length <= 200) return `${el.tagName.toLowerCase()}[href="${CSS.escape(href)}"]`;
                        return el.tagName.toLowerCase();
                    };
                    const roleToSelector = {
                        'button': 'button, [role="button"], input[type="button"], input[type="submit"]',
                        'link': 'a[href], [role="link"]',
                        'textbox': 'input:not([type="hidden"]), textarea, [role="textbox"]',
                        'searchbox': 'input[type="search"], [role="searchbox"]',
                        'combobox': 'select, [role="combobox"]',
                        'checkbox': 'input[type="checkbox"], [role="checkbox"]',
                        'radio': 'input[type="radio"], [role="radio"]',
                        'tab': '[role="tab"]',
                        'menuitem': '[role="menuitem"]',
                        'switch': '[role="switch"]',
                        'slider': 'input[type="range"], [role="slider"]',
                    };
                    return queries.map(q => {
                        const candidates = document.querySelectorAll(roleToSelector[q.role] || `[role="${q.role}"]`);
                        for (const el of candidates) {
                            const name = normalize(
                                el.getAttribute('aria-label') || el.getAttribute('title') ||
                                el.getAttribute('placeholder') || el.textContent || el.value || ''
                            );
                            if (name && name.includes(q.name.substring(0, 30))) {
                                return { tag: el.tagName.toLowerCase(), selector: selectorOf(el) };
                            }
                        }
                        return { tag: '', selector: '' };
                    });
                }
            """, queries)
            idx = 0
            for elem in self.elements:
                if not elem.selector:
                    if idx < len(results):
                        elem.tag = results[idx].get('tag', '')
                        elem.selector = results[idx].get('selector', '')
                    idx += 1
        except Exception:
            pass

    async def _extract_via_javascript(self, page):
        """
        使用JavaScript提取可交互元素（降级方案）

        当Playwright的accessibility API不可用时使用
        """
        elements_data = await page.evaluate("""
            () => {
                const elements = [];
                const interactiveSelectors = [
                    'a[href]',
                    'button',
                    'input:not([type="hidden"])',
                    'select',
                    'textarea',
                    '[role="button"]',
                    '[role="link"]',
                    '[role="textbox"]',
                    '[role="searchbox"]',
                    '[role="combobox"]',
                    '[role="checkbox"]',
                    '[role="radio"]',
                    '[role="tab"]',
                    '[role="menuitem"]',
                    '[onclick]',
                    '[tabindex]:not([tabindex="-1"])'
                ];

                const allElements = document.querySelectorAll(interactiveSelectors.join(','));

                allElements.forEach((el, index) => {
                    // 检查可见性
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    const isVisible = rect.width > 0 && rect.height > 0 &&
                                     style.display !== 'none' &&
                                     style.visibility !== 'hidden' &&
                                     style.opacity !== '0';

                    if (!isVisible) return;

                    // 获取角色
                    let role = el.getAttribute('role') || '';
                    if (!role) {
                        const tagName = el.tagName.toLowerCase();
                        if (tagName === 'a') role = 'link';
                        else if (tagName === 'button') role = 'button';
                        else if (tagName === 'input') {
                            const type = el.getAttribute('type') || 'text';
                            role = type === 'checkbox' ? 'checkbox' :
                                   type === 'radio' ? 'radio' : 'textbox';
                        }
                        else if (tagName === 'select') role = 'combobox';
                        else if (tagName === 'textarea') role = 'textbox';
                        else role = 'button';  // 默认
                    }

                    // 获取名称
                    let name = el.getAttribute('aria-label') ||
                              el.getAttribute('title') ||
                              el.getAttribute('placeholder') ||
                              el.textContent.trim().substring(0, 100) ||
                              el.getAttribute('value') ||
                              '';

                    // 生成选择器
                    let selector = '';
                    if (el.id) {
                        selector = `#${el.id}`;
                    } else if (el.className) {
                        const classes = el.className.split(' ').filter(c => c).join('.');
                        selector = `${el.tagName.toLowerCase()}.${classes}`;
                    } else {
                        selector = el.tagName.toLowerCase();
                    }

                    // 获取区域
                    let region = '';
                    let parent = el.closest('header, nav, main, footer, aside, [role="banner"], [role="navigation"], [role="main"], [role="contentinfo"]');
                    if (parent) {
                        region = parent.tagName.toLowerCase();
                        if (region === 'div') {
                            region = parent.getAttribute('role') || '';
                        }
                    }

                    elements.push({
                        index: index,
                        tag: el.tagName.toLowerCase(),
                        role: role,
                        name: name,
                        selector: selector,
                        bbox: {
                            x: rect.left,
                            y: rect.top,
                            width: rect.width,
                            height: rect.height
                        },
                        region: region,
                        attributes: {
                            href: el.getAttribute('href') || '',
                            type: el.getAttribute('type') || '',
                            value: el.getAttribute('value') || ''
                        }
                    });
                });

                return elements;
            }
        """)

        # 转换为AccessibleElement对象
        for data in elements_data:
            self.ref_counter += 1
            role = data['role']
            role_prefix = role[:3] if len(role) >= 3 else role
            ref = f"{role_prefix}-{self.ref_counter}"

            element = AccessibleElement(
                ref=ref,
                role=role,
                name=data['name'],
                tag=data['tag'],
                selector=data['selector'],
                bbox=data['bbox'],
                is_visible=True,
                is_interactive=True,
                region=data['region'],
                attributes=data['attributes']
            )

            self.elements.append(element)
            self.ref_to_element[ref] = element

    async def _enrich_with_context(self, page):
        """
        为元素添加上下文信息

        上下文帮助LLM理解元素的语义位置
        """
        if not self.elements:
            return

        # 批量获取上下文
        contexts = await page.evaluate("""
            (selectors) => {
                return selectors.map(selector => {
                    try {
                        const el = document.querySelector(selector);
                        if (!el) return {before: '', after: ''};

                        // 获取前后文本节点
                        let before = '';
                        let after = '';

                        // 前面的文本
                        let prev = el.previousSibling;
                        while (prev && before.length < 50) {
                            if (prev.nodeType === 3) {  // Text node
                                before = prev.textContent.trim() + ' ' + before;
                            }
                            prev = prev.previousSibling;
                        }

                        // 后面的文本
                        let next = el.nextSibling;
                        while (next && after.length < 50) {
                            if (next.nodeType === 3) {  // Text node
                                after = after + ' ' + next.textContent.trim();
                            }
                            next = next.nextSibling;
                        }

                        return {
                            before: before.trim(),
                            after: after.trim()
                        };
                    } catch (e) {
                        return {before: '', after: ''};
                    }
                });
            }
        """, [elem.selector for elem in self.elements])

        # 更新元素
        for elem, ctx in zip(self.elements, contexts):
            elem.context_before = ctx['before']
            elem.context_after = ctx['after']

    def to_llm_context(self, max_elements: int = 50,
                      filter_region: Optional[str] = None) -> str:
        """
        转换为LLM友好的紧凑格式

        Args:
            max_elements: 最多包含多少个元素
            filter_region: 只包含特定区域的元素（如'nav', 'main'）

        Returns:
            紧凑的文本描述
        """
        elements = self.elements

        # 过滤区域
        if filter_region:
            elements = [e for e in elements if e.region == filter_region]

        lines = ["# 页面可交互元素（使用ref引用）\n"]

        # 按区域分组
        by_region = {}
        for elem in elements[:max_elements]:
            region = elem.region or 'other'
            if region not in by_region:
                by_region[region] = []
            by_region[region].append(elem)

        # 输出
        for region, elems in by_region.items():
            if region != 'other':
                lines.append(f"\n## {region.upper()}区域")
            for elem in elems:
                lines.append(elem.to_compact_string())

        if len(self.elements) > max_elements:
            lines.append(f"\n... 还有 {len(self.elements) - max_elements} 个元素未显示")

        return "\n".join(lines)

    def get_element_by_ref(self, ref: str) -> Optional[AccessibleElement]:
        """通过ref获取元素"""
        return self.ref_to_element.get(ref)

    def to_json(self) -> str:
        """导出为JSON（用于调试）"""
        data = [
            {
                'ref': e.ref,
                'role': e.role,
                'name': e.name,
                'selector': e.selector,
                'region': e.region,
                'bbox': e.bbox
            }
            for e in self.elements
        ]
        return json.dumps(data, ensure_ascii=False, indent=2)
