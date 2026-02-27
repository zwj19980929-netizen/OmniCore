"""
OmniCore 智能 Web Worker Agent
自适应网页爬取：自主搜索、页面理解、反爬应对、自动导航、验证码处理
"""
import asyncio
import random
import re
from typing import Dict, Any, List, Optional
from playwright.async_api import async_playwright, Browser, Page, Playwright

from core.state import OmniCoreState, TaskItem
from core.llm import LLMClient
from utils.logger import log_agent_action, logger, log_success, log_error, log_warning
from utils.captcha_solver import CaptchaSolver

# 延迟导入避免循环引用（agents/__init__.py → web_worker → core → graph → agents）
def _import_paod():
    from agents.paod import (
        classify_failure, make_trace_step, evaluate_success_criteria,
        execute_fallback, MAX_FALLBACK_ATTEMPTS,
    )
    return classify_failure, make_trace_step, evaluate_success_criteria, execute_fallback, MAX_FALLBACK_ATTEMPTS


# URL 和导航分析提示词
URL_ANALYSIS_PROMPT = """你是一个智能网站导航专家。根据用户任务，推理出最佳的目标 URL。

## 用户任务
{task_description}

## 你的工作方式
1. 先理解用户到底想访问什么网站、获取什么数据
2. 根据你的知识推理出最可能的 URL（注意区分名称相似但完全不同的网站）
3. 如果你不确定具体 URL，设置 need_search 为 true，让系统通过搜索引擎查找
4. 优先给出具体的数据列表页 URL，而不是网站首页

## 返回 JSON
```json
{{
    "url": "最可能包含目标数据的完整 URL",
    "backup_urls": ["备选 URL 列表"],
    "need_search": false,
    "search_query": "如果 need_search 为 true，这里填搜索词"
}}
```

重要：如果你对 URL 没有把握，宁可设置 need_search=true 让搜索引擎帮忙，也不要瞎猜。
"""

# 页面分析提示词
PAGE_ANALYSIS_PROMPT = """你是一个网页结构分析专家。请分析以下 HTML，找出目标数据的 CSS 选择器。

## 任务目标
{task_description}

## 页面 HTML (已截取关键部分)
```html
{html_content}
```

## 页面当前 URL
{current_url}

## 你的工作方式
1. 仔细阅读 HTML 结构，理解页面布局
2. 找出包含目标数据的重复元素（列表项、表格行等）
3. 为每个需要提取的字段确定精确的 CSS 选择器
4. 如果页面结构不明确，给出你最有把握的选择器

返回 JSON 格式：
```json
{{
    "success": true,
    "item_selector": "每一条数据项的选择器（如 tr, li, div.item 等）",
    "fields": {{
        "title": "标题文本的选择器（相对于 item）",
        "link": "链接的选择器（相对于 item，a 标签会自动提取 href）",
        "date": "日期的选择器（可选）",
        "severity": "严重程度/等级的选择器（可选）",
        "id": "编号/ID 的选择器（可选）"
    }},
    "need_click_first": false,
    "click_selector": "如果需要先点击某元素才能看到数据，填写选择器",
    "notes": "其他注意事项"
}}
```

注意：
- item_selector 应该能选中多个重复的数据项
- fields 中的选择器是相对于每个 item 的
- 只填你在 HTML 中确实看到的选择器，看不到的字段留空字符串
"""


