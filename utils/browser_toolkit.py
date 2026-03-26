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
        self._anti_robot = None  # lazy
        self._semantic_ref_map: Dict[str, Dict[str, Any]] = {}

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
                # 只按 URL 模式拦截图片/字体/媒体，不拦截其他请求
                # 避免 route("**/*") 导致所有请求都经过 handler，
                # 在 SPA 点击导航时与 continue_() 产生竞态引发页面崩溃
                async def _abort_route(route):
                    try:
                        await route.abort()
                    except Exception:
                        pass

                _IMG_PATTERN = "**/*.{png,jpg,jpeg,gif,webp,svg,ico,bmp,avif,tiff}"
                _FONT_PATTERN = "**/*.{woff,woff2,ttf,eot,otf}"
                _MEDIA_PATTERN = "**/*.{mp4,mp3,webm,ogg,wav,avi,flv,m4a,aac}"
                for pattern in (_IMG_PATTERN, _FONT_PATTERN, _MEDIA_PATTERN):
                    await self._context.route(pattern, _abort_route)

            self._page = await self._context.new_page()

            # 注入反检测脚本（合并两处最优 + 增强反机器人检测）
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

                    // ── 核心: 隐藏自动化痕迹 ──
                    overrideGetter(navigator, 'webdriver', undefined);

                    // 删除 Playwright/Puppeteer 注入的全局变量
                    delete window.__playwright;
                    delete window.__pw_manual;
                    delete window.__PW_inspect;
                    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Array;
                    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Promise;
                    delete window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol;

                    // 构造逼真的 PluginArray
                    const makePlugin = (name, filename, desc) => {
                        const p = { name, filename, description: desc, length: 1 };
                        p[0] = { type: 'application/pdf', suffixes: 'pdf', description: '' };
                        return p;
                    };
                    const fakePlugins = [
                        makePlugin('PDF Viewer', 'internal-pdf-viewer', 'Portable Document Format'),
                        makePlugin('Chrome PDF Viewer', 'internal-pdf-viewer', ''),
                        makePlugin('Chromium PDF Viewer', 'internal-pdf-viewer', ''),
                        makePlugin('Microsoft Edge PDF Viewer', 'internal-pdf-viewer', ''),
                        makePlugin('WebKit built-in PDF', 'internal-pdf-viewer', ''),
                    ];
                    fakePlugins.item = (i) => fakePlugins[i] || null;
                    fakePlugins.namedItem = (name) => fakePlugins.find(p => p.name === name) || null;
                    fakePlugins.refresh = () => {};
                    overrideGetter(navigator, 'plugins', fakePlugins);

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

                    // ── 增强: 防止 toString 检测 ──
                    // 部分检测器会检查原生函数的 toString 是否被篡改
                    const nativeToString = Function.prototype.toString;
                    const spoofedFns = new Set();
                    const originalCall = Function.prototype.toString.call.bind(nativeToString);
                    Function.prototype.toString = function() {
                        if (spoofedFns.has(this)) {
                            return 'function ' + (this.name || '') + '() { [native code] }';
                        }
                        return originalCall(this);
                    };
                    spoofedFns.add(Function.prototype.toString);

                    // ── 增强: 防 iframe contentWindow 检测 ──
                    // 一些检测器通过创建 iframe 检测 navigator.webdriver
                    const origCreate = document.createElement.bind(document);
                    document.createElement = function(tagName, options) {
                        const el = origCreate(tagName, options);
                        if (tagName.toLowerCase() === 'iframe') {
                            const origAppend = el.__proto__.appendChild || Node.prototype.appendChild;
                            // 在 iframe 加载后也注入反检测
                            el.addEventListener('load', () => {
                                try {
                                    if (el.contentWindow && el.contentWindow.navigator) {
                                        Object.defineProperty(el.contentWindow.navigator, 'webdriver', {
                                            get: () => undefined, configurable: true,
                                        });
                                    }
                                } catch (e) {} // 跨域 iframe 会抛错，忽略
                            });
                        }
                        return el;
                    };
                    spoofedFns.add(document.createElement);

                    // ── 增强: Connection API ──
                    if (navigator.connection) {
                        overrideGetter(navigator.connection, 'rtt', 50);
                        overrideGetter(navigator.connection, 'downlink', 10);
                        overrideGetter(navigator.connection, 'effectiveType', '4g');
                        overrideGetter(navigator.connection, 'saveData', false);
                    }

                    // ── 增强: Battery API 不暴露 ──
                    // 真实浏览器中 getBattery 返回 Promise
                    if (navigator.getBattery) {
                        navigator.getBattery = () => Promise.resolve({
                            charging: true,
                            chargingTime: 0,
                            dischargingTime: Infinity,
                            level: 1.0,
                            addEventListener: () => {},
                            removeEventListener: () => {},
                        });
                        spoofedFns.add(navigator.getBattery);
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
            self._semantic_ref_map = {}
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
            self._semantic_ref_map.clear()  # 导航会切换页面，旧 ref 失效
            await self._page.goto(url, wait_until=wait_until, timeout=self._timeout(timeout_ms))
            return ToolkitResult(success=True, data=self._page.url)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def go_back(self) -> ToolkitResult:
        try:
            self._semantic_ref_map.clear()
            await self._page.go_back(timeout=self._timeout(settings.BROWSER_NAVIGATION_TIMEOUT))
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def go_forward(self) -> ToolkitResult:
        try:
            self._semantic_ref_map.clear()
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

    # ── anti-robot facade ────────────────────────────────────

    @property
    def anti_robot(self):
        """懒加载 AntiRobotBypass"""
        if self._anti_robot is None:
            from utils.anti_robot_bypass import AntiRobotBypass
            self._anti_robot = AntiRobotBypass(toolkit=self)
        return self._anti_robot

    async def detect_robot_challenge(self) -> ToolkitResult:
        """检测当前页面是否有反机器人验证挑战"""
        try:
            detection = await self.anti_robot.detect_challenge()
            return ToolkitResult(
                success=True,
                data={
                    "has_challenge": detection.challenge_type.value != "none",
                    "challenge_type": detection.challenge_type.value,
                    "confidence": detection.confidence,
                    "detail": detection.detail,
                },
            )
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def bypass_robot_challenge(self, max_retries: int = 3) -> ToolkitResult:
        """检测并绕过反机器人验证挑战"""
        try:
            bypassed = await self.anti_robot.detect_and_bypass(max_retries=max_retries)
            return ToolkitResult(success=bypassed)
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

    # ── semantic snapshot / ref actions ─────────────────────

    async def _trigger_lazy_content(self) -> None:
        """Quick scroll nudge to trigger IntersectionObserver / lazy-loaded content."""
        try:
            surface = self.active_surface
            # Scroll down ~1 viewport then back, giving lazy loaders time to fire
            await surface.evaluate("""
                () => {
                    const vh = window.innerHeight || 768;
                    window.scrollBy(0, vh);
                }
            """)
            await asyncio.sleep(0.3)
            await surface.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.15)
        except Exception:
            pass  # Non-critical — best effort

    async def semantic_snapshot(
        self,
        max_elements: int = 80,
        include_cards: bool = True,
    ) -> ToolkitResult:
        """Decomposed semantic snapshot: runs 5+ sub-scripts with per-script isolation."""
        from utils.perception_scripts import (
            SCRIPT_REGIONS,
            SCRIPT_INTERACTIVE_ELEMENTS,
            SCRIPT_TEXT_CONTENT,
            SCRIPT_CONTROLS,
            assemble_semantic_snapshot,
            build_page_meta_script,
            build_content_cards_script,
        )
        from config.settings import settings

        try:
            surface = self.active_surface

            # Trigger lazy-loaded content before snapshot
            await self._trigger_lazy_content()

            # Run sub-scripts with per-script try/catch
            page_meta = {}
            try:
                page_meta_script = build_page_meta_script(settings.MODAL_CONTENT_THRESHOLD)
                page_meta = await surface.evaluate(page_meta_script) or {}
            except Exception as exc:
                log_warning(f"perception sub-script PAGE_META failed: {exc}")

            regions_data = {}
            try:
                regions_data = await surface.evaluate(SCRIPT_REGIONS) or {}
            except Exception as exc:
                log_warning(f"perception sub-script REGIONS failed: {exc}")

            elements_data = {}
            try:
                elements_data = await surface.evaluate(
                    SCRIPT_INTERACTIVE_ELEMENTS,
                    {"max_elements": max_elements},
                ) or {}
            except Exception as exc:
                log_warning(f"perception sub-script INTERACTIVE_ELEMENTS failed: {exc}")

            cards_data = {}
            if include_cards:
                try:
                    cards_script = build_content_cards_script(
                        max_cards=settings.MAX_EXTRACT_CARDS,
                        card_title_chars=settings.CARD_TITLE_DISPLAY_CHARS,
                        card_source_chars=settings.CARD_SOURCE_DISPLAY_CHARS,
                        card_snippet_chars=settings.CARD_SNIPPET_DISPLAY_CHARS,
                    )
                    cards_data = await surface.evaluate(
                        cards_script,
                        {"elementRefs": {}},
                    ) or {}
                except Exception as exc:
                    log_warning(f"perception sub-script CONTENT_CARDS failed: {exc}")

            text_data = {}
            try:
                text_data = await surface.evaluate(SCRIPT_TEXT_CONTENT) or {}
            except Exception as exc:
                log_warning(f"perception sub-script TEXT_CONTENT failed: {exc}")

            controls_data = {}
            try:
                controls_data = await surface.evaluate(SCRIPT_CONTROLS) or {}
            except Exception as exc:
                log_warning(f"perception sub-script CONTROLS failed: {exc}")

            # Collect text/elements from same-origin iframes
            iframe_texts = []
            iframe_elements = []
            try:
                page_obj = self._page
                if page_obj:
                    for frame in page_obj.frames:
                        if frame == page_obj.main_frame:
                            continue
                        # Only process same-origin frames (cross-origin will throw)
                        try:
                            frame_url = frame.url or ""
                            if not frame_url or frame_url.startswith("about:") or frame_url.startswith("javascript:"):
                                continue
                            frame_text = await frame.evaluate(SCRIPT_TEXT_CONTENT) or {}
                            ft = (frame_text.get("main_text") or "").strip()
                            if ft and len(ft) > 30:
                                iframe_texts.append(ft[:3000])
                            frame_elems = await frame.evaluate(
                                SCRIPT_INTERACTIVE_ELEMENTS,
                                {"max_elements": 20},
                            ) or {}
                            for el in (frame_elems.get("elements") or []):
                                el["ref"] = f"iframe_{el.get('ref', '')}"
                                el["region"] = "iframe"
                                iframe_elements.append(el)
                        except Exception:
                            continue  # cross-origin or detached frame
            except Exception:
                pass

            # Merge iframe content into main results
            if iframe_texts:
                existing_main = text_data.get("main_text") or ""
                text_data["main_text"] = existing_main + "\n[iframe content]\n" + "\n".join(iframe_texts)
            if iframe_elements:
                existing_elems = elements_data.get("elements") or []
                elements_data["elements"] = existing_elems + iframe_elements

            # Assemble into unified snapshot
            snapshot = assemble_semantic_snapshot(
                page_meta=page_meta,
                regions=regions_data,
                elements=elements_data,
                cards_and_collections=cards_data,
                text_content=text_data,
                controls=controls_data,
            )

            # Build ref map (same logic as before)
            ref_map: Dict[str, Dict[str, Any]] = {}
            for item in snapshot.get("elements", []) or []:
                ref = str(item.get("ref", "") or "").strip()
                if ref:
                    ref_map[ref] = {
                        "selector": str(item.get("selector", "") or ""),
                        "role": str(item.get("role", "") or ""),
                        "text": str(item.get("text", "") or ""),
                        "label": str(item.get("label", "") or ""),
                        "placeholder": str(item.get("placeholder", "") or ""),
                        "value": str(item.get("value", "") or ""),
                        "href": str(item.get("href", "") or ""),
                        "type": str(item.get("type", "") or ""),
                    }
            for card in snapshot.get("cards", []) or []:
                ref = str(card.get("ref", "") or "").strip()
                if ref:
                    ref_map[ref] = {
                        "selector": str(card.get("target_selector", "") or ""),
                        "role": "link",
                        "text": str(card.get("title", "") or ""),
                        "label": str(card.get("title", "") or ""),
                        "placeholder": "",
                        "value": "",
                        "href": str(card.get("link", "") or ""),
                        "type": "card",
                        "target_ref": str(card.get("target_ref", "") or ""),
                    }
            for control in snapshot.get("controls", []) or []:
                ref = str(control.get("ref", "") or "").strip()
                if not ref:
                    continue
                ref_map[ref] = {
                    "selector": str(control.get("selector", "") or ""),
                    "role": "textbox" if str(control.get("kind", "") or "") == "search_input" else "button",
                    "text": str(control.get("text", "") or ""),
                    "label": str(control.get("text", "") or ""),
                    "placeholder": str(control.get("text", "") or ""),
                    "value": "",
                    "href": "",
                    "type": str(control.get("kind", "") or "control"),
                }
            for region in snapshot.get("regions", []) or []:
                ref = str(region.get("ref", "") or "").strip()
                if not ref:
                    continue
                ref_map[ref] = {
                    "selector": str(region.get("selector", "") or ""),
                    "role": "region",
                    "text": str(region.get("heading", "") or region.get("text_sample", "") or ""),
                    "label": str(region.get("heading", "") or ""),
                    "placeholder": "",
                    "value": "",
                    "href": "",
                    "type": str(region.get("kind", "") or "region"),
                }
            self._semantic_ref_map = ref_map
            return ToolkitResult(success=True, data=snapshot)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def _legacy_semantic_snapshot(
        self,
        max_elements: int = 80,
        include_cards: bool = True,
    ) -> ToolkitResult:
        """Legacy monolithic semantic snapshot (kept as fallback)."""
        try:
            surface = self.active_surface
            payload = await surface.evaluate(
                r"""
                (args) => {
                  const maxElements = Math.max(Number(args?.max_elements || 80), 20);
                  const includeCards = !!args?.include_cards;
                  const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
                  const cleanHost = (value) => String(value || '').replace(/^www\./, '').toLowerCase();
                  const currentHost = cleanHost(location.hostname || '');

                  const isVisible = (element) => {
                    if (!element) return false;
                    const style = window.getComputedStyle(element);
                    if (!style || style.visibility === 'hidden' || style.display === 'none') return false;
                    const rect = element.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                  };

                  const labelOf = (el) => {
                    if (!el) return '';
                    if (el.labels && el.labels.length) {
                      return normalize(Array.from(el.labels).map((item) => item.innerText || item.textContent || '').join(' '));
                    }
                    const id = el.getAttribute('id');
                    if (id) {
                      const explicit = document.querySelector(`label[for="${id}"]`);
                      if (explicit) return normalize(explicit.innerText || explicit.textContent || '');
                    }
                    const parentLabel = el.closest('label');
                    return parentLabel ? normalize(parentLabel.innerText || parentLabel.textContent || '') : '';
                  };

                  const selectorOf = (el) => {
                    if (!el) return '';
                    const stableDataAttrs = ['data-testid', 'data-id', 'data-cy', 'data-qa', 'data-test'];
                    for (const attr of stableDataAttrs) {
                      const value = el.getAttribute(attr);
                      if (value) return `[${attr}="${CSS.escape(value)}"]`;
                    }
                    if (el.id) return `#${CSS.escape(el.id)}`;
                    const name = el.getAttribute('name');
                    if (name) return `${el.tagName.toLowerCase()}[name="${CSS.escape(name)}"]`;
                    const placeholder = el.getAttribute('placeholder');
                    if (placeholder) return `${el.tagName.toLowerCase()}[placeholder="${CSS.escape(placeholder)}"]`;
                    const href = el.getAttribute('href');
                    if (href && href.length <= 200) return `${el.tagName.toLowerCase()}[href="${CSS.escape(href)}"]`;
                    const parts = [];
                    let current = el;
                    while (current && current.nodeType === 1 && parts.length < 5) {
                      let part = current.tagName.toLowerCase();
                      const parent = current.parentElement;
                      if (parent) {
                        const siblings = Array.from(parent.children).filter((item) => item.tagName === current.tagName);
                        if (siblings.length > 1) {
                          part += `:nth-of-type(${siblings.indexOf(current) + 1})`;
                        }
                      }
                      parts.unshift(part);
                      current = parent;
                    }
                    return parts.join(' > ');
                  };

                  const roleOf = (el) => {
                    const explicitRole = normalize(el.getAttribute('role') || '').toLowerCase();
                    if (explicitRole) return explicitRole;
                    const tag = el.tagName.toLowerCase();
                    const type = normalize(el.getAttribute('type') || '').toLowerCase();
                    if (tag === 'a') return 'link';
                    if (tag === 'button') return 'button';
                    if (tag === 'select') return 'combobox';
                    if (tag === 'textarea') return 'textbox';
                    if (tag === 'input') {
                      if (['submit', 'button', 'reset'].includes(type)) return 'button';
                      if (type === 'search') return 'searchbox';
                      if (type === 'checkbox') return 'checkbox';
                      if (type === 'radio') return 'radio';
                      return 'textbox';
                    }
                    return tag;
                  };

                  const elementTypeOf = (el) => {
                    const tag = el.tagName.toLowerCase();
                    const type = normalize(el.getAttribute('type') || '').toLowerCase();
                    if (tag === 'a') return 'link';
                    if (tag === 'button') return 'button';
                    if (tag === 'select') return 'select';
                    if (tag === 'textarea') return 'textarea';
                    if (tag === 'input' && type) return type;
                    return tag;
                  };

                  const regionOf = (el) => {
                    if (!el) return 'body';
                    const pairs = [
                      ['main, [role="main"]', 'main'],
                      ['header, [role="banner"]', 'header'],
                      ['footer, [role="contentinfo"]', 'footer'],
                      ['nav, [role="navigation"]', 'navigation'],
                      ['aside, [role="complementary"]', 'aside'],
                      ['dialog, [role="dialog"], [aria-modal="true"], .modal, .dialog', 'modal'],
                      ['form', 'form'],
                    ];
                    for (const [selector, name] of pairs) {
                      const region = el.closest(selector);
                      if (region) return name;
                    }
                    return 'body';
                  };

                  const isDisabled = (element) => {
                    if (!element) return true;
                    const ariaDisabled = normalize(element.getAttribute('aria-disabled') || '').toLowerCase();
                    return !!(
                      element.disabled ||
                      ariaDisabled === 'true' ||
                      element.classList.contains('disabled') ||
                      element.classList.contains('is-disabled')
                    );
                  };

                  const findVisibleAction = (selectors, matcher = null) => {
                    for (const selector of selectors) {
                      for (const element of Array.from(document.querySelectorAll(selector))) {
                        if (!isVisible(element) || isDisabled(element)) continue;
                        if (typeof matcher === 'function' && !matcher(element)) continue;
                        return element;
                      }
                    }
                    return null;
                  };

                  const searchEngineHosts = ['google.com', 'bing.com', 'duckduckgo.com', 'baidu.com', 'sogou.com'];
                  const isSearchHost = searchEngineHosts.some((host) => currentHost === host || currentHost.endsWith(`.${host}`));

                  const nodes = Array.from(document.querySelectorAll(
                    'a[href], button, input, textarea, select, [role="button"], [role="link"], [role="textbox"], [contenteditable="true"]'
                  )).filter((element) => isVisible(element)).slice(0, maxElements);

                  const entries = nodes.map((element, index) => {
                    const rect = element.getBoundingClientRect();
                    const text = normalize(
                      element.innerText ||
                      element.textContent ||
                      element.value ||
                      element.getAttribute('aria-label') ||
                      element.getAttribute('title') ||
                      element.getAttribute('placeholder') ||
                      ''
                    ).slice(0, 220);
                    const payload = {
                      ref: `el_${index + 1}`,
                      role: roleOf(element),
                      tag: element.tagName.toLowerCase(),
                      type: elementTypeOf(element),
                      text,
                      href: element.href || element.getAttribute('href') || '',
                      value: typeof element.value === 'string' ? String(element.value || '').slice(0, 220) : '',
                      label: labelOf(element).slice(0, 220),
                      placeholder: normalize(element.getAttribute('placeholder') || '').slice(0, 220),
                      selector: selectorOf(element),
                      visible: true,
                      enabled: !element.disabled,
                      region: regionOf(element),
                      parent_ref: '',
                      bbox: {
                        x: Math.round(rect.x),
                        y: Math.round(rect.y),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height),
                      },
                    };
                    return { node: element, payload };
                  });

                  const nodeToPayload = new Map(entries.map((entry) => [entry.node, entry.payload]));
                  const nodeToRef = new Map(entries.map((entry) => [entry.node, entry.payload.ref]));

                  const looksLikeSearchResultsUrl = () => {
                    const path = (location.pathname || '').toLowerCase();
                    const params = new URLSearchParams(location.search || '');
                    const hasAny = (...keys) => keys.some((key) => params.has(key));
                    if (currentHost === 'bing.com' || currentHost.endsWith('.bing.com')) {
                      return path.includes('/search') && hasAny('q');
                    }
                    if (currentHost === 'google.com' || currentHost.endsWith('.google.com')) {
                      return path.includes('/search') && hasAny('q');
                    }
                    if (currentHost === 'baidu.com' || currentHost.endsWith('.baidu.com')) {
                      return (path === '/s' || path.startsWith('/s')) && (
                        hasAny('wd', 'word') ||
                        !!document.querySelector('#content_left .result, #content_left .c-container, #content_left .result-op')
                      );
                    }
                    if (currentHost === 'duckduckgo.com' || currentHost.endsWith('.duckduckgo.com')) {
                      return (path === '/' || path.startsWith('/html') || path.startsWith('/lite')) && hasAny('q');
                    }
                    return (
                      (path.includes('/search') && hasAny('q', 'query')) ||
                      !!document.querySelector('#b_results .b_algo, #search .g, #content_left .result, .results .result')
                    );
                  };

                  const inferPageType = () => {
                    if (document.querySelector('dialog[open], [role="dialog"], [aria-modal="true"], .modal.show, .dialog')) {
                      return 'modal';
                    }
                    if (isSearchHost && looksLikeSearchResultsUrl()) return 'serp';
                    if (document.querySelector('input[type="password"]')) return 'login';
                    // form 判断：要求 form 内有 2+ text-like 输入框，避免仅有搜索框的页面被误判
                    const forms = document.querySelectorAll('form');
                    if (forms.length) {
                      const textInputCount = document.querySelectorAll(
                        'form input[type="text"], form input[type="email"], form input[type="tel"], form input[type="number"], form input:not([type]), form textarea, form select'
                      ).length;
                      if (textInputCount >= 2) return 'form';
                    }
                    if (document.querySelector('article h1, main h1, article time, article [datetime]')) return 'detail';
                    // 扩展 list 检测：包括现代 SPA 常用的卡片式列表
                    const listCandidates = document.querySelectorAll(
                      'main li, main article, [role="main"] li, [role="main"] article, table tbody tr, ' +
                      '[role="listitem"], [class*="card"]:not(nav *), [class*="result"]:not(nav *), [class*="item"]:not(nav *):not(li)'
                    );
                    if (listCandidates.length >= 4) return 'list';
                    if (document.querySelector('article, main, [role="main"]')) return 'detail';
                    return 'unknown';
                  };

                  const cards = [];
                  if (includeCards && isSearchHost) {
                    const selectorMap = {
                      'bing.com': ['#b_results li.b_algo', '#b_results li.b_ans', '#b_results .b_algo', '.b_algo'],
                      'google.com': ['#search .tF2Cxc', '#search .g', '#search .MjjYud', '[data-sokoban-container]'],
                      'baidu.com': ['#content_left .result', '#content_left .c-container', '#content_left .result-op', '#content_left .xpath-log'],
                      'duckduckgo.com': ['.results .result', '.result', '.result__body', '[data-testid="result"]'],
                      'sogou.com': ['.results .vrwrap', '.results .rb', '.results .fb', '.vrwrap', '.rb'],
                    };
                    let selectors = ['main a[href]'];
                    for (const [host, values] of Object.entries(selectorMap)) {
                      if (currentHost === host || currentHost.endsWith(`.${host}`)) {
                        selectors = values;
                        break;
                      }
                    }

                    const seen = new Set();
                    let rank = 0;
                    const toAbsoluteUrl = (value) => {
                      const text = normalize(value);
                      if (!text || /^javascript:/i.test(text)) return '';
                      try {
                        return new URL(text, location.href).toString();
                      } catch (_error) {
                        return '';
                      }
                    };
                    const hostOf = (value) => {
                      try {
                        return cleanHost(new URL(value, location.href).hostname);
                      } catch (_error) {
                        return '';
                      }
                    };
                    const isSearchIntermediaryUrl = (value) => {
                      const href = toAbsoluteUrl(value);
                      if (!href) return false;
                      try {
                        const parsed = new URL(href, location.href);
                        const host = cleanHost(parsed.hostname);
                        if (!host || host !== currentHost) return false;
                        const path = (parsed.pathname || '').toLowerCase();
                        const params = new Set(Array.from(parsed.searchParams.keys()).map((key) => String(key || '').trim().toLowerCase()));
                        if (path === '/s' && (params.has('wd') || params.has('word'))) return true;
                        if (path.startsWith('/link') || path.startsWith('/url') || path.startsWith('/ck/a')) return true;
                        return path.includes('/search') && (params.has('q') || params.has('query') || params.has('wd') || params.has('word'));
                      } catch (_error) {
                        return false;
                      }
                    };
                    const parseDataLog = (value) => {
                      const text = normalize(value);
                      if (!text) return '';
                      try {
                        const parsed = JSON.parse(text);
                        return normalize(
                          parsed.mu ||
                          parsed.url ||
                          parsed.target ||
                          parsed.lmu ||
                          parsed.land_url ||
                          (parsed.data && (parsed.data.mu || parsed.data.url || parsed.data.target)) ||
                          ''
                        );
                      } catch (_error) {
                        return '';
                      }
                    };
                    const decodeParamValue = (value) => {
                      let text = normalize(value);
                      if (!text) return '';
                      for (let i = 0; i < 2; i += 1) {
                        try {
                          const decoded = decodeURIComponent(text);
                          if (decoded === text) break;
                          text = decoded;
                        } catch (_error) {
                          break;
                        }
                      }
                      return /^https?:/i.test(text) ? text : '';
                    };
                    const extractRedirectTarget = (value) => {
                      const href = toAbsoluteUrl(value);
                      if (!href) return '';
                      try {
                        const parsed = new URL(href, location.href);
                        const candidates = ['uddg', 'u', 'url', 'q', 'target', 'redirect', 'imgurl']
                          .flatMap((key) => parsed.searchParams.getAll(key))
                          .map((candidate) => decodeParamValue(candidate))
                          .filter(Boolean);
                        return candidates[0] || '';
                      } catch (_error) {
                        return '';
                      }
                    };
                    const resolveSearchResultUrl = (container, anchor) => {
                      const rawHref = toAbsoluteUrl(anchor?.href || anchor?.getAttribute('href') || '');
                      const candidates = [
                        extractRedirectTarget(rawHref),
                        anchor?.getAttribute('mu'),
                        anchor?.getAttribute('data-landurl'),
                        anchor?.getAttribute('data-url'),
                        anchor?.getAttribute('data-target'),
                        container?.getAttribute('mu'),
                        container?.getAttribute('data-landurl'),
                        container?.getAttribute('data-url'),
                        container?.getAttribute('data-target'),
                        parseDataLog(anchor?.getAttribute('data-log') || ''),
                        parseDataLog(container?.getAttribute('data-log') || ''),
                      ]
                        .map((value) => toAbsoluteUrl(value))
                        .filter(Boolean);
                      const external = candidates.find((value) => {
                        const candidateHost = hostOf(value);
                        return candidateHost && candidateHost !== currentHost;
                      }) || '';
                      return {
                        rawHref,
                        targetUrl: external || candidates[0] || '',
                        link: external || candidates[0] || rawHref,
                      };
                    };
                    const buildCard = (container, anchor) => {
                      if (!container || !anchor || !isVisible(container) || !isVisible(anchor)) return false;
                      const resolvedLink = resolveSearchResultUrl(container, anchor);
                      const href = resolvedLink.link;
                      const rawHref = resolvedLink.rawHref || href;
                      if (!href || /^javascript:/i.test(href)) return false;

                      const host = hostOf(href);
                      if (!host) return false;
                      if (host === currentHost && !resolvedLink.targetUrl && !isSearchIntermediaryUrl(rawHref)) return false;

                      const titleNode = container.querySelector('h1, h2, h3') || anchor;
                      const title = normalize(
                        titleNode?.innerText ||
                        titleNode?.textContent ||
                        anchor.getAttribute('aria-label') ||
                        anchor.getAttribute('title') ||
                        ''
                      );
                      if (title.length < 3) return false;

                      const snippetNode = container.querySelector(
                        '.b_caption p, .snippet, .st, .c-abstract, .compText, p, [data-testid="result-snippet"]'
                      );
                      const sourceNode = container.querySelector(
                        'cite, .cite, .b_attribution, .source, .news-source, [data-testid="result-source"]'
                      );
                      const dateNode = container.querySelector('time, .news-date, .timestamp, .date');
                      let snippet = normalize(snippetNode?.innerText || snippetNode?.textContent || '');
                      if (!snippet) {
                        snippet = normalize((container.innerText || container.textContent || '').replace(title, ''));
                      }
                      const source = normalize(sourceNode?.innerText || sourceNode?.textContent || '');
                      const date = normalize(dateNode?.innerText || dateNode?.textContent || '');
                      const key = `${title}|${resolvedLink.targetUrl || href}`;
                      if (seen.has(key)) return false;
                      seen.add(key);

                      rank += 1;
                      const cardRef = `card_${rank}`;
                      const targetPayload = nodeToPayload.get(anchor);
                      if (targetPayload) {
                        targetPayload.parent_ref = cardRef;
                      }
                      for (const entry of entries) {
                        if (!entry.payload.parent_ref && container.contains(entry.node)) {
                          entry.payload.parent_ref = cardRef;
                        }
                      }
                      cards.push({
                        ref: cardRef,
                        card_type: 'search_result',
                        title: title.slice(0, 240),
                        source: source.slice(0, 120),
                        snippet: snippet.slice(0, 400),
                        date: date.slice(0, 80),
                        host,
                        link: href,
                        raw_link: rawHref,
                        target_url: resolvedLink.targetUrl,
                        rank,
                        target_ref: targetPayload ? targetPayload.ref : '',
                        target_selector: selectorOf(anchor),
                      });
                      return true;
                    };

                    const candidateContainers = Array.from(document.querySelectorAll(selectors.join(', ')));
                    for (const container of candidateContainers) {
                      if (!isVisible(container)) continue;
                      const anchor = container.matches('a[href]')
                        ? container
                        : container.querySelector('h2 a, h3 a, a[href]');
                      if (buildCard(container, anchor) && cards.length >= 10) break;
                    }

                    if (!cards.length) {
                      const fallbackContainers = Array.from(document.querySelectorAll(
                        'main li, main article, main section, main div, [role="main"] li, [role="main"] article, [role="main"] section, [role="main"] div'
                      ));
                      for (const container of fallbackContainers) {
                        if (!isVisible(container)) continue;
                        if (container.closest('nav, header, footer, aside, form, dialog, [role="dialog"], [aria-modal="true"]')) {
                          continue;
                        }
                        const containerText = normalize(container.innerText || container.textContent || '');
                        if (containerText.length < 12) continue;

                        const anchor = Array.from(container.querySelectorAll('a[href]')).find((candidate) => {
                          if (!isVisible(candidate)) return false;
                          const candidateText = normalize(
                            candidate.innerText ||
                            candidate.textContent ||
                            candidate.getAttribute('aria-label') ||
                            candidate.getAttribute('title') ||
                            ''
                          );
                          if (candidateText.length < 3) return false;
                          const resolvedLink = resolveSearchResultUrl(container, candidate);
                          if (!resolvedLink.link) return false;
                          const host = hostOf(resolvedLink.link);
                          return !!host;
                        });
                        if (buildCard(container, anchor) && cards.length >= 10) break;
                      }
                    }
                  }

                  const buildCollection = (kind, nodes, prefix) => {
                    const visibleNodes = nodes.filter((node) => isVisible(node));
                    if (!visibleNodes.length) return null;
                    const sampleTexts = [];
                    for (const node of visibleNodes) {
                      const text = normalize(node.innerText || node.textContent || '');
                      if (text && !sampleTexts.includes(text)) {
                        sampleTexts.push(text.slice(0, 200));
                      }
                      if (sampleTexts.length >= 5) break;
                    }
                    return {
                      ref: `${prefix}_${kind}`,
                      kind,
                      item_count: visibleNodes.length,
                      sample_items: sampleTexts,
                    };
                  };

                  const inferRegionKind = (element) => {
                    if (!element) return 'section';
                    if (element.matches('dialog, [role="dialog"], [aria-modal="true"], .modal, .dialog')) return 'modal';
                    if (element.matches('nav, [role="navigation"]')) return 'navigation';
                    if (element.matches('form')) return 'form';
                    const tableRows = element.querySelectorAll('table tbody tr, tbody tr, tr').length;
                    if (element.matches('table') || tableRows >= 3) return 'table';
                    const listItems = element.querySelectorAll('li, article, [role="listitem"], [data-testid*="result"], .result, .item').length;
                    if (element.matches('ul, ol') || listItems >= 4) return 'list';
                    if (element.matches('article') || element.querySelector('h1, h2, time, [datetime]')) return 'detail';
                    if (element.matches('main, [role="main"]')) return 'main';
                    if (element.matches('aside, [role="complementary"]')) return 'aside';
                    if (element.matches('section')) return 'section';
                    return regionOf(element);
                  };

                  const regionMetrics = (element) => {
                    const rect = element.getBoundingClientRect();
                    const text = normalize(element.innerText || element.textContent || '');
                    const headingNode = element.querySelector('h1, h2, h3, legend, caption, th');
                    const heading = normalize(
                      headingNode?.innerText ||
                      headingNode?.textContent ||
                      element.getAttribute('aria-label') ||
                      element.getAttribute('title') ||
                      ''
                    );
                    const listItems = Array.from(
                      element.querySelectorAll('li, article, [role="listitem"], table tbody tr, tbody tr')
                    ).filter((node) => isVisible(node)).length;
                    const links = Array.from(element.querySelectorAll('a[href]')).filter((node) => isVisible(node)).length;
                    const controls = Array.from(
                      element.querySelectorAll('input, textarea, select, button, [role="button"], [contenteditable="true"]')
                    ).filter((node) => isVisible(node)).length;
                    const samples = [];
                    for (const sampleNode of Array.from(
                      element.querySelectorAll('h1, h2, h3, li, article, p, table tbody tr, tbody tr, figcaption')
                    )) {
                      if (!isVisible(sampleNode)) continue;
                      const sampleText = normalize(sampleNode.innerText || sampleNode.textContent || '');
                      if (!sampleText || samples.includes(sampleText)) continue;
                      samples.push(sampleText.slice(0, 160));
                      if (samples.length >= 3) break;
                    }
                    const kind = inferRegionKind(element);
                    let score = Math.min(Math.round((rect.width * rect.height) / 40000), 8);
                    score += Math.min(Math.round(text.length / 120), 6);
                    score += Math.min(listItems, 6);
                    score += Math.min(links, 4);
                    score += Math.min(controls, 3);
                    if (heading) score += 2;
                    if (kind === 'detail') score += 3;
                    if (kind === 'table' || kind === 'list') score += 2;
                    if (kind === 'main') score += 2;
                    if (kind === 'navigation') score -= 2;
                    return {
                      node: element,
                      score,
                      kind,
                      selector: selectorOf(element),
                      text_sample: text.slice(0, 320),
                      heading: heading.slice(0, 160),
                      text_length: text.length,
                      item_count: listItems,
                      link_count: links,
                      control_count: controls,
                      region: regionOf(element),
                      bbox: {
                        x: Math.round(rect.x),
                        y: Math.round(rect.y),
                        width: Math.round(rect.width),
                        height: Math.round(rect.height),
                      },
                      sample_items: samples,
                    };
                  };

                  const rawRegions = Array.from(document.querySelectorAll(
                    'main, [role="main"], article, section, form, table, ul, ol, nav, aside, dialog[open], [role="dialog"], [aria-modal="true"]'
                  ))
                    .filter((element) => {
                      if (!isVisible(element)) return false;
                      const metrics = regionMetrics(element);
                      if (!metrics.text_sample && metrics.control_count === 0 && metrics.link_count === 0) return false;
                      if (metrics.kind === 'navigation' && metrics.link_count < 3) return false;
                      if (metrics.kind === 'section' && metrics.text_length < 80 && metrics.item_count < 2) return false;
                      return true;
                    })
                    .map((element) => regionMetrics(element))
                    .sort((left, right) => right.score - left.score);

                  const regions = [];
                  const regionEntries = [];
                  for (const metrics of rawRegions) {
                    const overlapsExisting = regionEntries.some((existing) => {
                      if (!existing || !existing.node) return false;
                      if (!existing.node.contains(metrics.node)) return false;
                      if (existing.kind === metrics.kind) return true;
                      const maxLength = Math.max(existing.text_length || 0, metrics.text_length || 0, 1);
                      return Math.min(existing.text_length || 0, metrics.text_length || 0) / maxLength >= 0.75;
                    });
                    if (overlapsExisting) continue;
                    regionEntries.push(metrics);
                    regions.push({
                      ref: `region_${regions.length + 1}`,
                      kind: metrics.kind,
                      selector: metrics.selector,
                      heading: metrics.heading,
                      text_sample: metrics.text_sample,
                      sample_items: metrics.sample_items,
                      item_count: metrics.item_count,
                      link_count: metrics.link_count,
                      control_count: metrics.control_count,
                      region: metrics.region,
                      bbox: metrics.bbox,
                    });
                    if (regions.length >= 8) break;
                  }

                  const collections = [];
                  const tableRows = Array.from(
                    document.querySelectorAll('main table tbody tr, [role="main"] table tbody tr, table tbody tr')
                  ).filter((node) => isVisible(node));
                  if (tableRows.length >= 2) {
                    const tableCollection = buildCollection('table', tableRows, 'collection_1');
                    if (tableCollection) collections.push(tableCollection);
                  }

                  const listItems = Array.from(
                    document.querySelectorAll('main li, article li, [role="main"] li, section li, main article, [role="main"] article')
                  ).filter((node) => isVisible(node));
                  if (listItems.length >= 4) {
                    const listCollection = buildCollection('list', listItems, 'collection_2');
                    if (listCollection) collections.push(listCollection);
                  }

                  // 现代 SPA 卡片容器识别：div.card / div.item / [role="listitem"] 等
                  if (collections.length < 3) {
                    const cardRoot = document.querySelector('main, article, [role="main"]') || document.body;
                    const cardSelectors = [
                      '[role="listitem"]',
                      '[class*="card"]:not(nav [class*="card"])',
                      '[class*="item"]:not(nav [class*="item"]):not(li)',
                      '[class*="result"]:not(nav [class*="result"])',
                      '[class*="post"]:not(nav [class*="post"])',
                      '[class*="entry"]:not(nav [class*="entry"])',
                    ];
                    for (const cardSel of cardSelectors) {
                      if (collections.length >= 3) break;
                      const cardNodes = Array.from(
                        cardRoot.querySelectorAll(cardSel)
                      ).filter((node) => {
                        if (!isVisible(node)) return false;
                        const text = normalize(node.innerText || node.textContent || '');
                        return text.length >= 20 && text.length < 2000;
                      });
                      if (cardNodes.length >= 3) {
                        const cardCollection = buildCollection('cards', cardNodes, `collection_${collections.length + 1}`);
                        if (cardCollection) collections.push(cardCollection);
                      }
                    }
                  }

                  const nextPageElement = findVisibleAction(
                    [
                      'a[rel="next"]',
                      'button[rel="next"]',
                      'a[aria-label*="next" i]',
                      'button[aria-label*="next" i]',
                      'a[aria-label*="下一页"]',
                      'button[aria-label*="下一页"]',
                      '.pagination a',
                      '.pagination button',
                      '.pager a',
                      '.pager button',
                      '[class*="pagination"] a',
                      '[class*="pagination"] button',
                      '[class*="pager"] a',
                      '[class*="pager"] button',
                    ],
                    (element) => /^(next|>|»|下一页|下页)$/i.test(normalize(element.innerText || element.textContent || element.getAttribute('aria-label') || ''))
                      || /next|下一页|pager-next|pagination-next/i.test(
                        `${normalize(element.className || '')} ${normalize(element.getAttribute('aria-label') || '')}`
                      )
                  );

                  const loadMoreElement = findVisibleAction(
                    [
                      'button',
                      'a',
                      '[role="button"]',
                    ],
                    (element) => /(load more|show more|view more|more results|加载更多|查看更多|更多|展开更多)/i.test(
                      normalize(element.innerText || element.textContent || element.getAttribute('aria-label') || '')
                    )
                  );

                  const searchInputElement = findVisibleAction([
                    'input[type="search"]',
                    'input[name*="search" i]',
                    'input[placeholder*="search" i]',
                    'input[placeholder*="搜索"]',
                  ]);

                  const modalRoot = findVisibleAction([
                    'dialog[open]',
                    '[role="dialog"]',
                    '[aria-modal="true"]',
                    '.modal.show',
                    '.dialog',
                    '[class*="modal"]',
                    '[class*="dialog"]',
                  ]);

                  const findModalAction = (patterns) => {
                    if (!modalRoot) return null;
                    const candidates = Array.from(modalRoot.querySelectorAll('button, a[href], [role="button"], input[type="button"], input[type="submit"]'));
                    for (const element of candidates) {
                      if (!isVisible(element) || isDisabled(element)) continue;
                      const text = normalize(
                        element.innerText ||
                        element.textContent ||
                        element.getAttribute('aria-label') ||
                        element.getAttribute('title') ||
                        element.getAttribute('value') ||
                        ''
                      );
                      if (!text) continue;
                      if (patterns.some((pattern) => pattern.test(text))) {
                        return element;
                      }
                    }
                    return null;
                  };

                  const modalPrimaryElement = findModalAction([
                    /accept/i, /agree/i, /allow/i, /continue/i, /ok/i, /okay/i, /got it/i,
                    /同意/, /接受/, /允许/, /继续/, /确定/, /好的/, /知道了/,
                  ]);
                  const modalSecondaryElement = findModalAction([
                    /reject/i, /decline/i, /deny/i, /not now/i, /skip/i, /later/i,
                    /拒绝/, /暂不/, /稍后/, /跳过/, /关闭/, /取消/,
                  ]);
                  const modalCloseElement = findModalAction([
                    /^×$/, /^x$/i, /close/i, /dismiss/i, /cancel/i, /关闭/, /取消/, /知道了/,
                  ]);

                  const controls = [];
                  const registerControl = (kind, element, defaultSelector) => {
                    if (!element) return;
                    controls.push({
                      ref: nodeToRef.get(element) || `ctl_${kind}`,
                      kind,
                      text: normalize(
                        element.innerText ||
                        element.textContent ||
                        element.getAttribute('aria-label') ||
                        element.getAttribute('placeholder') ||
                        ''
                      ).slice(0, 120),
                      selector: nodeToPayload.get(element)?.selector || defaultSelector || selectorOf(element),
                    });
                  };

                  registerControl('next_page', nextPageElement, '.pagination .next');
                  registerControl('load_more', loadMoreElement, 'button');
                  registerControl('search_input', searchInputElement, 'input[type="search"]');
                  registerControl('modal_primary', modalPrimaryElement, 'dialog button');
                  registerControl('modal_secondary', modalSecondaryElement, 'dialog button');
                  registerControl('modal_close', modalCloseElement, 'dialog button');

                  const pageType = inferPageType();
                  const collectionItemCount = collections.reduce(
                    (max, item) => Math.max(max, Number(item.item_count || 0)),
                    cards.length
                  );
                  const hasResults = cards.length > 0 || collectionItemCount > 0;
                  const contentRoot = document.querySelector('main, article, [role="main"]') || document.body;
                  const mainText = normalize(
                    contentRoot
                      ? (
                        contentRoot.innerText ||
                        contentRoot.textContent ||
                        document.body?.innerText ||
                        document.body?.textContent ||
                        ''
                      )
                      : (document.body?.innerText || document.body?.textContent || '')
                  ).slice(0, 6000);

                  const visibleTextBlocks = [];
                  const seenBlockTexts = new Set();
                  const blockNodes = Array.from(
                    (contentRoot || document.body).querySelectorAll(
                      'h1, h2, h3, h4, p, li, article, section, table tbody tr, tbody tr, dd, dt, figcaption, blockquote, [class*="content"], [class*="summary"], [class*="desc"]'
                    )
                  );
                  for (const node of blockNodes) {
                    if (!isVisible(node)) continue;
                    const text = normalize(node.innerText || node.textContent || '');
                    if (!text) continue;
                    if (text.length < (isSearchHost ? 6 : 16)) continue;
                    if (seenBlockTexts.has(text)) continue;
                    seenBlockTexts.add(text);
                    visibleTextBlocks.push({
                      kind: node.tagName.toLowerCase(),
                      text: text.slice(0, 320),
                      selector: selectorOf(node),
                      parent_ref: nodeToRef.get(node.closest('[data-testid], [id], article, section, main, li, tr')) || '',
                    });
                    if (visibleTextBlocks.length >= 16) break;
                  }

                  const blockedSignals = [];
                  const urlText = `${location.pathname || ''} ${location.search || ''}`.toLowerCase();
                  const titleText = normalize(document.title || '');
                  const bodyText = normalize(document.body?.innerText || document.body?.textContent || '');
                  const blockedChecks = [
                    ['url', /\/(sorry|captcha|verify|challenge|blocked|forbidden)/i, urlText],
                    ['title', /(unusual traffic|robot check|captcha|forbidden|access denied|blocked|人机身份验证|异常流量|验证码|安全验证|访问受限)/i, titleText],
                    ['body', /(unusual traffic|robot check|captcha|forbidden|access denied|blocked|人机身份验证|异常流量|验证码|安全验证|访问受限)/i, bodyText],
                  ];
                  for (const [kind, pattern, source] of blockedChecks) {
                    const match = String(source || '').match(pattern);
                    if (match && match[0]) {
                      blockedSignals.push(`${kind}:${String(match[0]).slice(0, 60)}`);
                    }
                  }

                  const inferPageStage = () => {
                    if (blockedSignals.length) return 'blocked';
                    if (modalRoot) return 'dismiss_modal';
                    if (pageType === 'serp') return hasResults ? 'selecting_source' : 'searching';
                    if (pageType === 'list') return hasResults ? 'extracting' : 'loading';
                    if (pageType === 'detail') return mainText.length >= 120 ? 'extracting' : 'loading';
                    if (pageType === 'form' || pageType === 'login') return 'interacting';
                    if (hasResults || mainText.length >= 120) return 'extracting';
                    return 'unknown';
                  };

                  return {
                    url: location.href,
                    title: document.title || '',
                    page_type: pageType,
                    page_stage: inferPageStage(),
                    main_text: mainText,
                    visible_text_blocks: visibleTextBlocks,
                    blocked_signals: blockedSignals,
                    regions,
                    elements: entries.map((entry) => entry.payload),
                    cards,
                    collections,
                    controls,
                    affordances: {
                      has_search_box: !!searchInputElement,
                      search_input_ref: searchInputElement ? (nodeToRef.get(searchInputElement) || 'ctl_search_input') : '',
                      search_input_selector: searchInputElement ? (nodeToPayload.get(searchInputElement)?.selector || selectorOf(searchInputElement)) : '',
                      has_pagination: !!nextPageElement || !!document.querySelector('.pagination, .pager, [class*="page-"], a[href*="page="]'),
                      next_page_ref: nextPageElement ? (nodeToRef.get(nextPageElement) || 'ctl_next_page') : '',
                      next_page_selector: nextPageElement ? (nodeToPayload.get(nextPageElement)?.selector || selectorOf(nextPageElement)) : '',
                      has_load_more: !!loadMoreElement,
                      load_more_ref: loadMoreElement ? (nodeToRef.get(loadMoreElement) || 'ctl_load_more') : '',
                      load_more_selector: loadMoreElement ? (nodeToPayload.get(loadMoreElement)?.selector || selectorOf(loadMoreElement)) : '',
                      has_modal: !!modalRoot,
                      modal_primary_ref: modalPrimaryElement ? (nodeToRef.get(modalPrimaryElement) || 'ctl_modal_primary') : '',
                      modal_primary_selector: modalPrimaryElement ? (nodeToPayload.get(modalPrimaryElement)?.selector || selectorOf(modalPrimaryElement)) : '',
                      modal_secondary_ref: modalSecondaryElement ? (nodeToRef.get(modalSecondaryElement) || 'ctl_modal_secondary') : '',
                      modal_secondary_selector: modalSecondaryElement ? (nodeToPayload.get(modalSecondaryElement)?.selector || selectorOf(modalSecondaryElement)) : '',
                      modal_close_ref: modalCloseElement ? (nodeToRef.get(modalCloseElement) || 'ctl_modal_close') : '',
                      modal_close_selector: modalCloseElement ? (nodeToPayload.get(modalCloseElement)?.selector || selectorOf(modalCloseElement)) : '',
                      has_login_form: !!document.querySelector('input[type="password"]'),
                      has_results: hasResults,
                      collection_item_count: collectionItemCount,
                    },
                  };
                }
                """,
                {"max_elements": max_elements, "include_cards": include_cards},
            )
            snapshot = payload if isinstance(payload, dict) else {}
            ref_map: Dict[str, Dict[str, Any]] = {}
            for item in snapshot.get("elements", []) or []:
                ref = str(item.get("ref", "") or "").strip()
                if ref:
                    ref_map[ref] = {
                        "selector": str(item.get("selector", "") or ""),
                        "role": str(item.get("role", "") or ""),
                        "text": str(item.get("text", "") or ""),
                        "label": str(item.get("label", "") or ""),
                        "placeholder": str(item.get("placeholder", "") or ""),
                        "value": str(item.get("value", "") or ""),
                        "href": str(item.get("href", "") or ""),
                        "type": str(item.get("type", "") or ""),
                    }
            for card in snapshot.get("cards", []) or []:
                ref = str(card.get("ref", "") or "").strip()
                if ref:
                    ref_map[ref] = {
                        "selector": str(card.get("target_selector", "") or ""),
                        "role": "link",
                        "text": str(card.get("title", "") or ""),
                        "label": str(card.get("title", "") or ""),
                        "placeholder": "",
                        "value": "",
                        "href": str(card.get("link", "") or ""),
                        "type": "card",
                        "target_ref": str(card.get("target_ref", "") or ""),
                    }
            for control in snapshot.get("controls", []) or []:
                ref = str(control.get("ref", "") or "").strip()
                if not ref:
                    continue
                ref_map[ref] = {
                    "selector": str(control.get("selector", "") or ""),
                    "role": "textbox" if str(control.get("kind", "") or "") == "search_input" else "button",
                    "text": str(control.get("text", "") or ""),
                    "label": str(control.get("text", "") or ""),
                    "placeholder": str(control.get("text", "") or ""),
                    "value": "",
                    "href": "",
                    "type": str(control.get("kind", "") or "control"),
                }
            for region in snapshot.get("regions", []) or []:
                ref = str(region.get("ref", "") or "").strip()
                if not ref:
                    continue
                ref_map[ref] = {
                    "selector": str(region.get("selector", "") or ""),
                    "role": "region",
                    "text": str(region.get("heading", "") or region.get("text_sample", "") or ""),
                    "label": str(region.get("heading", "") or ""),
                    "placeholder": "",
                    "value": "",
                    "href": "",
                    "type": str(region.get("kind", "") or "region"),
                }
            self._semantic_ref_map = ref_map
            return ToolkitResult(success=True, data=snapshot)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    def resolve_ref(self, ref: str) -> Dict[str, Any]:
        return dict(self._semantic_ref_map.get(str(ref or "").strip(), {}))

    async def click_ref(self, ref: str, timeout: int = None) -> ToolkitResult:
        info = self.resolve_ref(ref)
        if not info:
            return ToolkitResult(success=False, error=f"unknown ref: {ref}")

        target_ref = str(info.get("target_ref", "") or "").strip()
        if target_ref and target_ref != str(ref).strip():
            nested = await self.click_ref(target_ref, timeout=timeout)
            if nested.success:
                return nested

        selector = str(info.get("selector", "") or "")
        if selector:
            direct = await self.click(selector, timeout=timeout)
            if direct.success:
                return direct

        role = str(info.get("role", "") or "")
        for label in [info.get("label", ""), info.get("text", "")]:
            label_value = str(label or "").strip()[:80]
            if role and label_value:
                by_role = await self.click_by_role(role, label_value, timeout=timeout)
                if by_role.success:
                    return by_role
            if label_value:
                by_label = await self.click_by_label(label_value, timeout=timeout)
                if by_label.success:
                    return by_label

        if selector:
            locator = await self.locator_click(selector, timeout=timeout)
            if locator.success:
                return locator
            forced = await self.force_click(selector, timeout=timeout)
            if forced.success:
                return forced

        return ToolkitResult(success=False, error=f"failed to click ref: {ref}")

    async def input_ref(self, ref: str, value: str, timeout: int = None) -> ToolkitResult:
        info = self.resolve_ref(ref)
        if not info:
            return ToolkitResult(success=False, error=f"unknown ref: {ref}")

        selector = str(info.get("selector", "") or "")
        if selector:
            direct = await self.input_text(selector, value, timeout=timeout)
            if direct.success:
                return direct

        for placeholder in [info.get("placeholder", ""), info.get("label", ""), info.get("text", "")]:
            key = str(placeholder or "").strip()[:80]
            if not key:
                continue
            by_placeholder = await self.fill_by_placeholder(key, value, timeout=timeout)
            if by_placeholder.success:
                return by_placeholder
            by_label = await self.fill_by_label(key, value, timeout=timeout)
            if by_label.success:
                return by_label

        # 注意：不在这里做 type_text 回退，避免和调用方的 direct_type 策略重复
        # type_text 是追加模式，重复调用会导致文字翻倍（如 "openclawopenclaw"）
        return ToolkitResult(success=False, error=f"failed to input ref: {ref}")

    async def select_ref(self, ref: str, value: str, timeout: int = None) -> ToolkitResult:
        info = self.resolve_ref(ref)
        selector = str(info.get("selector", "") or "")
        if not selector:
            return ToolkitResult(success=False, error=f"unknown ref: {ref}")
        return await self.select_option(selector, value, timeout=timeout)

    async def wait_for_url_change(self, previous_url: str, timeout: int = None) -> ToolkitResult:
        try:
            timeout_ms = timeout if timeout is not None else settings.BROWSER_NAVIGATION_TIMEOUT
            await self._page.wait_for_function(
                "(expected) => window.location.href !== expected",
                previous_url,
                timeout=self._timeout(timeout_ms),
            )
            return ToolkitResult(success=True, data=self._page.url)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def wait_for_text_appear(self, text: str, timeout: int = None) -> ToolkitResult:
        try:
            timeout_ms = timeout if timeout is not None else settings.BROWSER_SELECTOR_TIMEOUT
            await self._page.wait_for_function(
                "(expected) => document.body && document.body.innerText && document.body.innerText.includes(expected)",
                text,
                timeout=self._timeout(timeout_ms),
            )
            return ToolkitResult(success=True)
        except Exception as e:
            return ToolkitResult(success=False, error=str(e))

    async def wait_for_page_type_change(self, previous_page_type: str, timeout: int = None) -> ToolkitResult:
        timeout_ms = timeout if timeout is not None else settings.BROWSER_NAVIGATION_TIMEOUT
        deadline = asyncio.get_running_loop().time() + (self._timeout(timeout_ms) / 1000)
        while asyncio.get_running_loop().time() < deadline:
            snapshot = await self.semantic_snapshot()
            if snapshot.success:
                current_type = str((snapshot.data or {}).get("page_type", "") or "")
                if current_type and current_type != previous_page_type:
                    return ToolkitResult(success=True, data=current_type)
            await asyncio.sleep(0.2)
        return ToolkitResult(success=False, error="page type unchanged")
