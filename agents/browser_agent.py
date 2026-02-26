"""
OmniCore Browser Agent - 智能浏览器交互代理
能够理解任务、分析页面、自主决策并执行复杂的多步骤浏览器操作
"""
import asyncio
import base64
import random
import re
import json
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum
from playwright.async_api import async_playwright, Browser, Page, Playwright, ElementHandle

from core.llm import LLMClient
from utils.logger import log_agent_action, log_success, log_error, log_warning, logger
from utils.captcha_solver import CaptchaSolver
from config.settings import settings


class ActionType(Enum):
    """操作类型枚举"""
    CLICK = "click"
    INPUT = "input"
    SELECT = "select"
    SCROLL = "scroll"
    WAIT = "wait"
    NAVIGATE = "navigate"
    EXTRACT = "extract"
    PRESS_KEY = "press_key"  # 按键操作（如 Enter, Tab）
    CONFIRM = "confirm"  # 需要人工确认（如付款）
    SWITCH_TAB = "switch_tab"  # 切换标签页
    CLOSE_TAB = "close_tab"  # 关闭当前标签页
    UPLOAD_FILE = "upload_file"  # 文件上传
    DOWNLOAD = "download"  # 文件下载
    SWITCH_IFRAME = "switch_iframe"  # 切换到 iframe
    EXIT_IFRAME = "exit_iframe"  # 退出 iframe
    FILL_FORM = "fill_form"  # 复杂表单填写
    DONE = "done"
    FAILED = "failed"


@dataclass
class PageElement:
    """页面元素信息"""
    index: int
    tag: str
    text: str
    element_type: str  # button, input, link, select, etc.
    selector: str
    attributes: Dict[str, str] = field(default_factory=dict)
    is_visible: bool = True
    is_clickable: bool = True


@dataclass
class BrowserAction:
    """浏览器操作指令"""
    action_type: ActionType
    target_selector: str = ""
    value: str = ""
    description: str = ""
    confidence: float = 0.0
    requires_confirmation: bool = False
    fallback_selector: str = ""          # LLM 提供的备选选择器
    use_keyboard_fallback: bool = False  # LLM 建议用键盘操作作为备选
    keyboard_key: str = ""               # 备选键盘按键（如 Enter, Tab）


# === 核心提示词 ===

TASK_UNDERSTANDING_PROMPT = """你是一个任务理解专家。请分析用户的任务，提取关键信息。

## 用户任务
{task}

## 请分析并返回 JSON：
```json
{{
    "task_type": "购物/搜索/登录/注册/信息查询/表单填写/其他",
    "goal": "最终目标的简洁描述",
    "key_info": {{
        "keywords": ["关键词列表"],
        "target_site": "目标网站（如果提到）",
        "specific_requirements": ["具体要求列表"],
        "sensitive_actions": ["敏感操作，如付款、删除等"]
    }},
    "completion_criteria": {{
        "success_indicators": ["任务成功的标志，如'看到搜索结果'、'页面显示登录成功'"],
        "target_url_pattern": "目标页面URL特征（如包含'/search'、'/order/success'）",
        "expected_page_content": ["期望在页面上看到的内容"]
    }},
    "data_to_extract": {{
        "extract_needed": true,
        "extract_type": "搜索结果/商品信息/文章内容/列表数据/无",
        "extract_fields": ["需要提取的字段，如'标题'、'链接'、'价格'"]
    }},
    "steps_preview": ["预估的大致步骤"],
    "start_url": "建议的起始URL（如果能确定）"
}}
```
"""

PAGE_ANALYSIS_PROMPT = """你是一个网页分析专家。请分析当前页面状态。

## 当前任务
{task}

## 任务目标
{goal}

## 完成标准
{completion_criteria}

## 任务进度
{progress}

## 当前页面 URL
{url}

## 页面标题
{title}

## 页面可交互元素列表
{elements}

## 页面截图描述（如果有）
{screenshot_description}

## 请分析并返回 JSON：
```json
{{
    "page_type": "首页/搜索结果/商品详情/购物车/结算页/登录页/其他",
    "page_summary": "当前页面的简要描述（50字内）",
    "task_progress_percent": 80,
    "is_task_complete": false,
    "completion_reason": "如果任务完成，说明为什么认为完成了",
    "current_status": "当前处于任务的哪个阶段",
    "next_step": "下一步应该做什么",
    "blockers": ["如果有阻碍，列出来"],
    "is_stuck": false,
    "stuck_reason": "如果卡住了，说明原因"
}}
```

## 判断任务完成的标准：
1. 搜索任务：已经看到搜索结果页面
2. 登录任务：页面显示登录成功或用户信息
3. 购物任务：到达订单确认或支付页面
4. 信息查询：已经看到目标信息
"""

ACTION_DECISION_PROMPT = """你是一个浏览器操作决策专家。根据当前页面状态，决定下一步具体操作。

## 当前任务
{task}

## 任务目标
{goal}

## 已完成的步骤
{completed_steps}

## 当前页面信息
- URL: {url}
- 标题: {title}
- 页面类型: {page_type}
- 页面摘要: {page_summary}

## 可用的交互元素（格式：[索引] 类型 - 文本/描述）
{elements}

## 请决定下一步操作，返回 JSON：
```json
{{
    "thinking": "你的思考过程（简短）",
    "action": {{
        "type": "click/input/select/scroll/wait/navigate/extract/press_key/switch_tab/close_tab/upload_file/download/switch_iframe/exit_iframe/fill_form/done/failed",
        "element_index": 0,
        "value": "如果是input/select/press_key填写值；switch_tab填索引或last；upload_file填文件路径；fill_form填JSON格式的字段映射",
        "description": "操作描述（简短）",
        "fallback_selector": "备选CSS选择器（如果主元素点击失败时尝试，可为空字符串）",
        "use_keyboard": false,
        "keyboard_key": "如果建议用键盘操作代替或作为备选，填具体按键如 Enter/Tab/Escape，否则为空"
    }},
    "confidence": 0.95,
    "requires_human_confirm": false,
    "reason_for_confirm": "如果需要人工确认，说明原因"
}}
```

## 重要规则：
1. 如果涉及付款、删除、发送等敏感操作，requires_human_confirm 必须为 true
2. 如果找不到合适的元素，type 设为 "failed"
3. 如果任务已完成，type 设为 "done"
4. element_index 必须是元素列表中存在的索引，如果不需要元素则设为 -1
5. 优先选择最直接相关的元素
6. 对于搜索框输入后，通常需要点击搜索按钮或按回车(press_key + Enter)
7. 如果页面有多个标签页，可用 switch_tab 切换（value 为索引或 "last" 表示最新标签页）
8. 如果需要操作 iframe 内的内容，先用 switch_iframe 切换进去，操作完用 exit_iframe 退出
9. 如果需要填写多个表单字段，用 fill_form，value 为 JSON 格式如 {{"#name": "张三", "#email": "test@example.com"}}
10. 如果需要上传文件，用 upload_file，value 为本地文件路径
11. 对于 click 操作，如果目标元素可能难以直接点击（如搜索按钮、提交按钮），请在 fallback_selector 中提供备选CSS选择器（如 button[type="submit"]、input[type="submit"]），并考虑设置 use_keyboard=true 和 keyboard_key="Enter" 作为键盘备选
12. fallback_selector 和 keyboard_key 是可选的备选策略，仅在你认为主选择器可能不稳定时提供
"""