class WebWorker:
    """
    智能 Web Worker Agent
    具备自主搜索、页面理解、反爬应对能力
    """

    def __init__(self, llm_client: LLMClient = None):
        self.name = "WebWorker"
        self.llm = llm_client or LLMClient()
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self.captcha_solver = CaptchaSolver()

    async def _ensure_browser(self, headless: bool = True) -> Browser:
        """启动浏览器，配置反检测参数"""
        if self._browser is not None and self._browser.is_connected():
            return self._browser
        # 关闭旧实例
        await self._close_browser()
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=headless,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--no-sandbox',
            ]
        )
        self._headless = headless
        return self._browser

    async def _restart_browser_visible(self):
        """反爬触发时，切换到有头模式重启浏览器"""
        log_agent_action(self.name, "检测到反爬，切换到有头浏览器模式")
        await self._close_browser()
        await self._ensure_browser(headless=False)

    async def _create_stealth_page(self) -> Page:
        """创建具有反检测能力的页面"""
        browser = await self._ensure_browser()
        context = await browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            locale='zh-CN',
            timezone_id='Asia/Shanghai',
        )

        page = await context.new_page()

        # 注入反检测脚本
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
            window.chrome = { runtime: {} };
        """)

        return page

    async def _human_like_delay(self, min_ms: int = 500, max_ms: int = 2000):
        """模拟人类操作延迟"""
        delay = random.randint(min_ms, max_ms) / 1000
        await asyncio.sleep(delay)

    async def _scroll_page(self, page: Page):
        """模拟人类滚动页面"""
        for _ in range(random.randint(2, 4)):
            await page.mouse.wheel(0, random.randint(200, 500))
            await self._human_like_delay(300, 800)

    async def _close_browser(self):
        """关闭浏览器"""
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def determine_target_url(self, task_description: str) -> Dict[str, Any]:
        """
        用 LLM 分析任务，确定目标 URL
        """
        log_agent_action(self.name, "分析目标 URL", task_description[:50])

        response = self.llm.chat_with_system(
            system_prompt=URL_ANALYSIS_PROMPT.format(task_description=task_description),
            user_message="请分析应该访问哪个 URL",
            temperature=0.2,
            json_mode=True,
        )

        try:
            result = self.llm.parse_json_response(response)
            log_agent_action(self.name, f"目标 URL", result.get("url", "未知"))
            return result
        except Exception as e:
            log_error(f"URL 分析失败: {e}")
            return {"url": "", "need_search": True, "search_query": task_description}

    async def search_for_url(self, query: str) -> Optional[str]:
        """通过搜索引擎查找目标网站"""
        log_agent_action(self.name, "搜索目标网站", query)

        page = await self._create_stealth_page()
        try:
            search_url = f"https://www.bing.com/search?q={query}"
            await page.goto(search_url, wait_until="domcontentloaded")
            await self._human_like_delay()

            results = await page.query_selector_all("li.b_algo h2 a")
            if results:
                href = await results[0].get_attribute("href")
                log_success(f"找到目标网站: {href}")
                return href
        except Exception as e:
            log_error(f"搜索失败: {e}")
        finally:
            await page.close()

        return None

    async def explore_for_data_page(self, page: Page, task_description: str) -> Optional[str]:
        """
        当前页面没有目标数据时，分析页面上的导航链接，找到数据所在的子页面。
        返回最可能包含数据的 URL，找不到则返回 None。
        """
        log_agent_action(self.name, "探索页面导航，寻找数据页面")

        # 提取页面上所有链接
        links = await page.evaluate("""() => {
            const anchors = document.querySelectorAll('a[href]');
            const results = [];
            for (const a of anchors) {
                const href = a.href;
                const text = a.innerText.trim();
                if (href && text && text.length < 50 && !href.startsWith('javascript:')) {
                    results.push({text, href});
                }
            }
            return results.slice(0, 50);
        }""")

        if not links:
            return None

        links_text = "\n".join([f"- [{l['text']}]({l['href']})" for l in links])

        response = self.llm.chat_with_system(
            system_prompt=f"""你是一个网页导航专家。用户想要获取特定数据，但当前页面是网站首页或非数据页。
请从下面的链接列表中，找出最可能包含目标数据的链接。

## 用户任务
{task_description}

## 当前页面 URL
{page.url}

## 页面上的链接
{links_text}

返回 JSON：
```json
{{"target_url": "最可能包含数据的链接URL", "reasoning": "为什么选这个链接"}}
```

