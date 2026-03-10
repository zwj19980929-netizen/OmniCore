"""
BrowserToolkit — 统一的原子浏览器操作工具箱
所有浏览器操作的唯一入口，Agent 层不再直接碰 Playwright。
每个方法返回 ToolkitResult，绝不抛异常。
"""
import asyncio
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
    Frame,
)

from config.settings import settings
from utils.browser_runtime_pool import (
    BrowserLease,
    BrowserPoolCircuitOpenError,
    BrowserPoolLaunchError,
    get_browser_runtime_pool,
)
from utils.logger import log_agent_action, log_debug_metrics, log_error, log_warning


@dataclass
class ToolkitResult:
    """每个原子操作的统一返回类型"""
    success: bool
    data: Any = None
    error: Optional[str] = None


class BrowserToolkit:
    """
    原子浏览器操作工具箱。
    合并 browser_agent + web_worker 的反检测逻辑，统一管理生命周期和 iframe 状态。
    支持 async with 上下文管理器，确保资源正确释放。
    """

    def __init__(
        self,
        headless: Optional[bool] = None,
        fast_mode: Optional[bool] = None,
        block_heavy_resources: Optional[bool] = None,
        user_data_dir: Optional[str] = None,
    ):
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._browser_lease: Optional[BrowserLease] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._current_frame: Optional[Frame] = None
        self._in_iframe: bool = False

        self.fast_mode = fast_mode if fast_mode is not None else settings.BROWSER_FAST_MODE
        self.headless = headless if headless is not None else self.fast_mode
        self.block_heavy_resources = (
            block_heavy_resources if block_heavy_resources is not None
            else settings.BLOCK_HEAVY_RESOURCES
        )
        self.user_data_dir = user_data_dir
        self._captcha_solver = None  # lazy

    # ── context manager support ──────────────────────────────

    async def __aenter__(self):
        """异步上下文管理器入口"""
        await self.launch()
        await self.create_page()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器退出，确保资源释放"""
        await self.close()
        return False  # 不抑制异常

    # ── helpers ──────────────────────────────────────────────

    def _timeout(self, ms: int = 8000) -> int:
        return max(ms // 3, 2000) if self.fast_mode else ms

    async def _safe(self, coro, default=None):
        """Run a coroutine, swallow exceptions, return default on failure."""
        try:
            return await coro
        except Exception:
            return default

    # ── lifecycle ────────────────────────────────────────────

    async def launch(self) -> ToolkitResult:
        """启动浏览器（合并两处反检测参数）"""
        try:
            if self._browser and self._browser.is_connected():
                return ToolkitResult(success=True)
            await self.close()
            if settings.BROWSER_POOL_ENABLED:
                pool = get_browser_runtime_pool()
                try:
                    lease = await pool.acquire_browser(
                        headless=self.headless,
                    )
                except (BrowserPoolCircuitOpenError, BrowserPoolLaunchError) as exc:
                    log_warning(f"Browser pool bypassed, falling back to direct launch: {exc}")
                else:
                    self._browser_lease = lease
                    self._browser = lease.browser
                    self._playwright = None
                    log_debug_metrics("browser_pool.acquire", pool.snapshot_stats())
                    return ToolkitResult(success=True)
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self.headless,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-first-run",
                    "--disable-infobars",
                    "--no-sandbox",
                    "--window-size=1366,768",
                ],
                ignore_default_args=["--enable-automation"],
            )
            return ToolkitResult(success=True)
        except Exception as e:
            cleanup_result = await self.close()
            if not cleanup_result.success:
                log_warning(
                    f"BrowserToolkit launch cleanup failed: {cleanup_result.error}"
                )
            return ToolkitResult(success=False, error=str(e))

    async def create_page(self) -> ToolkitResult:
        """创建带反检测的页面（合并两处 stealth 逻辑）"""
        try:
            r = await self.launch()
            if not r.success:
                return r

            import os
            context_kwargs = {
                "viewport": {"width": 1366, "height": 768},
                "screen": {"width": 1366, "height": 768},
                "device_scale_factor": 1.0,
                "is_mobile": False,
                "has_touch": False,
                "locale": "zh-CN",
                "timezone_id": "Asia/Shanghai",
                "color_scheme": "light",
                "reduced_motion": "no-preference",
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                "extra_http_headers": {
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Upgrade-Insecure-Requests": "1",
                },
            }

            storage_path = self._get_storage_state_path()
            if storage_path and os.path.exists(storage_path):
                context_kwargs["storage_state"] = storage_path

            self._context = await self._browser.new_context(**context_kwargs)

            if self.block_heavy_resources:
                async def _route_handler(route):
                    try:
                        if route.request.resource_type in {"image", "font", "media"}:
                            await route.abort()
                        else:
                            await route.continue_()
                    except Exception:
                        try:
                            await route.continue_()
                        except Exception:
                            pass

                await self._context.route("**/*", _route_handler)

            self._page = await self._context.new_page()

            # 注入反检测脚本（合并两处最优）
            await self._page.add_init_script("""
                (() => {
                    const overrideGetter = (obj, key, value) => {
                        try {
                            Object.defineProperty(obj, key, {
                                get: () => value,
                                configurable: true,
                            });
                        } catch (e) {}
                    };

                    overrideGetter(navigator, 'webdriver', undefined);
                    overrideGetter(navigator, 'plugins', [1, 2, 3, 4, 5]);
                    overrideGetter(navigator, 'languages', ['zh-CN', 'zh', 'en']);
                    overrideGetter(navigator, 'platform', 'Win32');
                    overrideGetter(navigator, 'vendor', 'Google Inc.');
                    overrideGetter(navigator, 'hardwareConcurrency', 8);
                    overrideGetter(navigator, 'deviceMemory', 8);
                    overrideGetter(navigator, 'maxTouchPoints', 0);
                    overrideGetter(screen, 'colorDepth', 24);
                    overrideGetter(window, 'outerWidth', 1366);
                    overrideGetter(window, 'outerHeight', 768);

                    window.chrome = window.chrome || {};
                    window.chrome.runtime = window.chrome.runtime || {};
                    window.chrome.app = window.chrome.app || {
                        InstallState: 'installed',
                        RunningState: 'running',
                        getDetails: () => null,
                        getIsInstalled: () => false,
                    };
                    window.chrome.csi = window.chrome.csi || (() => ({ onloadT: Date.now(), startE: Date.now() - 150 }));
                    window.chrome.loadTimes = window.chrome.loadTimes || (() => ({
                        commitLoadTime: Date.now() / 1000,
                        finishDocumentLoadTime: Date.now() / 1000,
                        finishLoadTime: Date.now() / 1000,
                        firstPaintAfterLoadTime: 0,
                        firstPaintTime: Date.now() / 1000,
                        navigationType: 'Other',
                        npnNegotiatedProtocol: 'h2',
                        requestTime: (Date.now() - 1200) / 1000,
                        startLoadTime: (Date.now() - 1000) / 1000,
                        wasAlternateProtocolAvailable: false,
                        wasFetchedViaSpdy: true,
                        wasNpnNegotiated: true,
                    }));

                    if (navigator.permissions && navigator.permissions.query) {
                        const originalQuery = navigator.permissions.query.bind(navigator.permissions);
                        navigator.permissions.query = (parameters) => (
                            parameters && parameters.name === 'notifications'
                                ? Promise.resolve({ state: Notification.permission })
                                : originalQuery(parameters)
                        );
                    }

                    const uaData = {
                        brands: [
                            { brand: 'Chromium', version: '122' },
                            { brand: 'Google Chrome', version: '122' },
                            { brand: 'Not A;Brand', version: '99' },
                        ],
                        mobile: false,
                        platform: 'Windows',
                        getHighEntropyValues: async (hints) => {
                            const supported = {
                                architecture: 'x86',
                                bitness: '64',
                                model: '',
                                platform: 'Windows',
                                platformVersion: '10.0.0',
                                uaFullVersion: '122.0.0.0',
                                fullVersionList: [
                                    { brand: 'Chromium', version: '122.0.0.0' },
                                    { brand: 'Google Chrome', version: '122.0.0.0' },
                                    { brand: 'Not A;Brand', version: '99.0.0.0' },
                                ],
                            };
                            const out = {};
                            for (const hint of (hints || [])) {
                                if (hint in supported) out[hint] = supported[hint];
                            }
                            return out;
                        },
                        toJSON: function() {
                            return {
                                brands: this.brands,
                                mobile: this.mobile,
                                platform: this.platform,
                            };
                        },
                    };
                    overrideGetter(navigator, 'userAgentData', uaData);

                    const originalGetParameter = WebGLRenderingContext.prototype.getParameter;
                    WebGLRenderingContext.prototype.getParameter = function(parameter) {
                        if (parameter === 37445) return 'Intel Inc.';
                        if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                        return originalGetParameter.call(this, parameter);
                    };
                    if (window.WebGL2RenderingContext) {
                        const originalGetParameter2 = WebGL2RenderingContext.prototype.getParameter;
                        WebGL2RenderingContext.prototype.getParameter = function(parameter) {
                            if (parameter === 37445) return 'Intel Inc.';
                            if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                            return originalGetParameter2.call(this, parameter);
                        };
                    }
                })();
            """)

            self._current_frame = None
            self._in_iframe = False
            return ToolkitResult(success=True, data=self._page)
        except Exception as e:
            cleanup_result = await self.close()
            if not cleanup_result.success:
                log_warning(
                    f"BrowserToolkit create_page cleanup failed: {cleanup_result.error}"
                )
            return ToolkitResult(success=False, error=str(e))

    async def close(self) -> ToolkitResult:
        """关闭所有资源，保存 storage state"""
        try:
            lease = self._browser_lease
            self._browser_lease = None
            if self._context:
                storage_path = self._get_storage_state_path()
                if storage_path:
                    try:
                        import os
                        os.makedirs(os.path.dirname(storage_path), exist_ok=True)
                        await self._context.storage_state(path=storage_path)
                    except Exception:
                        pass
            if self._page and not self._page.is_closed():
                await self._page.close()
            self._page = None
            if self._context:
                await self._context.close()
            self._context = None
            self._current_frame = None
            self._in_iframe = False
            if lease is not None:
                await lease.release()
                log_debug_metrics(
                    "browser_pool.release",
                    get_browser_runtime_pool().snapshot_stats(),
                )
                self._browser = None
                self._playwright = None
                return ToolkitResult(success=True)
            if self._browser:
                await self._browser.close()
            self._browser = None
            if self._playwright:
                await self._playwright.stop()
            self._playwright = None
            return ToolkitResult(success=True)
        except Exception as e:
            if 'lease' in locals() and lease is not None:
                try:
                    await lease.release()
                except Exception:
                    pass
            self._browser_lease = None
            self._page = None
            self._context = None
            self._browser = None
            self._playwright = None
            return ToolkitResult(success=False, error=str(e))

    def _get_storage_state_path(self) -> Optional[str]:
        if not self.user_data_dir:
            return None
        import os
        return os.path.join(self.user_data_dir, "storage_state.json")

    @property
    def page(self) -> Optional[Page]:
        return self._page

    @property
    def context(self) -> Optional[BrowserContext]:
        return self._context

    @property
    def active_surface(self) -> Any:
        """返回当前操作面：iframe 或 page"""
        if self._in_iframe and self._current_frame is not None:
            return self._current_frame
        return self._page

    # ── navigation ────────────────────────────────────────────

    async def goto(self, url: str, wait_until: str = "domcontentloaded", timeout: int = None) -> ToolkitResult:
        try:
            timeout_ms = timeout if timeout is not None else settings.BROWSER_NAVIGATION_TIMEOUT
            await self._page.goto(url, wait_until=wait_until, timeout=self._timeout(timeout_ms))
            return ToolkitResult(success=True, data=self._page.url)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def go_back(self) -> ToolkitResult:
        try:
            await self._page.go_back(timeout=self._timeout(settings.BROWSER_NAVIGATION_TIMEOUT))
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def go_forward(self) -> ToolkitResult:
        try:
            await self._page.go_forward(timeout=self._timeout(settings.BROWSER_NAVIGATION_TIMEOUT))
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def refresh(self) -> ToolkitResult:
        try:
            await self._page.reload(timeout=self._timeout(settings.BROWSER_NAVIGATION_TIMEOUT))
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def wait_for_load(self, state: str = "domcontentloaded", timeout: int = None) -> ToolkitResult:
        try:
            timeout_ms = timeout if timeout is not None else settings.BROWSER_LOAD_TIMEOUT
            target = self.active_surface or self._page
            await target.wait_for_load_state(state, timeout=self._timeout(timeout_ms))
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def wait_for_selector(self, selector: str, timeout: int = None) -> ToolkitResult:
        try:
            timeout_ms = timeout if timeout is not None else settings.BROWSER_SELECTOR_TIMEOUT
            target = self.active_surface or self._page
            elem = await target.wait_for_selector(selector, timeout=self._timeout(timeout_ms))
            return ToolkitResult(success=True, data=elem)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    # ── interaction ──────────────────────────────────────────

    async def click(self, selector: str, timeout: int = None) -> ToolkitResult:
        try:
            timeout_ms = timeout if timeout is not None else settings.BROWSER_ACTION_TIMEOUT
            surface = self.active_surface
            await surface.click(selector, timeout=self._timeout(timeout_ms))
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def double_click(self, selector: str, timeout: int = None) -> ToolkitResult:
        try:
            timeout_ms = timeout if timeout is not None else settings.BROWSER_ACTION_TIMEOUT
            surface = self.active_surface
            await surface.dblclick(selector, timeout=self._timeout(timeout_ms))
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def right_click(self, selector: str, timeout: int = None) -> ToolkitResult:
        try:
            timeout_ms = timeout if timeout is not None else settings.BROWSER_ACTION_TIMEOUT
            surface = self.active_surface
            await surface.click(selector, button="right", timeout=self._timeout(timeout_ms))
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def hover(self, selector: str, timeout: int = None) -> ToolkitResult:
        try:
            timeout_ms = timeout if timeout is not None else settings.BROWSER_ACTION_TIMEOUT
            surface = self.active_surface
            await surface.hover(selector, timeout=self._timeout(timeout_ms))
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def input_text(self, selector: str, text: str, timeout: int = None) -> ToolkitResult:
        """fill — 直接设置值（快速）"""
        try:
            timeout_ms = timeout if timeout is not None else settings.BROWSER_ACTION_TIMEOUT
            surface = self.active_surface
            await surface.locator(selector).first.fill(text, timeout=self._timeout(timeout_ms))
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def type_text(self, selector: str, text: str, delay: int = 20, timeout: int = None) -> ToolkitResult:
        """type — 逐字符输入（模拟人类）"""
        try:
            timeout_ms = timeout if timeout is not None else settings.BROWSER_ACTION_TIMEOUT
            surface = self.active_surface
            await surface.locator(selector).first.type(text, delay=delay, timeout=self._timeout(timeout_ms))
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def clear_input(self, selector: str, timeout: int = None) -> ToolkitResult:
        try:
            timeout_ms = timeout if timeout is not None else settings.BROWSER_ACTION_TIMEOUT
            surface = self.active_surface
            await surface.locator(selector).first.fill("", timeout=self._timeout(timeout_ms))
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def select_option(self, selector: str, value: str, timeout: int = None) -> ToolkitResult:
        try:
            timeout_ms = timeout if timeout is not None else settings.BROWSER_ACTION_TIMEOUT
            surface = self.active_surface
            await surface.locator(selector).first.select_option(value=value, timeout=self._timeout(timeout_ms))
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def check_checkbox(self, selector: str, timeout: int = None) -> ToolkitResult:
        try:
            timeout_ms = timeout if timeout is not None else settings.BROWSER_ACTION_TIMEOUT
            surface = self.active_surface
            await surface.locator(selector).first.check(timeout=self._timeout(timeout_ms))
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def upload_file(self, selector: str, path: str, timeout: int = None) -> ToolkitResult:
        try:
            timeout_ms = timeout if timeout is not None else settings.BROWSER_ACTION_TIMEOUT
            surface = self.active_surface
            await surface.locator(selector).first.set_input_files(path, timeout=self._timeout(timeout_ms))
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def force_click(self, selector: str, timeout: int = None) -> ToolkitResult:
        try:
            timeout_ms = timeout if timeout is not None else settings.BROWSER_ACTION_TIMEOUT
            surface = self.active_surface
            await surface.locator(selector).first.click(timeout=self._timeout(timeout_ms), force=True)
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def locator_click(self, selector: str, timeout: int = None) -> ToolkitResult:
        try:
            timeout_ms = timeout if timeout is not None else settings.BROWSER_ACTION_TIMEOUT
            surface = self.active_surface
            await surface.locator(selector).first.click(timeout=self._timeout(timeout_ms))
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    # ── scroll ───────────────────────────────────────────────

    async def scroll_down(self, pixels: int = 800) -> ToolkitResult:
        try:
            await self._page.mouse.wheel(0, pixels)
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def scroll_up(self, pixels: int = 800) -> ToolkitResult:
        try:
            await self._page.mouse.wheel(0, -pixels)
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def scroll_to_bottom(self) -> ToolkitResult:
        try:
            await self._page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def scroll_to_element(self, selector: str) -> ToolkitResult:
        try:
            surface = self.active_surface
            await surface.locator(selector).first.scroll_into_view_if_needed()
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    # ── keyboard ──────────────────────────────────────────────

    async def press_key(self, key: str) -> ToolkitResult:
        try:
            await self._page.keyboard.press(key)
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def hotkey(self, *keys: str) -> ToolkitResult:
        """按组合键，如 hotkey('Control', 'a')"""
        try:
            combo = "+".join(keys)
            await self._page.keyboard.press(combo)
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    # ── extraction ───────────────────────────────────────────

    async def get_text(self, selector: str) -> ToolkitResult:
        try:
            surface = self.active_surface
            text = await surface.locator(selector).first.inner_text()
            return ToolkitResult(success=True, data=text.strip())
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def get_attribute(self, selector: str, attr: str) -> ToolkitResult:
        try:
            surface = self.active_surface
            val = await surface.locator(selector).first.get_attribute(attr)
            return ToolkitResult(success=True, data=val)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def get_all_texts(self, selector: str) -> ToolkitResult:
        try:
            surface = self.active_surface
            elements = await surface.locator(selector).all()
            texts = []
            for el in elements:
                texts.append((await el.inner_text()).strip())
            return ToolkitResult(success=True, data=texts)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def query_all(self, selector: str) -> ToolkitResult:
        """返回匹配选择器的所有 ElementHandle 列表"""
        try:
            surface = self.active_surface
            elements = await surface.query_selector_all(selector)
            return ToolkitResult(success=True, data=elements)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def xpath_query(self, xpath: str) -> ToolkitResult:
        try:
            surface = self.active_surface
            elem = await surface.locator(f"xpath={xpath}").first.element_handle()
            return ToolkitResult(success=True, data=elem)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def xpath_query_all(self, xpath: str) -> ToolkitResult:
        try:
            surface = self.active_surface
            elements = await surface.locator(f"xpath={xpath}").all()
            return ToolkitResult(success=True, data=elements)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def extract_table(self, selector: str) -> ToolkitResult:
        """提取表格数据为 list of dicts"""
        try:
            surface = self.active_surface
            data = await surface.evaluate("""(sel) => {
                const table = document.querySelector(sel);
                if (!table) return [];
                const headers = Array.from(table.querySelectorAll('thead th, tr:first-child th, tr:first-child td'))
                    .map(th => th.innerText.trim());
                const rows = Array.from(table.querySelectorAll('tbody tr, tr:not(:first-child)'));
                return rows.map(row => {
                    const cells = Array.from(row.querySelectorAll('td'));
                    const obj = {};
                    cells.forEach((cell, i) => {
                        const key = headers[i] || ('col_' + i);
                        obj[key] = cell.innerText.trim();
                    });
                    return obj;
                });
            }""", selector)
            return ToolkitResult(success=True, data=data)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def extract_links(self, selector: str = "a[href]") -> ToolkitResult:
        """提取链接列表 [{text, href}]"""
        try:
            surface = self.active_surface
            data = await surface.evaluate("""(sel) => {
                return Array.from(document.querySelectorAll(sel))
                    .map(a => ({
                        text: (a.innerText || '').trim(),
                        href: a.href || a.getAttribute('href') || '',
                    }))
                    .filter(item => item.href);
            }""", selector)
            return ToolkitResult(success=True, data=data)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def get_page_html(self) -> ToolkitResult:
        try:
            surface = self.active_surface
            html = await surface.content()
            return ToolkitResult(success=True, data=html)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def get_page_text(self) -> ToolkitResult:
        try:
            surface = self.active_surface
            text = await surface.evaluate("() => document.body.innerText")
            return ToolkitResult(success=True, data=text)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def evaluate_js(self, script: str, arg: Any = None) -> ToolkitResult:
        try:
            surface = self.active_surface
            if arg is not None:
                result = await surface.evaluate(script, arg)
            else:
                result = await surface.evaluate(script)
            return ToolkitResult(success=True, data=result)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def get_input_value(self, selector: str) -> ToolkitResult:
        """读取输入框当前值"""
        try:
            surface = self.active_surface
            locator = surface.locator(selector).first
            try:
                val = await locator.input_value()
            except Exception:
                val = await locator.evaluate(
                    "el => typeof el.value === 'string' ? el.value : (el.textContent || '').trim()"
                )
            return ToolkitResult(success=True, data=str(val or ""))
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    # ── perception ───────────────────────────────────────────

    async def screenshot(self, full_page: bool = False) -> ToolkitResult:
        try:
            data = await self._page.screenshot(full_page=full_page)
            return ToolkitResult(success=True, data=data)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def screenshot_element(self, selector: str) -> ToolkitResult:
        try:
            surface = self.active_surface
            elem = await surface.query_selector(selector)
            if not elem:
                return ToolkitResult(success=False, error="element not found")
            data = await elem.screenshot()
            return ToolkitResult(success=True, data=data)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def get_current_url(self) -> ToolkitResult:
        try:
            return ToolkitResult(success=True, data=self._page.url)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def get_title(self) -> ToolkitResult:
        try:
            title = await self._page.title()
            return ToolkitResult(success=True, data=title)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def is_visible(self, selector: str) -> ToolkitResult:
        try:
            surface = self.active_surface
            visible = await surface.locator(selector).first.is_visible()
            return ToolkitResult(success=True, data=visible)
        except Exception as e:
            return ToolkitResult(success=False, data=False, error=str(e))

    async def is_enabled(self, selector: str) -> ToolkitResult:
        try:
            surface = self.active_surface
            enabled = await surface.locator(selector).first.is_enabled()
            return ToolkitResult(success=True, data=enabled)
        except Exception as e:
            return ToolkitResult(success=False, data=False, error=str(e))

    async def element_exists(self, selector: str) -> ToolkitResult:
        try:
            surface = self.active_surface
            count = await surface.locator(selector).count()
            return ToolkitResult(success=True, data=count > 0)
        except Exception as e:
            return ToolkitResult(success=False, data=False, error=str(e))

    async def get_bounding_box(self, selector: str) -> ToolkitResult:
        try:
            surface = self.active_surface
            elem = await surface.query_selector(selector)
            if not elem:
                return ToolkitResult(success=False, error="element not found")
            box = await elem.bounding_box()
            return ToolkitResult(success=True, data=box)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def get_page_dimensions(self) -> ToolkitResult:
        try:
            dims = await self._page.evaluate("""() => ({
                width: document.documentElement.scrollWidth,
                height: document.documentElement.scrollHeight,
                viewportWidth: window.innerWidth,
                viewportHeight: window.innerHeight,
            })""")
            return ToolkitResult(success=True, data=dims)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    # ── iframe / tab ─────────────────────────────────────────

    async def switch_to_iframe(self, selector: str = "") -> ToolkitResult:
        try:
            frame = None
            if selector:
                frame_handle = await self._page.locator(selector).first.element_handle()
                if frame_handle:
                    frame = await frame_handle.content_frame()
            if frame is None:
                child_frames = [f for f in self._page.frames if f != self._page.main_frame]
                if child_frames:
                    frame = child_frames[0]
            if frame is None:
                return ToolkitResult(success=False, error="no iframe found")
            self._current_frame = frame
            self._in_iframe = True
            await self._safe(frame.wait_for_load_state("domcontentloaded", timeout=3000))
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def exit_iframe(self) -> ToolkitResult:
        self._current_frame = None
        self._in_iframe = False
        return ToolkitResult(success=True)

    async def switch_tab(self, index: int = -1) -> ToolkitResult:
        try:
            if not self._context or not self._context.pages:
                return ToolkitResult(success=False, error="no tabs")
            candidates = [p for p in self._context.pages if not p.is_closed()]
            if not candidates:
                return ToolkitResult(success=False, error="no open tabs")
            if index == -1:
                target = candidates[-1]
            elif 0 <= index < len(candidates):
                target = candidates[index]
            else:
                return ToolkitResult(success=False, error=f"tab index {index} out of range")
            self._page = target
            self._current_frame = None
            self._in_iframe = False
            await self._page.bring_to_front()
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def close_tab(self) -> ToolkitResult:
        try:
            if self._page:
                await self._page.close()
            if self._context:
                candidates = [p for p in self._context.pages if not p.is_closed()]
            else:
                candidates = []
            if candidates:
                self._page = candidates[-1]
                self._current_frame = None
                self._in_iframe = False
                await self._page.bring_to_front()
                return ToolkitResult(success=True)
            self._page = None
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def new_tab(self, url: str = "") -> ToolkitResult:
        try:
            if not self._context:
                return ToolkitResult(success=False, error="no browser context")
            page = await self._context.new_page()
            self._page = page
            self._current_frame = None
            self._in_iframe = False
            if url:
                await page.goto(url, wait_until="domcontentloaded", timeout=self._timeout(30000))
            return ToolkitResult(success=True, data=page)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    # ── mouse coordinates ─────────────────────────────────────

    async def mouse_click_at(self, x: float, y: float) -> ToolkitResult:
        try:
            await self._page.mouse.click(x, y)
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def mouse_down_at(self, x: float, y: float) -> ToolkitResult:
        try:
            await self._page.mouse.move(x, y)
            await self._page.mouse.down()
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def mouse_move_to(self, x: float, y: float) -> ToolkitResult:
        try:
            await self._page.mouse.move(x, y)
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def mouse_up(self) -> ToolkitResult:
        try:
            await self._page.mouse.up()
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    # ── anti-detection helpers ───────────────────────────────

    async def human_delay(self, min_ms: int = 300, max_ms: int = 800) -> ToolkitResult:
        """模拟人类操作延迟，fast_mode 下大幅缩短"""
        try:
            if self.fast_mode:
                min_ms = max(min_ms // 5, 20)
                max_ms = max(max_ms // 5, 60)
            delay = random.randint(min_ms, max_ms) / 1000
            await asyncio.sleep(delay)
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def random_mouse_move(self) -> ToolkitResult:
        """随机移动鼠标，模拟人类行为"""
        try:
            dims = await self._page.evaluate(
                "() => ({w: window.innerWidth, h: window.innerHeight})"
            )
            x = random.randint(100, dims["w"] - 100)
            y = random.randint(100, dims["h"] - 100)
            await self._page.mouse.move(x, y)
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    # ── captcha facade ───────────────────────────────────────

    @property
    def captcha_solver(self):
        """懒加载 CaptchaSolver，避免循环导入"""
        if self._captcha_solver is None:
            from utils.captcha_solver import CaptchaSolver
            self._captcha_solver = CaptchaSolver(toolkit=self)
        return self._captcha_solver

    async def detect_captcha(self) -> ToolkitResult:
        """检测当前页面是否有验证码"""
        try:
            result = await self.captcha_solver.detect_captcha()
            return ToolkitResult(success=True, data=result)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def solve_captcha(self, max_retries: int = 3) -> ToolkitResult:
        """自动检测并解决验证码"""
        try:
            solved = await self.captcha_solver.solve(max_retries=max_retries)
            return ToolkitResult(success=solved)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    # ── download ─────────────────────────────────────────────

    async def expect_download(self, selector: str, save_path: str = "", timeout: int = None) -> ToolkitResult:
        """点击触发下载并可选保存"""
        try:
            timeout_ms = timeout if timeout is not None else settings.BROWSER_DOWNLOAD_TIMEOUT
            surface = self.active_surface
            async with self._page.expect_download(timeout=self._timeout(timeout_ms)) as dl_info:
                await surface.click(selector, timeout=self._timeout(settings.BROWSER_ACTION_TIMEOUT))
            download = await dl_info.value
            if save_path:
                await download.save_as(save_path)
            return ToolkitResult(success=True, data=download.suggested_filename)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    # ── semantic locators (for fallback strategies) ──────────

    async def click_by_role(self, role: str, name: str, timeout: int = None) -> ToolkitResult:
        try:
            timeout_ms = timeout if timeout is not None else settings.BROWSER_ACTION_TIMEOUT
            surface = self.active_surface
            await surface.get_by_role(role, name=name, exact=False).first.click(timeout=self._timeout(timeout_ms))
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def click_by_label(self, label: str, timeout: int = None) -> ToolkitResult:
        try:
            timeout_ms = timeout if timeout is not None else settings.BROWSER_ACTION_TIMEOUT
            surface = self.active_surface
            await surface.get_by_label(label, exact=False).first.click(timeout=self._timeout(timeout_ms))
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def fill_by_placeholder(self, placeholder: str, value: str, timeout: int = None) -> ToolkitResult:
        try:
            timeout_ms = timeout if timeout is not None else settings.BROWSER_ACTION_TIMEOUT
            surface = self.active_surface
            await surface.get_by_placeholder(placeholder, exact=False).first.fill(value, timeout=self._timeout(timeout_ms))
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def fill_by_label(self, label: str, value: str, timeout: int = None) -> ToolkitResult:
        try:
            timeout_ms = timeout if timeout is not None else settings.BROWSER_ACTION_TIMEOUT
            surface = self.active_surface
            await surface.get_by_label(label, exact=False).first.fill(value, timeout=self._timeout(timeout_ms))
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))