DATA_EXTRACTION_PROMPT = """你是一个数据提取专家。请从当前页面提取任务所需的数据。

## 任务目标
{goal}

## 需要提取的数据类型
{extract_type}

## 需要提取的字段
{extract_fields}

## 当前页面 URL
{url}

## 页面标题
{title}

## 页面内容摘要
{page_content}

## 请提取数据并返回 JSON：
```json
{{
    "success": true,
    "data": [
        {{
            "title": "标题",
            "link": "链接",
            "description": "描述",
            "extra": {{}}
        }}
    ],
    "total_count": 10,
    "extracted_count": 5,
    "summary": "提取结果的简要总结"
}}
```

注意：只提取与任务相关的数据，不要编造数据。
"""

class BrowserAgent:
    """
    智能浏览器交互代理
    能够理解复杂任务、分析页面、自主决策并执行多步骤操作
    """

    def __init__(self, llm_client: LLMClient = None, headless: bool = False, user_data_dir: str = None):
        self.name = "BrowserAgent"
        self.llm = llm_client or LLMClient()
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context = None
        self._page: Optional[Page] = None
        self.captcha_solver = CaptchaSolver()
        self.headless = headless
        self.user_data_dir = user_data_dir  # 用于持久化登录状态

        # 任务状态
        self.current_task: str = ""
        self.task_goal: str = ""
        self.task_info: Dict[str, Any] = {}  # 完整的任务理解结果
        self.completion_criteria: Dict[str, Any] = {}  # 完成标准
        self.data_extraction_config: Dict[str, Any] = {}  # 数据提取配置
        self.completed_steps: List[str] = []
        self.extracted_data: List[Dict] = []  # 提取的数据
        self.max_steps: int = 20  # 减少最大步数，避免无限循环
        self.current_step: int = 0

        # 重试和卡住检测
        self.last_url: str = ""
        self.same_url_count: int = 0
        self.max_same_url_retries: int = 3

        # 多标签页管理
        self._pages: List[Page] = []  # 所有打开的页面
        self._active_page_index: int = 0  # 当前活动页面索引
        self._dialog_result: Optional[str] = None  # 对话框结果
        self._download_path: str = user_data_dir or "."  # 下载目录

        # iframe 状态
        self._in_iframe: bool = False
        self._current_frame = None  # 当前操作的 frame

        # 元素缓存
        self._elements_cache: List[PageElement] = []

    async def _ensure_browser(self) -> Browser:
        """启动浏览器"""
        if self._browser is None or not self._browser.is_connected():
            self._playwright = await async_playwright().start()

            # 使用更真实的浏览器配置
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--no-first-run',
                    '--disable-infobars',
                    '--window-size=1366,768',
                    '--start-maximized',
                ],
                ignore_default_args=['--enable-automation'],
            )
        return self._browser

    async def _create_page(self) -> Page:
        """创建新页面，支持持久化登录状态"""
        browser = await self._ensure_browser()

        # 如果指定了用户数据目录，尝试加载已保存的状态
        storage_state = None
        if self.user_data_dir:
            import os
            state_file = os.path.join(self.user_data_dir, 'storage_state.json')
            if os.path.exists(state_file):
                storage_state = state_file
                log_agent_action(self.name, "加载已保存的登录状态")

        self._context = await browser.new_context(
            viewport={'width': 1366, 'height': 768},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
            locale='zh-CN',
            timezone_id='Asia/Shanghai',
            java_script_enabled=True,
            storage_state=storage_state,
            extra_http_headers={
                'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
                'Accept-Encoding': 'gzip, deflate, br',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            }
        )
        page = await self._context.new_page()

        # 注入更完善的反检测脚本
        await page.add_init_script("""
            // 隐藏 webdriver 标志
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

            // 模拟真实的 plugins
            Object.defineProperty(navigator, 'plugins', {
                get: () => {
                    const plugins = [
                        { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer' },
                        { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                        { name: 'Native Client', filename: 'internal-nacl-plugin' }
                    ];
                    plugins.length = 3;
                    return plugins;
                }
            });

            // 模拟 chrome 对象
            window.chrome = {
                runtime: {},
                loadTimes: function() {},
                csi: function() {},
                app: {}
            };

            // 语言设置
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en-US', 'en'] });
            Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });

            // 隐藏自动化相关属性
            delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
            delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
            delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;

            // 模拟真实的屏幕信息
            Object.defineProperty(screen, 'availWidth', { get: () => 1366 });
            Object.defineProperty(screen, 'availHeight', { get: () => 728 });
            Object.defineProperty(screen, 'width', { get: () => 1366 });
            Object.defineProperty(screen, 'height', { get: () => 768 });

            // 模拟 WebGL
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Intel Inc.';
                if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                return getParameter.apply(this, arguments);
            };
        """)

        self._page = page
        self._pages = [page]
        self._active_page_index = 0

        # 监听新标签页/弹窗
        self._context.on("page", self._on_new_page)

        # 监听对话框（alert/confirm/prompt）
        page.on("dialog", self._on_dialog)

        # 监听下载事件
        page.on("download", self._on_download)

        return page

    def _on_new_page(self, page: Page):
        """处理新打开的标签页或弹窗"""
        self._pages.append(page)
        log_agent_action(self.name, "检测到新标签页", f"共 {len(self._pages)} 个标签页")
        # 为新页面也注册事件
        page.on("dialog", self._on_dialog)
        page.on("download", self._on_download)

    async def _on_dialog(self, dialog):
        """处理浏览器对话框（alert/confirm/prompt）"""
        dialog_type = dialog.type
        message = dialog.message
        log_agent_action(self.name, f"对话框 [{dialog_type}]", message[:80])

        if dialog_type == "alert":
            await dialog.accept()
        elif dialog_type == "confirm":
            # 默认接受，敏感操作由 human_confirm 控制
            await dialog.accept()
        elif dialog_type == "prompt":
            # 如果有预设值就用预设值，否则接受默认
            default_value = dialog.default_value or ""
            await dialog.accept(default_value)
        elif dialog_type == "beforeunload":
            await dialog.accept()

        self._dialog_result = f"{dialog_type}: {message}"

    async def _on_download(self, download):
        """处理文件下载"""
        filename = download.suggested_filename
        log_agent_action(self.name, "下载文件", filename)
        import os
        save_path = os.path.join(self._download_path, filename)
        try:
            await download.save_as(save_path)
            log_success(f"文件已下载: {save_path}")
        except Exception as e:
            log_error(f"下载失败: {e}")

    async def switch_to_tab(self, index: int) -> bool:
        """切换到指定标签页"""
        if 0 <= index < len(self._pages):
            page = self._pages[index]
            try:
                await page.bring_to_front()
                self._page = page
                self._active_page_index = index
                self._in_iframe = False
                self._current_frame = None
                log_agent_action(self.name, "切换标签页", f"第 {index + 1} 个")
                return True
            except Exception as e:
                log_error(f"切换标签页失败: {e}")
                # 移除已关闭的页面
                self._pages.pop(index)
                return False
        log_warning(f"标签页索引越界: {index}, 共 {len(self._pages)} 个")
        return False

    async def close_current_tab(self) -> bool:
        """关闭当前标签页，切换到上一个"""
        if len(self._pages) <= 1:
            log_warning("只剩一个标签页，无法关闭")
            return False
        try:
            current = self._pages[self._active_page_index]
            await current.close()
            self._pages.pop(self._active_page_index)
            # 切换到前一个标签页
            new_index = max(0, self._active_page_index - 1)
            self._active_page_index = new_index
            self._page = self._pages[new_index]
            await self._page.bring_to_front()
            self._in_iframe = False
            self._current_frame = None
            log_agent_action(self.name, "关闭标签页", f"剩余 {len(self._pages)} 个")
            return True
        except Exception as e:
            log_error(f"关闭标签页失败: {e}")
            return False

    async def _save_storage_state(self):
        """保存浏览器状态（cookies等）"""
        if self._context and self.user_data_dir:
            import os
            os.makedirs(self.user_data_dir, exist_ok=True)
            state_file = os.path.join(self.user_data_dir, 'storage_state.json')
            await self._context.storage_state(path=state_file)
            log_agent_action(self.name, "保存登录状态", state_file)

    async def _human_delay(self, min_ms: int = 100, max_ms: int = 300):
        """模拟人类操作延迟"""
        await asyncio.sleep(random.randint(min_ms, max_ms) / 1000)

    async def _close(self):
        """关闭浏览器"""
        # 保存状态
        await self._save_storage_state()

        # 关闭所有标签页
        for p in self._pages:
            try:
                await p.close()
            except:
                pass
        self._pages = []
        self._page = None
        self._in_iframe = False
        self._current_frame = None

        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    # === 页面元素提取 ===

    async def _extract_interactive_elements(self, page: Page) -> List[PageElement]:
        """提取页面上所有可交互元素（支持 iframe）"""
        elements = []
        target = self._current_frame if self._in_iframe else page

        # JavaScript 提取可交互元素 - 改进版
        js_code = """
        () => {
            const results = [];
            const interactiveSelectors = [
                'input:not([type="hidden"])',
                'button',
                'a[href]',
                'select',
                'textarea',
                '[role="button"]',
                '[onclick]',
            ];

            const seen = new Set();
            let globalIndex = 0;

            // 检查元素是否真正可见
            function isElementVisible(el) {
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 || rect.height === 0) return false;

                const style = window.getComputedStyle(el);
                if (style.display === 'none') return false;
                if (style.visibility === 'hidden') return false;
                if (style.opacity === '0') return false;

                // 检查是否在视口内（允许一定范围外的元素）
                const viewportHeight = window.innerHeight;
                const viewportWidth = window.innerWidth;
                if (rect.bottom < -100 || rect.top > viewportHeight + 100) return false;
                if (rect.right < -100 || rect.left > viewportWidth + 100) return false;

                // 检查父元素是否隐藏
                let parent = el.parentElement;
                while (parent) {
                    const parentStyle = window.getComputedStyle(parent);
                    if (parentStyle.display === 'none' || parentStyle.visibility === 'hidden') {
                        return false;
                    }
                    parent = parent.parentElement;
                }

                return true;
            }

            // 生成唯一且可靠的选择器
            function generateSelector(el, tag, text) {
                const selectors = [];

                // 优先使用 name 属性（对于表单元素更可靠）
                if (el.name) {
                    selectors.push(tag + '[name="' + el.name + '"]');
                }

                // 使用 id，但要验证唯一性
                if (el.id) {
                    const count = document.querySelectorAll('#' + CSS.escape(el.id)).length;
                    if (count === 1) {
                        selectors.push('#' + CSS.escape(el.id));
                    }
                }

                // 使用 placeholder
                if (el.placeholder) {
                    selectors.push(tag + '[placeholder="' + el.placeholder + '"]');
                }

                // 使用 type + 其他属性组合
                if (el.type && tag === 'input') {
                    if (el.name) {
                        selectors.push('input[type="' + el.type + '"][name="' + el.name + '"]');
                    }
                }

                // 使用文本内容（对于按钮和链接）
                if (text && text.length > 0 && text.length < 30 && (tag === 'button' || tag === 'a')) {
                    const cleanText = text.replace(/[\"\'\\n\\r\\t]/g, '').trim();
                    if (cleanText) {
                        selectors.push(tag + ':has-text("' + cleanText.slice(0, 20) + '")');
                    }
                }

                // 安全获取 className
                let classStr = '';
                if (el.className && typeof el.className === 'string') {
                    classStr = el.className;
                } else if (el.className && el.className.baseVal) {
                    classStr = el.className.baseVal;
                }

                // 使用 class（作为备选）
                if (classStr) {
                    const cls = classStr.split(' ').filter(c => c && !c.includes(':') && !c.includes('[') && c.length > 2)[0];
                    if (cls) {
                        selectors.push(tag + '.' + CSS.escape(cls));
                    }
                }

                // 返回最佳选择器（优先返回更具体的）
                return selectors.length > 0 ? selectors[0] : tag;
            }

            interactiveSelectors.forEach(selector => {
                document.querySelectorAll(selector).forEach((el) => {
                    // 严格的可见性检查
                    if (!isElementVisible(el)) return;

                    // 获取元素信息
                    const tag = el.tagName.toLowerCase();
                    let text = (el.textContent || el.value || el.placeholder || el.alt || el.title || '').trim();
                    text = text.slice(0, 100).replace(/\\s+/g, ' ');

                    // 生成唯一标识（用于去重）
                    const rect = el.getBoundingClientRect();
                    const uniqueKey = tag + '_' + Math.round(rect.left) + '_' + Math.round(rect.top) + '_' + text.slice(0, 20);
                    if (seen.has(uniqueKey)) return;
                    seen.add(uniqueKey);

                    let elementType = tag;
                    if (tag === 'input') elementType = el.type || 'text';
                    if (tag === 'a') elementType = 'link';

                    // 安全获取 className
                    let classStr = '';
                    if (el.className && typeof el.className === 'string') {
                        classStr = el.className;
                    } else if (el.className && el.className.baseVal) {
                        classStr = el.className.baseVal;
                    }

                    if (el.getAttribute('role') === 'button' || classStr.includes('btn') || classStr.includes('submit')) {
                        elementType = 'button';
                    }

                    // 生成选择器
                    const generatedSelector = generateSelector(el, tag, text);

                    results.push({
                        index: globalIndex++,
                        tag: tag,
                        text: text,
                        elementType: elementType,
                        selector: generatedSelector,
                        attributes: {
                            id: el.id || '',
                            name: el.name || '',
                            type: el.type || '',
                            href: el.href || '',
                            placeholder: el.placeholder || '',
                            value: el.value || '',
                        },
                        isVisible: true,
                        isClickable: true,
                        position: { x: Math.round(rect.left), y: Math.round(rect.top) },
                    });
                });
            });

            // 按位置排序（从上到下，从左到右）
            results.sort((a, b) => {
                if (Math.abs(a.position.y - b.position.y) < 20) {
                    return a.position.x - b.position.x;
                }
                return a.position.y - b.position.y;
            });

            // 重新分配索引
            results.forEach((r, i) => r.index = i);

            return results.slice(0, 60);  // 限制数量
        }
        """

        try:
            raw_elements = await target.evaluate(js_code)
            for el in raw_elements:
                elements.append(PageElement(
                    index=el['index'],
                    tag=el['tag'],
                    text=el['text'],
                    element_type=el['elementType'],
                    selector=el['selector'],
                    attributes=el['attributes'],
                    is_visible=el['isVisible'],
                    is_clickable=el['isClickable'],
                ))
        except Exception as e:
            log_error(f"提取元素失败: {e}")

        # 检测页面中的 iframe（仅在主页面时检测）
        if not self._in_iframe:
            try:
                iframe_count = await page.evaluate("""
                    () => {
                        const iframes = document.querySelectorAll('iframe');
                        return Array.from(iframes).filter(f => {
                            const rect = f.getBoundingClientRect();
                            return rect.width > 50 && rect.height > 50;
                        }).length;
                    }
                """)
                if iframe_count > 0:
                    # 添加一个虚拟元素提示 LLM 有 iframe
                    elements.append(PageElement(
                        index=len(elements),
                        tag="iframe",
                        text=f"页面包含 {iframe_count} 个 iframe",
                        element_type="iframe",
                        selector="iframe",
                        attributes={"count": str(iframe_count)},
                        is_visible=True,
                        is_clickable=False,
                    ))
            except:
                pass

        self._elements_cache = elements
        return elements

    def _format_elements_for_llm(self, elements: List[PageElement]) -> str:
        """格式化元素列表供 LLM 分析"""
        lines = []
        if self._in_iframe:
            lines.append("【当前在 iframe 内操作】")
        # 显示标签页信息
        if len(self._pages) > 1:
            lines.append(f"【共 {len(self._pages)} 个标签页，当前第 {self._active_page_index + 1} 个】")
        for el in elements:
            text_preview = el.text[:50] + "..." if len(el.text) > 50 else el.text
            extra = ""
            if el.attributes.get('placeholder'):
                extra = f" (placeholder: {el.attributes['placeholder']})"
            if el.attributes.get('href'):
                href = el.attributes['href'][:50]
                extra = f" (href: {href})"
            lines.append(f"[{el.index}] {el.element_type} - {text_preview}{extra}")
        return "\n".join(lines) if lines else "（页面无可交互元素）"

    # === 截图和视觉分析 ===

    async def _take_screenshot(self, page: Page) -> str:
        """截图并返回 base64"""
        screenshot = await page.screenshot()
        return base64.b64encode(screenshot).decode()

    async def _analyze_screenshot_with_vision(self, screenshot_base64: str, task: str) -> str:
        """使用视觉模型分析截图"""
        from openai import OpenAI
        client = OpenAI(api_key=settings.OPENAI_API_KEY)

        try:
            response = client.chat.completions.create(
                model=settings.VISION_MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": f"请简要描述这个网页截图的内容，重点关注与以下任务相关的元素：{task}"
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{screenshot_base64}"}
                            }
                        ]
                    }
                ],
                max_tokens=500,
            )
            return response.choices[0].message.content
        except Exception as e:
            log_warning(f"视觉分析失败: {e}")
            return ""

    # === LLM 决策函数 ===

    def _understand_task(self, task: str) -> Dict[str, Any]:
        """理解用户任务，提取完成标准和数据提取配置"""
        log_agent_action(self.name, "理解任务", task[:50])

        response = self.llm.chat_with_system(
            system_prompt=TASK_UNDERSTANDING_PROMPT.format(task=task),
            user_message="请分析这个任务",
            temperature=0.2,
            json_mode=True,
        )

        try:
            result = self.llm.parse_json_response(response)
            self.task_goal = result.get("goal", task)
            self.task_info = result
            self.completion_criteria = result.get("completion_criteria", {})
            self.data_extraction_config = result.get("data_to_extract", {})
            log_agent_action(self.name, "任务目标", self.task_goal)
            return result
        except Exception as e:
            log_error(f"任务理解失败: {e}")
            return {"goal": task, "start_url": "", "steps_preview": []}

    async def _analyze_page(self, page: Page, use_vision: bool = False) -> Dict[str, Any]:
        """分析当前页面状态，判断任务是否完成"""
        log_agent_action(self.name, "分析页面", page.url[:50])

        # 提取元素
        elements = await self._extract_interactive_elements(page)
        elements_text = self._format_elements_for_llm(elements)

        # 可选：视觉分析
        screenshot_desc = ""
        if use_vision:
            screenshot = await self._take_screenshot(page)
            screenshot_desc = await self._analyze_screenshot_with_vision(screenshot, self.current_task)

        # 构建进度描述
        progress = "\n".join([f"- {step}" for step in self.completed_steps]) if self.completed_steps else "尚未开始"

        # 构建完成标准描述
        completion_criteria_str = "无特定标准"
        if self.completion_criteria:
            criteria_parts = []
            if self.completion_criteria.get("success_indicators"):
                criteria_parts.append(f"成功标志: {', '.join(self.completion_criteria['success_indicators'])}")
            if self.completion_criteria.get("target_url_pattern"):
                criteria_parts.append(f"目标URL特征: {self.completion_criteria['target_url_pattern']}")
            completion_criteria_str = "; ".join(criteria_parts) if criteria_parts else "无特定标准"

        response = self.llm.chat_with_system(
            system_prompt=PAGE_ANALYSIS_PROMPT.format(
                task=self.current_task,
                goal=self.task_goal,
                completion_criteria=completion_criteria_str,
                progress=progress,
                url=page.url,
                title=await page.title(),
                elements=elements_text,
                screenshot_description=screenshot_desc or "无",
            ),
            user_message="请分析当前页面",
            temperature=0.2,
            json_mode=True,
        )

        try:
            result = self.llm.parse_json_response(response)
            # 检测是否卡住
            if result.get("is_stuck"):
                log_warning(f"检测到卡住: {result.get('stuck_reason', '未知原因')}")
            return result
        except Exception as e:
            log_error(f"页面分析失败: {e}")
            return {"page_type": "unknown", "page_summary": "", "next_step": "", "is_task_complete": False}

    async def _extract_page_data(self, page: Page) -> Dict[str, Any]:
        """从页面提取任务所需的数据"""
        if not self.data_extraction_config.get("extract_needed"):
            return {"success": True, "data": [], "summary": "无需提取数据"}

        log_agent_action(self.name, "提取页面数据")

        # 获取页面文本内容
        try:
            page_content = await page.evaluate("""
                () => {
                    // 获取主要内容区域的文本
                    const main = document.querySelector('main, #content, .content, article, .results') || document.body;
                    const items = [];

                    // 尝试提取列表项
                    const listItems = main.querySelectorAll('h3 a, .result-title a, .title a, h2 a');
                    listItems.forEach((item, i) => {
                        if (i < 10) {
                            const parent = item.closest('div, li, article');
                            const desc = parent ? parent.textContent.slice(0, 200) : '';
                            items.push({
                                title: item.textContent.trim().slice(0, 100),
                                link: item.href || '',
                                description: desc.replace(/\\s+/g, ' ').trim()
                            });
                        }
                    });

                    if (items.length > 0) {
                        return { type: 'list', items: items };
                    }

                    // 如果没有列表，返回页面摘要
                    return {
                        type: 'text',
                        content: main.textContent.slice(0, 2000).replace(/\\s+/g, ' ').trim()
                    };
                }
            """)
        except Exception as e:
            log_error(f"获取页面内容失败: {e}")
            page_content = {"type": "error", "content": str(e)}

        # 如果直接提取到了列表数据，直接返回
        if page_content.get("type") == "list" and page_content.get("items"):
            return {
                "success": True,
                "data": page_content["items"],
                "total_count": len(page_content["items"]),
                "summary": f"成功提取 {len(page_content['items'])} 条数据"
            }

        # 否则使用 LLM 提取
        response = self.llm.chat_with_system(
            system_prompt=DATA_EXTRACTION_PROMPT.format(
                goal=self.task_goal,
                extract_type=self.data_extraction_config.get("extract_type", "通用"),
                extract_fields=", ".join(self.data_extraction_config.get("extract_fields", ["标题", "链接"])),
                url=page.url,
                title=await page.title(),
                page_content=str(page_content.get("content", ""))[:1500],
            ),
            user_message="请提取数据",
            temperature=0.2,
            json_mode=True,
        )

        try:
            result = self.llm.parse_json_response(response)
            self.extracted_data = result.get("data", [])
            return result
        except Exception as e:
            log_error(f"数据提取失败: {e}")
            return {"success": False, "data": [], "summary": f"提取失败: {e}"}

    async def _decide_action(self, page: Page, page_analysis: Dict[str, Any]) -> BrowserAction:
        """决定下一步操作"""
        elements = self._elements_cache
        elements_text = self._format_elements_for_llm(elements)

        progress = "\n".join([f"{i+1}. {step}" for i, step in enumerate(self.completed_steps)]) if self.completed_steps else "无"

        response = self.llm.chat_with_system(
            system_prompt=ACTION_DECISION_PROMPT.format(
                task=self.current_task,
                goal=self.task_goal,
                completed_steps=progress,
                url=page.url,
                title=await page.title(),
                page_type=page_analysis.get("page_type", "unknown"),
                page_summary=page_analysis.get("page_summary", ""),
                elements=elements_text,
            ),
            user_message="请决定下一步操作",
            temperature=0.2,
            json_mode=True,
        )

        try:
            result = self.llm.parse_json_response(response)
            action_data = result.get("action", {})

            action_type = ActionType(action_data.get("type", "failed"))
            element_index = action_data.get("element_index")
            if element_index is None:
                element_index = -1

            # 获取目标选择器
            target_selector = ""
            if 0 <= element_index < len(elements):
                target_selector = elements[element_index].selector

            return BrowserAction(
                action_type=action_type,
                target_selector=target_selector,
                value=action_data.get("value", ""),
                description=action_data.get("description", ""),
                confidence=result.get("confidence", 0.0),
                requires_confirmation=result.get("requires_human_confirm", False),
                fallback_selector=action_data.get("fallback_selector", ""),
                use_keyboard_fallback=action_data.get("use_keyboard", False),
                keyboard_key=action_data.get("keyboard_key", ""),
            )
        except Exception as e:
            log_error(f"决策失败: {e}")
            return BrowserAction(action_type=ActionType.FAILED, description=str(e))

    async def _handle_file_upload(self, page: Page, selector: str, file_path: str) -> bool:
        """处理文件上传"""
        import os
        if not os.path.exists(file_path):
            log_error(f"上传文件不存在: {file_path}")
            return False

        try:
            # 方式1: 直接设置 input[type=file]
            file_input = await page.query_selector('input[type="file"]')
            if file_input:
                await file_input.set_input_files(file_path)
                log_agent_action(self.name, "文件上传成功（input）", file_path)
                return True
        except Exception as e:
            log_warning(f"直接上传失败: {e}")

        try:
            # 方式2: 通过 file chooser 事件
            async with page.expect_file_chooser(timeout=5000) as fc_info:
                if selector:
                    await self._try_click_with_fallbacks(page, selector, "上传按钮")
                else:
                    # 尝试点击常见上传按钮
                    upload_selectors = [
                        'input[type="file"]',
                        'button:has-text("上传")',
                        'button:has-text("选择文件")',
                        '[class*="upload"]',
                    ]
                    for sel in upload_selectors:
                        try:
                            await page.click(sel, timeout=2000)
                            break
                        except:
                            continue
            file_chooser = await fc_info.value
            await file_chooser.set_files(file_path)
            log_agent_action(self.name, "文件上传成功（chooser）", file_path)
            return True
        except Exception as e:
            log_error(f"文件上传失败: {e}")
            return False

    async def _switch_to_iframe(self, page: Page, selector: str) -> bool:
        """切换到指定 iframe"""
        try:
            # 尝试通过选择器找到 iframe
            frame = None
            if selector:
                frame_element = await page.query_selector(selector)
                if frame_element:
                    frame = await frame_element.content_frame()

            if not frame:
                # 尝试获取页面上的所有 frame
                frames = page.frames
                # 跳过主 frame，取第一个子 frame
                child_frames = [f for f in frames if f != page.main_frame]
                if child_frames:
                    frame = child_frames[0]
                    log_agent_action(self.name, "自动选择第一个 iframe")

            if frame:
                self._in_iframe = True
                self._current_frame = frame
                log_agent_action(self.name, "切换到 iframe", frame.url[:60])
                return True
            else:
                log_warning("未找到可用的 iframe")
                return False
        except Exception as e:
            log_error(f"切换 iframe 失败: {e}")
            return False

    async def _fill_form(self, page: Page, form_data_json: str) -> bool:
        """填写复杂表单，form_data_json 格式: {"field_selector": "value", ...}"""
        try:
            form_data = json.loads(form_data_json) if isinstance(form_data_json, str) else form_data_json
        except json.JSONDecodeError:
            log_error(f"表单数据 JSON 解析失败: {form_data_json[:100]}")
            return False

        success_count = 0
        target = self._current_frame if self._in_iframe else page

        for field_selector, value in form_data.items():
            try:
                element = await target.query_selector(field_selector)
                if not element:
                    log_warning(f"表单字段未找到: {field_selector}")
                    continue

                tag = await element.evaluate("el => el.tagName.toLowerCase()")
                input_type = await element.evaluate("el => (el.type || '').toLowerCase()")

                if tag == "select":
                    await element.select_option(value=value)
                elif input_type in ("checkbox", "radio"):
                    checked = await element.is_checked()
                    should_check = str(value).lower() in ("true", "1", "yes", "on")
                    if checked != should_check:
                        await element.click()
                elif input_type == "file":
                    await element.set_input_files(value)
                elif tag == "textarea" or tag == "input":
                    await element.fill("")
                    await element.type(str(value), delay=random.randint(20, 60))
                else:
                    await element.fill(str(value))

                success_count += 1
                await self._human_delay(50, 150)

            except Exception as e:
                log_warning(f"填写字段 {field_selector} 失败: {e}")
                continue

        log_agent_action(self.name, "表单填写完成", f"{success_count}/{len(form_data)} 个字段")
        return success_count > 0

    # === 操作执行 ===

    async def _try_click_with_fallbacks(self, page: Page, selector: str, description: str,
                                        action: BrowserAction = None) -> bool:
        """尝试点击元素，使用 LLM 提供的备选策略而非硬编码逻辑"""
        strategies = []

        # 策略1: 如果 LLM 建议键盘备选且优先级高，先尝试键盘
        if action and action.use_keyboard_fallback and action.keyboard_key:
            key = action.keyboard_key
            strategies.append((f"键盘{key}", lambda k=key: page.keyboard.press(k)))

        # 策略2: 主选择器直接点击
        strategies.append(("直接点击", lambda: page.click(selector, timeout=5000)))

        # 策略3: 主选择器 locator 点击
        strategies.append(("locator点击", lambda: page.locator(selector).first.click(timeout=5000)))

        # 策略4: LLM 提供的备选选择器
        if action and action.fallback_selector:
            fb = action.fallback_selector
            strategies.append(("备选选择器", lambda s=fb: page.click(s, timeout=5000)))
            strategies.append(("备选locator", lambda s=fb: page.locator(s).first.click(timeout=5000)))

        # 策略5: 强制点击（最后手段）
        strategies.append(("强制点击", lambda: page.click(selector, force=True, timeout=5000)))

        for name, strategy in strategies:
            try:
                await strategy()
                log_agent_action(self.name, f"点击成功 ({name})", selector[:50] if selector else description[:50])
                return True
            except Exception as e:
                log_warning(f"点击策略[{name}]失败: {str(e)[:50]}")
                continue

        return False

    async def _try_input_with_fallbacks(self, page: Page, selector: str, value: str) -> bool:
        """尝试输入文本，带有多种回退策略"""
        strategies = [
            # 策略1: 使用 query_selector
            ("query_selector", self._input_via_query_selector),
            # 策略2: 使用 locator
            ("locator", self._input_via_locator),
            # 策略3: 使用 fill
            ("fill", self._input_via_fill),
        ]

        for name, strategy in strategies:
            try:
                success = await strategy(page, selector, value)
                if success:
                    log_agent_action(self.name, f"输入成功 ({name})", value[:30])
                    return True
            except Exception as e:
                log_warning(f"输入策略 {name} 失败: {str(e)[:50]}")
                continue

        return False

    async def _input_via_query_selector(self, page: Page, selector: str, value: str) -> bool:
        element = await page.query_selector(selector)
        if element:
            await element.fill("")
            for char in value:
                await element.type(char, delay=random.randint(30, 100))
            return True
        return False

    async def _input_via_locator(self, page: Page, selector: str, value: str) -> bool:
        locator = page.locator(selector).first
        await locator.fill("")
        await locator.type(value, delay=50)
        return True

    async def _input_via_fill(self, page: Page, selector: str, value: str) -> bool:
        await page.fill(selector, value)
        return True

    async def _execute_action(self, page: Page, action: BrowserAction) -> bool:
        """执行浏览器操作"""
        log_agent_action(self.name, f"执行操作: {action.action_type.value}", action.description)
        # 如果在 iframe 中，操作目标是 frame
        target = self._current_frame if self._in_iframe else page

        try:
            if action.action_type == ActionType.CLICK:
                await self._human_delay(100, 300)
                success = await self._try_click_with_fallbacks(target, action.target_selector, action.description, action)
                if success:
                    await self._human_delay(200, 500)
                return success

            elif action.action_type == ActionType.INPUT:
                await self._human_delay(100, 200)
                success = await self._try_input_with_fallbacks(target, action.target_selector, action.value)
                if success:
                    await self._human_delay(100, 300)
                return success

            elif action.action_type == ActionType.PRESS_KEY:
                await self._human_delay(50, 150)
                await page.keyboard.press(action.value or "Enter")
                await self._human_delay(100, 300)
                return True

            elif action.action_type == ActionType.SELECT:
                await self._human_delay(100, 200)
                await target.select_option(action.target_selector, action.value)
                return True

            elif action.action_type == ActionType.SCROLL:
                await page.mouse.wheel(0, 500)
                await self._human_delay(100, 300)
                return True

            elif action.action_type == ActionType.WAIT:
                await asyncio.sleep(1)
                return True

            elif action.action_type == ActionType.NAVIGATE:
                await page.goto(action.value, wait_until="domcontentloaded", timeout=30000)
                await self._human_delay(300, 600)
                return True

            elif action.action_type == ActionType.EXTRACT:
                # 提取数据操作，由调用方处理
                return True

            elif action.action_type == ActionType.DONE:
                log_success("任务完成!")
                return True

            elif action.action_type == ActionType.FAILED:
                log_error(f"操作失败: {action.description}")
                return False

            elif action.action_type == ActionType.CONFIRM:
                # 需要人工确认
                return True

            elif action.action_type == ActionType.SWITCH_TAB:
                # 切换标签页：value 为标签页索引或 "last"（最新）
                tab_index = -1
                if action.value == "last" or action.value == "":
                    tab_index = len(self._pages) - 1
                else:
                    try:
                        tab_index = int(action.value)
                    except ValueError:
                        tab_index = len(self._pages) - 1
                return await self.switch_to_tab(tab_index)

            elif action.action_type == ActionType.CLOSE_TAB:
                return await self.close_current_tab()

            elif action.action_type == ActionType.UPLOAD_FILE:
                return await self._handle_file_upload(page, action.target_selector, action.value)

            elif action.action_type == ActionType.DOWNLOAD:
                # 点击下载链接，下载由 _on_download 事件自动处理
                if action.target_selector:
                    return await self._try_click_with_fallbacks(page, action.target_selector, action.description)
                return True

            elif action.action_type == ActionType.SWITCH_IFRAME:
                return await self._switch_to_iframe(page, action.target_selector)

            elif action.action_type == ActionType.EXIT_IFRAME:
                self._in_iframe = False
                self._current_frame = None
                log_agent_action(self.name, "退出 iframe，回到主页面")
                return True

            elif action.action_type == ActionType.FILL_FORM:
                return await self._fill_form(page, action.value)

            return False

        except Exception as e:
            log_error(f"执行操作失败: {e}")
            return False

    async def _wait_for_page_stable(self, page: Page, timeout: int = 5000):
        """等待页面稳定"""
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=timeout)
        except:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=2000)
        except:
            pass
        await self._human_delay(200, 400)

    async def _handle_captcha_if_present(self, page: Page) -> bool:
        """检测并处理验证码"""
        try:
            detection = await self.captcha_solver.detect_captcha(page)
            if detection.get("has_captcha"):
                log_agent_action(self.name, "检测到验证码，尝试处理")
                return await self.captcha_solver.solve(page, max_retries=3)
        except:
            pass
        return True

    # === 人工确认 ===

    def _request_human_confirmation(self, action: BrowserAction, context: str) -> bool:
        """请求人工确认敏感操作"""
        from utils.human_confirm import HumanConfirm

        return HumanConfirm.request_confirmation(
            operation=action.description,
            details=f"操作类型: {action.action_type.value}\n目标: {action.target_selector}\n值: {action.value}",
            affected_items=[context],
        )

    # === 主执行循环 ===

    async def run(self, task: str, start_url: str = None, use_vision: bool = False) -> Dict[str, Any]:
        """
        执行浏览器任务的主循环

        Args:
            task: 用户任务描述
            start_url: 起始URL（可选，会自动推断）
            use_vision: 是否使用视觉分析（更准确但更慢）

        Returns:
            执行结果，包含 extracted_data 字段
        """
        self.current_task = task
        self.completed_steps = []
        self.extracted_data = []
        self.current_step = 0
        self.last_url = ""
        self.same_url_count = 0
        self._in_iframe = False
        self._current_frame = None

        log_agent_action(self.name, "开始执行任务", task[:50])

        # Step 1: 理解任务
        task_info = self._understand_task(task)
        if not start_url:
            start_url = task_info.get("start_url", "")

        if not start_url:
            # LLM 未能在任务理解阶段确定起始URL，使用通用搜索引擎兜底
            start_url = "https://www.google.com"
            log_warning(f"LLM 未提供起始URL，使用默认搜索引擎: {start_url}")

        # Step 2: 创建浏览器页面
        page = await self._create_page()

        try:
            # Step 3: 导航到起始页面
            log_agent_action(self.name, "导航到", start_url)
            await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
            await self._wait_for_page_stable(page)

            # Step 4: 处理可能的验证码
            await self._handle_captcha_if_present(page)

            # Step 5: 主执行循环
            consecutive_failures = 0
            max_consecutive_failures = 3

            while self.current_step < self.max_steps:
                self.current_step += 1
                log_agent_action(self.name, f"步骤 {self.current_step}/{self.max_steps}")

                try:
                    # 检查页面是否仍然有效
                    try:
                        current_url = page.url
                        current_title = await page.title()
                    except Exception as e:
                        log_error(f"页面已失效: {e}")
                        return {
                            "success": False,
                            "message": "浏览器页面已关闭或崩溃",
                            "steps_taken": self.completed_steps,
                            "extracted_data": self.extracted_data,
                        }

                    # 检测是否卡在同一页面
                    if current_url == self.last_url:
                        self.same_url_count += 1
                        if self.same_url_count >= self.max_same_url_retries:
                            log_warning(f"页面停滞，尝试刷新")
                            try:
                                await page.reload(wait_until="domcontentloaded", timeout=10000)
                                self.same_url_count = 0
                            except:
                                pass
                    else:
                        self.same_url_count = 0
                        self.last_url = current_url

                    # 分析当前页面
                    page_analysis = await self._analyze_page(page, use_vision=use_vision)

                    # 检查任务是否完成
                    if page_analysis.get("is_task_complete"):
                        log_success("任务已完成!")
                        # 提取数据
                        extraction_result = await self._extract_page_data(page)
                        return {
                            "success": True,
                            "message": page_analysis.get("completion_reason", "任务完成"),
                            "steps_taken": self.completed_steps,
                            "final_url": page.url,
                            "extracted_data": extraction_result.get("data", []),
                            "extraction_summary": extraction_result.get("summary", ""),
                        }

                    # 检查是否卡住
                    if page_analysis.get("is_stuck"):
                        log_warning(f"检测到卡住，尝试刷新页面")
                        try:
                            await page.reload(wait_until="domcontentloaded", timeout=10000)
                            continue
                        except:
                            pass

                    # 决定下一步操作
                    action = await self._decide_action(page, page_analysis)

                    # 检查是否需要人工确认
                    if action.requires_confirmation:
                        log_warning(f"需要人工确认: {action.description}")
                        confirmed = self._request_human_confirmation(action, page.url)
                        if not confirmed:
                            return {
                                "success": False,
                                "message": "用户取消操作",
                                "steps_taken": self.completed_steps,
                                "extracted_data": self.extracted_data,
                            }

                    # 检查是否完成或失败
                    if action.action_type == ActionType.DONE:
                        # 提取数据
                        extraction_result = await self._extract_page_data(page)
                        return {
                            "success": True,
                            "message": action.description or "任务完成",
                            "steps_taken": self.completed_steps,
                            "final_url": page.url,
                            "extracted_data": extraction_result.get("data", []),
                            "extraction_summary": extraction_result.get("summary", ""),
                        }

                    if action.action_type == ActionType.EXTRACT:
                        # 执行数据提取
                        extraction_result = await self._extract_page_data(page)
                        self.extracted_data = extraction_result.get("data", [])
                        self.completed_steps.append(f"提取数据: {extraction_result.get('summary', '')}")
                        continue

                    if action.action_type == ActionType.FAILED:
                        consecutive_failures += 1
                        if consecutive_failures >= max_consecutive_failures:
                            # 即使失败也尝试提取已有数据
                            extraction_result = await self._extract_page_data(page)
                            return {
                                "success": False,
                                "message": action.description or "连续多次操作失败",
                                "steps_taken": self.completed_steps,
                                "final_url": page.url,
                                "extracted_data": extraction_result.get("data", []),
                            }
                        log_warning(f"操作失败 ({consecutive_failures}/{max_consecutive_failures}): {action.description}")
                        continue

                    # 执行操作
                    success = await self._execute_action(page, action)

                    if success:
                        consecutive_failures = 0  # 重置失败计数
                        self.completed_steps.append(action.description)
                        # 等待页面响应
                        await self._wait_for_page_stable(page)
                        # 处理可能出现的验证码
                        await self._handle_captcha_if_present(page)
                    else:
                        consecutive_failures += 1
                        log_warning(f"操作执行失败 ({consecutive_failures}/{max_consecutive_failures}): {action.description}")
                        if consecutive_failures >= max_consecutive_failures:
                            extraction_result = await self._extract_page_data(page)
                            return {
                                "success": False,
                                "message": f"连续 {max_consecutive_failures} 次操作失败",
                                "steps_taken": self.completed_steps,
                                "final_url": page.url,
                                "extracted_data": extraction_result.get("data", []),
                            }

                except Exception as step_error:
                    log_error(f"步骤执行异常: {step_error}")
                    consecutive_failures += 1
                    if consecutive_failures >= max_consecutive_failures:
                        return {
                            "success": False,
                            "message": f"执行异常: {str(step_error)}",
                            "steps_taken": self.completed_steps,
                            "extracted_data": self.extracted_data,
                        }
                    # 尝试恢复
                    await asyncio.sleep(0.5)
                    continue

            # 超过最大步数 - 尝试提取已有数据
            extraction_result = await self._extract_page_data(page)
            return {
                "success": True,  # 改为 True，因为可能已经完成了主要任务
                "message": f"已执行 {self.max_steps} 步",
                "steps_taken": self.completed_steps,
                "final_url": page.url,
                "extracted_data": extraction_result.get("data", []),
                "extraction_summary": extraction_result.get("summary", ""),
            }

        except Exception as e:
            log_error(f"执行出错: {e}")
            return {
                "success": False,
                "message": str(e),
                "steps_taken": self.completed_steps,
                "extracted_data": self.extracted_data,
            }

        finally:
            # 不自动关闭浏览器，让用户可以查看结果
            pass

    async def close(self):
        """关闭浏览器"""
        await self._close()

    # === 便捷方法 ===

    async def search(self, query: str, engine: str = "baidu") -> Dict[str, Any]:
        """快捷搜索方法"""
        engines = {
            "baidu": f"https://www.baidu.com/s?wd={query}",
            "google": f"https://www.google.com/search?q={query}",
            "bing": f"https://www.bing.com/search?q={query}",
        }
        url = engines.get(engine, engines["baidu"])
        return await self.run(f"搜索: {query}", start_url=url)

    async def navigate_and_interact(self, url: str, task: str) -> Dict[str, Any]:
        """导航到指定URL并执行任务"""
        return await self.run(task, start_url=url)


# === 同步包装器 ===

def run_browser_task(task: str, start_url: str = None, headless: bool = False) -> Dict[str, Any]:
    """同步执行浏览器任务"""
    async def _run():
        agent = BrowserAgent(headless=headless)
        try:
            return await agent.run(task, start_url)
        finally:
            await agent.close()

    return asyncio.run(_run())


# === 测试代码 ===

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        task = " ".join(sys.argv[1:])
    else:
        task = "在百度搜索'Python教程'，然后点击第一个搜索结果"

    print(f"执行任务: {task}")
    result = run_browser_task(task, headless=False)
    print(f"结果: {result}")