如果没有合适的链接，target_url 设为空字符串。""",
            user_message="请分析哪个链接最可能包含目标数据",
            temperature=0.2,
            json_mode=True,
        )

        try:
            result = self.llm.parse_json_response(response)
            target = result.get("target_url", "")
            if target:
                log_agent_action(self.name, "找到数据页面", target[:80])
            return target or None
        except:
            return None

    def validate_data_quality(self, data: List[Dict], task_description: str, limit: int) -> Dict[str, Any]:
        """
        让 LLM 判断抓到的数据是否符合任务要求。
        返回 {"valid": bool, "reason": str, "suggestion": str}
        """
        if not data:
            return {"valid": False, "reason": "数据为空", "suggestion": "换页面或换选择器"}

        sample = data[:3]
        sample_str = json.dumps(sample, ensure_ascii=False, default=str)[:1500]

        response = self.llm.chat_with_system(
            system_prompt="""你是一个数据质量审查专家。判断抓取到的数据是否符合用户的任务要求。

请根据用户的任务描述，自主判断：
1. 抓到的数据和用户想要的是同一类东西吗？
2. 数据的关键信息是否足够？
3. 如果数据不对，你觉得应该去哪里找才对？

返回 JSON：
```json
{
    "valid": true,
    "reason": "判断理由",
    "suggestion": "如果数据不对，建议下一步怎么做"
}
```""",
            user_message=f"任务：{task_description}\n\n抓到的数据样本（前3条）：\n{sample_str}\n\n共抓到 {len(data)} 条，要求 {limit} 条",
            temperature=0.2,
            json_mode=True,
        )

        try:
            return self.llm.parse_json_response(response)
        except:
            return {"valid": True, "reason": "审查失败，默认通过", "suggestion": ""}

    async def analyze_page_structure(
        self,
        page: Page,
        task_description: str,
    ) -> Dict[str, Any]:
        """用 LLM 分析页面结构，生成选择器"""
        log_agent_action(self.name, "分析页面结构")

        current_url = page.url
        html = await page.content()

        # 清理 HTML
        html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r'<!--.*?-->', '', html, flags=re.DOTALL)
        html = re.sub(r'\s+', ' ', html)

        # 截取
        if len(html) > 15000:
            html = html[:15000] + "\n... (truncated)"

        response = self.llm.chat_with_system(
            system_prompt=PAGE_ANALYSIS_PROMPT.format(
                task_description=task_description,
                html_content=html,
                current_url=current_url,
            ),
            user_message="请分析页面结构并返回选择器配置",
            temperature=0.2,
            max_tokens=4096,
            json_mode=True,
        )

        try:
            config = self.llm.parse_json_response(response)
            log_agent_action(self.name, "页面分析完成", f"item_selector: {config.get('item_selector', 'N/A')}")
            return config
        except Exception as e:
            log_error(f"页面分析失败: {e}")
            return {"success": False, "error": str(e)}

    async def extract_data_with_selectors(
        self,
        page: Page,
        config: Dict[str, Any],
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """根据选择器配置提取数据"""
        results = []

        item_selector = config.get("item_selector", "")
        fields = config.get("fields", {})

        if not item_selector:
            log_warning("未找到有效的项目选择器")
            return results

        log_agent_action(self.name, f"提取数据", f"选择器: {item_selector}")

        # 如果需要先点击某元素
        if config.get("need_click_first") and config.get("click_selector"):
            try:
                await page.click(config["click_selector"])
                await self._human_like_delay(1000, 2000)
            except Exception as e:
                log_warning(f"点击元素失败: {e}")

        try:
            items = await page.query_selector_all(item_selector)
            log_agent_action(self.name, f"找到 {len(items)} 个元素")

            for i, item in enumerate(items[:limit]):
                data = {"index": i + 1}

                for field_name, selector in fields.items():
                    if not selector:
                        continue

                    try:
                        elem = await item.query_selector(selector)
                        if elem:
                            tag = await elem.evaluate("el => el.tagName.toLowerCase()")
                            text = (await elem.inner_text()).strip()

                            if tag == "a":
                                data[field_name] = text
                                href = await elem.get_attribute("href")
                                if href:
                                    if field_name == "title":
                                        data["link"] = href
                                    else:
                                        data[f"{field_name}_link"] = href
                            else:
                                data[field_name] = text
                    except Exception as e:
                        logger.debug(f"提取字段 {field_name} 失败: {e}")

                # 只保留有实际内容的数据
                if len([v for k, v in data.items() if k != "index" and v]) > 0:
                    results.append(data)

        except Exception as e:
            log_error(f"数据提取失败: {e}")

        return results

    async def smart_scrape(
        self,
        url: Optional[str],
        task_description: str,
        limit: int = 10,
    ) -> Dict[str, Any]:
        """智能爬取：自动分析页面并提取数据"""
        log_agent_action(self.name, "开始智能爬取", task_description[:50])

        # Step 1: 用 LLM 分析确定最佳目标 URL（始终执行，因为 LLM 知道具体的列表页路径）
        url_info = await self.determine_target_url(task_description)
        best_url = url_info.get("url", "")

        # 优先使用 LLM 推荐的 URL
        if best_url:
            url = best_url
        elif not url and url_info.get("need_search"):
            query = url_info.get("search_query", task_description)
            url = await self.search_for_url(query)

        if not url:
            return {
                "success": False,
                "error": "无法确定目标网站 URL",
                "data": [],
            }

        page = await self._create_stealth_page()

        # 用于捕获 SPA 页面的 API 响应数据
        api_responses = []

        async def _capture_api_response(response):
            """拦截 XHR/Fetch API 响应，捕获 JSON 数据"""
            try:
                content_type = response.headers.get("content-type", "")
                if "json" in content_type and response.status == 200:
                    body = await response.json()
                    # 只保留包含列表数据的响应（通常是数组或包含数组的对象）
                    if isinstance(body, list) and len(body) > 0:
                        api_responses.append({"url": response.url, "data": body})
                    elif isinstance(body, dict):
                        # 查找响应中的列表字段
                        for key, val in body.items():
                            if isinstance(val, list) and len(val) >= 3 and isinstance(val[0], dict):
                                api_responses.append({"url": response.url, "data": val, "key": key})
            except:
                pass

        page.on("response", _capture_api_response)

        try:
            # Step 2: 访问页面（带重试 + 反爬自适应）
            log_agent_action(self.name, "访问页面", url)
            page_loaded = False
            for goto_attempt in range(3):
                try:
                    wait_strategy = "domcontentloaded" if goto_attempt < 2 else "commit"
                    await page.goto(url, wait_until=wait_strategy, timeout=45000)
                    page_loaded = True
                    break
                except Exception as goto_err:
                    if goto_attempt == 0:
                        # 第一次失败：切换到有头浏览器模式绕过反爬
                        log_warning(f"页面加载失败，切换到有头浏览器模式重试...")
                        await page.close()
                        await self._restart_browser_visible()
                        page = await self._create_stealth_page()
                        page.on("response", _capture_api_response)
                    elif goto_attempt == 1:
                        log_warning(f"第 3 次尝试，降低等待要求...")
                    else:
                        raise goto_err

            if not page_loaded:
                return {"success": False, "error": "页面加载失败（可能被反爬拦截）", "data": [], "url": url}

            await self._human_like_delay(1500, 3000)

            # 等待页面稳定
            try:
                await page.wait_for_load_state("networkidle", timeout=15000)
            except:
                pass  # 超时也继续

            # 等待动态内容渲染（SPA 页面需要额外等待）
            try:
                await page.wait_for_selector("table, .list, ul li, [class*='list'], [class*='item'], .el-table", timeout=10000)
            except:
                # 没找到常见列表元素，多等一会儿让 JS 执行完
                await self._human_like_delay(3000, 5000)

            # Step 2.5: 检测并处理验证码
            try:
                captcha_detection = await self.captcha_solver.detect_captcha(page)
            except Exception as e:
                # 页面可能已经导航，不是验证码页面
                captcha_detection = {"has_captcha": False}

            if captcha_detection["has_captcha"]:
                log_agent_action(self.name, "检测到验证码，尝试自动处理")
                captcha_solved = await self.captcha_solver.solve(page, max_retries=5)
                if captcha_solved:
                    # 验证码通过后，等待页面完全加载
                    await self._human_like_delay(2000, 3000)
                    try:
                        await page.wait_for_load_state("domcontentloaded", timeout=10000)
                    except:
                        pass
                    try:
                        await page.wait_for_load_state("networkidle", timeout=10000)
                    except:
                        pass
                else:
                    return {
                        "success": False,
                        "error": "验证码处理失败",
                        "data": [],
                        "url": url,
                    }

            # 模拟人类行为
            await self._scroll_page(page)

            # Step 3-5: 提取数据（带自主探索重试）
            max_attempts = 3
            data = []

            for attempt in range(max_attempts):
                if attempt > 0:
                    log_agent_action(self.name, f"第 {attempt + 1} 次尝试提取数据", page.url[:60])

                # Step 3: 分析页面结构
                config = await self.analyze_page_structure(page, task_description)

                if config.get("item_selector"):
                    # Step 4: 用选择器提取数据
                    data = await self.extract_data_with_selectors(page, config, limit)

                if not data:
                    # 尝试通用选择器
                    common_selectors = [
                        "table tbody tr", "table tr:not(:first-child)",
                        ".list-item", ".item", "ul.list li",
                        ".el-table__row", "[class*='vuln'] tr", "[class*='list'] li",
                    ]
                    for sel in common_selectors:
                        items = await page.query_selector_all(sel)
                        if len(items) >= 3:
                            config["item_selector"] = sel
                            config["fields"] = {"title": "td:nth-child(2)", "link": "a"}
                            data = await self.extract_data_with_selectors(page, config, limit)
                            if data:
                                break

                if not data and api_responses:
                    # 尝试使用拦截到的 API 数据
                    best_api = max(api_responses, key=lambda x: len(x["data"]))
                    data = best_api["data"][:limit]
                    log_success(f"从 API 响应中提取到 {len(data)} 条数据")

                if data:
                    # 数据自检：验证抓到的数据是否符合任务要求
                    quality = self.validate_data_quality(data, task_description, limit)
                    if quality.get("valid"):
                        log_success(f"数据质量验证通过: {quality.get('reason', '')[:50]}")
                        break
                    else:
                        log_warning(f"数据不符合要求: {quality.get('reason', '')[:80]}")
                        log_agent_action(self.name, "建议", quality.get("suggestion", "")[:80])
                        data = []  # 清空，继续尝试

                # 数据为空或不合格，尝试探索页面导航找到正确的数据页
                if attempt < max_attempts - 1:
                    log_agent_action(self.name, "当前页面无数据，尝试探索导航链接")
                    next_url = await self.explore_for_data_page(page, task_description)
                    if next_url and next_url != page.url:
                        api_responses.clear()
                        await page.goto(next_url, wait_until="domcontentloaded", timeout=30000)
                        await self._human_like_delay(2000, 4000)
                        try:
                            await page.wait_for_load_state("networkidle", timeout=15000)
                        except:
                            pass
                        # 等待动态内容
                        try:
                            await page.wait_for_selector("table, .list, ul li, [class*='list'], [class*='item'], .el-table", timeout=10000)
                        except:
                            await self._human_like_delay(3000, 5000)
                        await self._scroll_page(page)
                    else:
                        break  # 没有新页面可探索了

            # 处理相对链接
            base_url = "/".join(url.split("/")[:3])
            for item in data:
                for key in ["link", "title_link", "id_link"]:
                    if item.get(key) and not item[key].startswith("http"):
                        if item[key].startswith("/"):
                            item[key] = base_url + item[key]
                        else:
                            item[key] = url.rsplit("/", 1)[0] + "/" + item[key]

            if data:
                log_success(f"最终成功提取 {len(data)} 条数据")
            else:
                log_warning("所有尝试均未能提取到数据")

            return {
                "success": len(data) > 0,
                "data": data,
                "count": len(data),
                "source": url,
                "selectors_used": config,
            }

        except Exception as e:
            log_error(f"爬取失败: {e}")
            return {
                "success": False,
                "error": str(e),
                "data": [],
                "url": url,
            }

        finally:
            await page.close()

    async def execute_async(
        self,
        task: TaskItem,
        shared_memory: Dict[str, Any],
    ) -> Dict[str, Any]:
        """异步执行 Web 任务（PAOD 微反思包装）"""
        classify_failure, make_trace_step, evaluate_success_criteria, execute_fallback, MAX_FALLBACK_ATTEMPTS = _import_paod()

        params = task["params"]
        url = params.get("url", "")
        limit = params.get("limit", 10)
        task_description = task["description"]
        trace: List[Dict[str, Any]] = task.get("execution_trace", [])
        step_no = len(trace) + 1

        # --- 主执行 ---
        trace.append(make_trace_step(step_no, "执行 smart_scrape", f"url={url}, limit={limit}", "", ""))
        result = await self.smart_scrape(url, task_description, limit)
        trace[-1]["observation"] = f"success={result.get('success')}, count={result.get('count', 0)}"

        # --- 评估 success_criteria ---
        criteria = task.get("success_criteria", [])
        if result.get("success") and evaluate_success_criteria(criteria, result):
            trace[-1]["decision"] = "criteria_met → done"
            task["execution_trace"] = trace
            return result

        # --- 不满足 → 尝试 fallback ---
        trace[-1]["decision"] = "criteria_not_met → try fallback"
        fb_index = 0
        while fb_index < MAX_FALLBACK_ATTEMPTS:
            fb = execute_fallback(task, fb_index, shared_memory)
            if fb is None:
                break
            fb_index += 1
            step_no += 1

            if fb["action"] == "switch_worker":
                # 返回特殊信号，让 process() 处理 worker 切换
                trace.append(make_trace_step(step_no, f"switch_worker → {fb['target']}", "signal", "", "escalate"))
                task["execution_trace"] = trace
                result["_switch_worker"] = fb["target"]
                result["_switch_params"] = fb.get("param_patch", {})
                return result

            # retry with param_patch
            patch = fb.get("param_patch", {})
            patched_params = {**params, **patch}
            retry_url = patched_params.get("url", url)
            retry_limit = patched_params.get("limit", limit)
            trace.append(make_trace_step(step_no, f"retry #{fb_index}", f"url={retry_url}, patch={patch}", "", ""))

            result = await self.smart_scrape(retry_url, task_description, retry_limit)
            trace[-1]["observation"] = f"success={result.get('success')}, count={result.get('count', 0)}"

            if result.get("success") and evaluate_success_criteria(criteria, result):
                trace[-1]["decision"] = "criteria_met → done"
                task["execution_trace"] = trace
                return result
            trace[-1]["decision"] = "still_failing → next fallback"

        # --- 所有 fallback 耗尽 ---
        if not result.get("success"):
            task["failure_type"] = classify_failure(result.get("error", ""))
        task["execution_trace"] = trace
        return result

    def execute(self, task: TaskItem, shared_memory: Dict[str, Any]) -> Dict[str, Any]:
        """同步执行入口"""
        return asyncio.run(self.execute_async(task, shared_memory))

    def process(self, state: OmniCoreState) -> OmniCoreState:
        """LangGraph 节点函数（PAOD 增强）"""
        classify_failure = _import_paod()[0]

        async def _process_all():
            for idx, task in enumerate(state["task_queue"]):
                if task["task_type"] == "web_worker" and task["status"] == "pending":
                    state["task_queue"][idx]["status"] = "running"

                    result = await self.execute_async(task, state["shared_memory"])

                    # 检测 switch_worker 信号
                    if isinstance(result, dict) and result.get("_switch_worker"):
                        target = result.pop("_switch_worker")
                        patch = result.pop("_switch_params", {})
                        log_warning(f"WebWorker 触发 switch_worker → {target}")
                        state["task_queue"][idx]["task_type"] = target
                        state["task_queue"][idx]["params"].update(patch)
                        state["task_queue"][idx]["status"] = "pending"
                        continue

                    state["task_queue"][idx]["status"] = (
                        "completed" if result.get("success") else "failed"
                    )
                    state["task_queue"][idx]["result"] = result

                    if result.get("success") and result.get("data"):
                        state["shared_memory"][task["task_id"]] = result["data"]

                    if not result.get("success"):
                        state["task_queue"][idx]["failure_type"] = classify_failure(
                            result.get("error", "")
                        )
                        state["error_trace"] = result.get("error", "未知错误")

            await self._close_browser()

        asyncio.run(_process_all())
        return state
