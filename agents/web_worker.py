"""
OmniCore 智能 Web Worker Agent
自适应网页爬取：自主搜索、页面理解、反爬应对、自动导航、验证码处理
所有浏览器操作通过 BrowserToolkit 完成。
"""
import asyncio
import json
import random
import re
from typing import Dict, Any, List, Optional
from urllib.parse import urljoin
import requests

from core.state import OmniCoreState, TaskItem
from core.llm import LLMClient
from utils.logger import log_agent_action, logger, log_success, log_error, log_warning
from utils.browser_toolkit import BrowserToolkit, ToolkitResult
from utils.retry import async_retry, is_retryable
from config.settings import settings

# ==================== 预编译正则表达式 ====================
# HTML 清理相关
RE_SCRIPT_TAG = re.compile(r"<script[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
RE_STYLE_TAG = re.compile(r"<style[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
RE_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
RE_HTML_TAG = re.compile(r"<[^>]+>")
RE_WHITESPACE = re.compile(r"\s+")

# 链接提取相关
RE_HEADING_WITH_LINK = re.compile(
    r"<(h1|h2|h3)[^>]*>.*?<a[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>.*?</\1>",
    re.DOTALL | re.IGNORECASE,
)
RE_ANCHOR_TAG = re.compile(
    r"<a[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
    re.DOTALL | re.IGNORECASE,
)

# 文本块提取相关
RE_PARAGRAPH_BLOCK = re.compile(r"<(p|li)[^>]*>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
RE_CONTENT_BLOCK = re.compile(
    r"<(main|article|section|div)[^>]*>(.*?)</\1>", re.DOTALL | re.IGNORECASE
)

# 分词相关
RE_TOKEN_SPLIT = re.compile(r"[\s,.;:|/\\]+")

# ==================== 延迟导入 ====================
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
    所有浏览器操作通过 BrowserToolkit 完成。
    """

    def __init__(self, llm_client: LLMClient = None):
        self.name = "WebWorker"
        self.llm = llm_client or LLMClient()
        self.fast_mode = settings.BROWSER_FAST_MODE
        self.block_heavy_resources = settings.BLOCK_HEAVY_RESOURCES
        self.static_fetch_enabled = settings.STATIC_FETCH_ENABLED

    def _create_toolkit(self, headless: bool = True) -> BrowserToolkit:
        return BrowserToolkit(
            headless=headless,
            fast_mode=self.fast_mode,
            block_heavy_resources=self.block_heavy_resources,
        )

    # ── URL determination (pure LLM, no browser) ────────────

    async def determine_target_url(self, task_description: str) -> Dict[str, Any]:
        log_agent_action(self.name, "分析目标 URL", task_description[:50])
        response = self.llm.chat_with_system(
            system_prompt=URL_ANALYSIS_PROMPT.format(task_description=task_description),
            user_message="请分析应该访问哪个 URL",
            temperature=0.2, json_mode=True,
        )
        try:
            result = self.llm.parse_json_response(response)
            log_agent_action(self.name, "目标 URL", result.get("url", "未知"))
            return result
        except Exception as e:
            log_error(f"URL 分析失败: {e}")
            return {"url": "", "need_search": True, "search_query": task_description}

    # ── static fetch (no browser) ──────────────────────────────

    def _can_use_static_fetch(self, task_description: str, url: Optional[str]) -> bool:
        if not self.static_fetch_enabled or not url:
            return False
        desc = (task_description or "").lower()
        interactive_keywords = [
            "登录", "注册", "填写", "点击", "提交", "支付", "购买",
            "login", "sign in", "register", "click", "submit", "checkout", "buy",
        ]
        return not any(k in desc for k in interactive_keywords)

    def _clean_html_text(self, raw_html: str) -> str:
        html = RE_SCRIPT_TAG.sub("", raw_html)
        html = RE_STYLE_TAG.sub("", html)
        html = RE_HTML_COMMENT.sub("", html)
        return html

    def _strip_tags(self, text: str) -> str:
        text = RE_HTML_TAG.sub(" ", text)
        text = RE_WHITESPACE.sub(" ", text)
        return text.strip()

    def _is_noise_link(self, text: str, href: str) -> bool:
        t = (text or "").strip().lower()
        h = (href or "").strip().lower()
        if not t or len(t) < 4:
            return True
        if h.startswith("javascript:") or h.startswith("mailto:") or h == "#" or not h:
            return True
        noise_keywords = [
            "login", "register", "privacy", "terms", "cookie", "help", "about",
            "登录", "注册", "隐私", "条款", "帮助", "关于", "更多",
        ]
        return any(k in t for k in noise_keywords)

    def _score_static_link(self, text: str, href: str, task_description: str) -> int:
        score = 0
        t = text.lower()
        task = (task_description or "").lower()
        if len(text) >= 12:
            score += 2
        if href.startswith("http"):
            score += 1
        for token in RE_TOKEN_SPLIT.split(task):
            token = token.strip()
            if len(token) >= 3 and token in t:
                score += 2
        return score

    def _prefers_static_text(self, task_description: str) -> bool:
        desc = (task_description or "").lower()
        text_keywords = [
            "read", "summary", "summarize", "extract text", "article", "content",
            "读取", "总结", "概述", "正文", "文章", "内容",
        ]
        return any(k in desc for k in text_keywords)

    def _extract_static_links(self, html: str, base_url: str, task_description: str, limit: int) -> List[Dict[str, Any]]:
        cleaned = self._clean_html_text(html)
        candidates: List[Dict[str, Any]] = []
        seen = set()

        def _append(href: str, raw_text: str):
            text = self._strip_tags(raw_text)
            full_href = urljoin(base_url, href.strip())
            if self._is_noise_link(text, full_href):
                return
            key = (text[:80], full_href)
            if key in seen:
                return
            seen.add(key)
            candidates.append({
                "title": text[:160], "link": full_href,
                "_score": self._score_static_link(text, full_href, task_description),
                "_order": len(candidates),
            })

        for match in RE_HEADING_WITH_LINK.finditer(cleaned):
            _append(match.group(2), match.group(3))
        if len(candidates) < limit:
            for match in RE_ANCHOR_TAG.finditer(cleaned):
                _append(match.group(1), match.group(2))
                if len(candidates) >= max(limit * 4, 20):
                    break
        candidates.sort(key=lambda x: (-x["_score"], x["_order"]))
        results = candidates[:limit]
        for item in results:
            item.pop("_score", None)
            item.pop("_order", None)
        return results

    def _extract_static_text_blocks(self, html: str, limit: int) -> List[Dict[str, Any]]:
        cleaned = self._clean_html_text(html)
        blocks: List[Dict[str, Any]] = []
        seen = set()
        patterns = [RE_PARAGRAPH_BLOCK, RE_CONTENT_BLOCK]
        for pattern in patterns:
            for match in pattern.finditer(cleaned):
                text = self._strip_tags(match.group(2))
                if len(text) < 40:
                    continue
                key = RE_WHITESPACE.sub(" ", text).strip().lower()[:120]
                if key in seen:
                    continue
                seen.add(key)
                blocks.append({"text": text[:400]})
                if len(blocks) >= limit:
                    return blocks
        return blocks

    def _static_fetch(self, url: str, task_description: str, limit: int) -> Dict[str, Any]:
        try:
            resp = requests.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                },
                timeout=15,
            )
            resp.raise_for_status()
            content_type = resp.headers.get("content-type", "").lower()
            if "html" not in content_type:
                return {"success": False, "error": f"非HTML响应: {content_type}", "data": [], "url": url}
            html = resp.text
            text_data = self._extract_static_text_blocks(html, max(3, min(limit, 6)))
            link_data = self._extract_static_links(html, url, task_description, limit)
            if self._prefers_static_text(task_description):
                data = text_data or link_data
            else:
                data = link_data or text_data
            if not data:
                return {"success": False, "error": "静态抓取未提取到有效内容", "data": [], "url": url}
            return {"success": True, "data": data, "count": len(data), "source": url,
                    "mode": "static_fetch_text" if "text" in data[0] else "static_fetch"}
        except Exception as e:
            return {"success": False, "error": str(e), "data": [], "url": url}

    # ── data quality validation (pure LLM) ───────────────────

    def validate_data_quality(self, data: List[Dict], task_description: str, limit: int) -> Dict[str, Any]:
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
            temperature=0.2, json_mode=True,
        )
        try:
            return self.llm.parse_json_response(response)
        except Exception:
            return {"valid": True, "reason": "审查失败，默认通过", "suggestion": ""}

    # ── search (uses toolkit) ──────────────────────────────────

    async def search_for_url(self, query: str) -> Optional[str]:
        results = await self.search_for_urls(query, max_results=1)
        return results[0] if results else None

    async def search_for_urls(self, query: str, max_results: int = 5, tk: BrowserToolkit = None) -> List[str]:
        log_agent_action(self.name, "搜索候选网站", query[:60])
        own_tk = tk is None
        if own_tk:
            tk = self._create_toolkit()
            await tk.create_page()
        urls = []
        try:
            search_url = f"https://www.bing.com/search?q={query}"
            await tk.goto(search_url)
            await tk.human_delay(500, 2000)

            r = await tk.query_all("li.b_algo h2 a")
            if not r.success:
                return urls
            for elem in (r.data or [])[:max_results * 2]:
                href = await elem.get_attribute("href")
                if not href:
                    continue
                if any(tracker in href for tracker in [
                    "bing.com/ck/a", "baidu.com/link", "google.com/url",
                    "sogou.com/link", "so.com/link",
                ]):
                    import urllib.parse
                    parsed = urllib.parse.urlparse(href)
                    qs = urllib.parse.parse_qs(parsed.query)
                    real_url = qs.get("u", qs.get("url", [None]))[0] if qs else None
                    if real_url and real_url.startswith("http"):
                        href = real_url
                    else:
                        continue
                if href.startswith("http") and href not in urls:
                    urls.append(href)
                if len(urls) >= max_results:
                    break
            if urls:
                log_success(f"搜索到 {len(urls)} 个候选网站")
            else:
                log_warning("搜索未找到结果")
        except Exception as e:
            log_error(f"搜索失败: {e}")
        finally:
            if own_tk:
                await tk.close()
        return urls

    async def explore_for_data_page(self, tk: BrowserToolkit, task_description: str) -> Optional[str]:
        log_agent_action(self.name, "探索页面导航，寻找数据页面")
        r = await tk.evaluate_js("""() => {
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
        links = r.data if r.success else []
        if not links:
            return None

        url_r = await tk.get_current_url()
        links_text = "\n".join([f"- [{l['text']}]({l['href']})" for l in links])
        response = self.llm.chat_with_system(
            system_prompt=f"""你是一个网页导航专家。用户想要获取特定数据，但当前页面是网站首页或非数据页。
请从下面的链接列表中，找出最可能包含目标数据的链接。

## 用户任务
{task_description}

## 当前页面 URL
{url_r.data or ""}

## 页面上的链接
{links_text}

返回 JSON：
```json
{{"target_url": "最可能包含数据的链接URL", "reasoning": "为什么选这个链接"}}
```

如果没有合适的链接，target_url 设为空字符串。""",
            user_message="请分析哪个链接最可能包含目标数据",
            temperature=0.2, json_mode=True,
        )
        try:
            result = self.llm.parse_json_response(response)
            target = result.get("target_url", "")
            if target:
                log_agent_action(self.name, "找到数据页面", target[:80])
            return target or None
        except Exception:
            return None

    # ── page analysis & extraction (uses toolkit) ─────────────

    async def analyze_page_structure(self, tk: BrowserToolkit, task_description: str) -> Dict[str, Any]:
        log_agent_action(self.name, "分析页面结构")
        url_r = await tk.get_current_url()
        html_r = await tk.get_page_html()
        html = html_r.data or ""

        html = RE_SCRIPT_TAG.sub('', html)
        html = RE_STYLE_TAG.sub('', html)
        html = RE_HTML_COMMENT.sub('', html)
        html = RE_WHITESPACE.sub(' ', html)
        if len(html) > 15000:
            html = html[:15000] + "\n... (truncated)"

        response = self.llm.chat_with_system(
            system_prompt=PAGE_ANALYSIS_PROMPT.format(
                task_description=task_description,
                html_content=html,
                current_url=url_r.data or "",
            ),
            user_message="请分析页面结构并返回选择器配置",
            temperature=0.2, max_tokens=4096, json_mode=True,
        )
        try:
            config = self.llm.parse_json_response(response)
            log_agent_action(self.name, "页面分析完成", f"item_selector: {config.get('item_selector', 'N/A')}")
            return config
        except Exception as e:
            log_error(f"页面分析失败: {e}")
            return {"success": False, "error": str(e)}

    async def extract_data_with_selectors(self, tk: BrowserToolkit, config: Dict[str, Any], limit: int = 10) -> List[Dict[str, Any]]:
        results = []
        item_selector = config.get("item_selector", "")
        fields = config.get("fields", {})
        if not item_selector:
            log_warning("未找到有效的项目选择器")
            return results

        log_agent_action(self.name, "提取数据", f"选择器: {item_selector}")

        if config.get("need_click_first") and config.get("click_selector"):
            r = await tk.click(config["click_selector"])
            if r.success:
                await tk.human_delay(1000, 2000)

        try:
            items_r = await tk.query_all(item_selector)
            items = items_r.data if items_r.success else []
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

                if len([v for k, v in data.items() if k != "index" and v]) > 0:
                    results.append(data)
        except Exception as e:
            log_error(f"数据提取失败: {e}")
        return results

    # ── smart_scrape (main entry, uses toolkit) ────────────────

    async def smart_scrape(self, url: Optional[str], task_description: str, limit: int = 10) -> Dict[str, Any]:
        log_agent_action(self.name, "开始智能爬取", task_description[:50])

        # Step 1: LLM 分析确定最佳目标 URL
        url_info = await self.determine_target_url(task_description)
        best_url = url_info.get("url", "")
        if not url and best_url:
            url = best_url
        elif not url and url_info.get("need_search"):
            query = url_info.get("search_query", task_description)
            url = await self.search_for_url(query)
        if not url:
            return {"success": False, "error": "无法确定目标网站 URL", "data": []}

        # Step 1.5: 纯读场景优先静态抓取
        if self._can_use_static_fetch(task_description, url):
            static_result = self._static_fetch(url, task_description, limit)
            if static_result.get("success"):
                log_success(f"静态抓取成功，获取 {static_result.get('count', 0)} 条数据")
                return static_result
            log_warning(f"静态抓取失败，回退浏览器模式: {static_result.get('error', '')[:80]}")

        tk = self._create_toolkit()
        await tk.create_page()

        # 用于捕获 SPA 页面的 API 响应数据
        api_responses = []

        async def _capture_api_response(response):
            try:
                content_type = response.headers.get("content-type", "")
                if "json" in content_type and response.status == 200:
                    body = await response.json()
                    if isinstance(body, list) and len(body) > 0:
                        api_responses.append({"url": response.url, "data": body})
                    elif isinstance(body, dict):
                        for key, val in body.items():
                            if isinstance(val, list) and len(val) >= 3 and isinstance(val[0], dict):
                                api_responses.append({"url": response.url, "data": val, "key": key})
            except Exception:
                pass

        if tk.page:
            tk.page.on("response", _capture_api_response)

        config = {}
        try:
            # Step 2: 访问页面（带重试 + 反爬自适应）
            log_agent_action(self.name, "访问页面", url)
            _goto_attempt = 0

            async def _rebuild_page_and_goto():
                nonlocal _goto_attempt
                _goto_attempt += 1
                if tk.page and tk.page.is_closed():
                    await tk.create_page()
                    if tk.page:
                        tk.page.on("response", _capture_api_response)
                wait_strategy = "domcontentloaded" if _goto_attempt <= 2 else "commit"
                return await tk.goto(url, wait_until=wait_strategy, timeout=45000)

            try:
                await async_retry(
                    _rebuild_page_and_goto, max_attempts=4,
                    base_delay=2.0, max_delay=15.0, caller_name=self.name,
                )
            except Exception as goto_err:
                return {"success": False, "error": f"页面加载失败: {str(goto_err)[:200]}", "data": [], "url": url}

            await tk.human_delay(180, 3000)

            # 等待页面稳定
            if self.fast_mode:
                await tk.wait_for_load("domcontentloaded", timeout=3000)
            else:
                await tk.wait_for_load("networkidle", timeout=15000)

            # 等待动态内容渲染
            await tk.wait_for_selector(
                "table, .list, ul li, [class*='list'], [class*='item'], .el-table",
                timeout=4000 if self.fast_mode else 10000,
            )

            # Step 2.5: 检测并处理验证码
            captcha_r = await tk.detect_captcha()
            if captcha_r.success and captcha_r.data and captcha_r.data.get("has_captcha"):
                log_agent_action(self.name, "检测到验证码，尝试自动处理")
                solve_r = await tk.solve_captcha(max_retries=5)
                if solve_r.success:
                    await tk.human_delay(250, 3000)
                    await tk.wait_for_load("domcontentloaded", timeout=10000)
                    await tk.wait_for_load("networkidle", timeout=10000)
                else:
                    return {"success": False, "error": "验证码处理失败", "data": [], "url": url}

            # 模拟人类滚动
            for _ in range(random.randint(1, 2) if self.fast_mode else random.randint(2, 4)):
                await tk.scroll_down(random.randint(200, 500))
                await tk.human_delay(120, 800)

            # Step 3-5: 提取数据（带自主探索重试）
            max_attempts = 3
            data = []

            for attempt in range(max_attempts):
                if attempt > 0:
                    url_r = await tk.get_current_url()
                    log_agent_action(self.name, f"第 {attempt + 1} 次尝试提取数据", (url_r.data or "")[:60])

                # Step 3: 分析页面结构
                config = await self.analyze_page_structure(tk, task_description)

                if config.get("item_selector"):
                    # Step 4: 用选择器提取数据
                    data = await self.extract_data_with_selectors(tk, config, limit)

                    # Step 4.5: 数据不够时，滚动加载更多
                    if data and len(data) < limit:
                        log_agent_action(self.name, f"数据不足 ({len(data)}/{limit})，尝试滚动加载更多")
                        last_count = len(data)
                        no_change = 0
                        for _ in range(8):
                            await tk.scroll_down(random.randint(600, 1000))
                            await tk.human_delay(300, 2000)
                            data = await self.extract_data_with_selectors(tk, config, limit)
                            if len(data) >= limit:
                                break
                            if len(data) <= last_count:
                                no_change += 1
                                if no_change >= 2:
                                    break
                            else:
                                no_change = 0
                                last_count = len(data)

                    # Step 4.6: 滚动后仍不够，尝试翻页
                    if data and len(data) < limit:
                        for _page_num in range(3):
                            if len(data) >= limit:
                                break
                            clicked = await self._try_next_page_via_toolkit(tk)
                            if not clicked:
                                break
                            for _ in range(random.randint(1, 2)):
                                await tk.scroll_down(random.randint(200, 500))
                                await tk.human_delay(120, 800)
                            page_data = await self.extract_data_with_selectors(tk, config, limit - len(data))
                            if not page_data:
                                break
                            data.extend(page_data)
                            log_agent_action(self.name, f"翻页后累计 {len(data)}/{limit} 条数据")

                if not data:
                    # 尝试通用选择器
                    common_selectors = [
                        "table tbody tr", "table tr:not(:first-child)",
                        ".list-item", ".item", "ul.list li",
                        ".el-table__row", "[class*='vuln'] tr", "[class*='list'] li",
                    ]
                    for sel in common_selectors:
                        items_r = await tk.query_all(sel)
                        if items_r.success and len(items_r.data or []) >= 3:
                            config["item_selector"] = sel
                            config["fields"] = {"title": "td:nth-child(2)", "link": "a"}
                            data = await self.extract_data_with_selectors(tk, config, limit)
                            if data:
                                break

                if not data and api_responses:
                    best_api = max(api_responses, key=lambda x: len(x["data"]))
                    data = best_api["data"][:limit]
                    log_success(f"从 API 响应中提取到 {len(data)} 条数据")

                if data:
                    quality = self.validate_data_quality(data, task_description, limit)
                    if quality.get("valid"):
                        log_success(f"数据质量验证通过: {quality.get('reason', '')[:50]}")
                        break
                    else:
                        log_warning(f"数据不符合要求: {quality.get('reason', '')[:80]}")
                        data = []

                # 探索页面导航找到正确的数据页
                if attempt < max_attempts - 1:
                    log_agent_action(self.name, "当前页面无数据，尝试探索导航链接")
                    next_url = await self.explore_for_data_page(tk, task_description)
                    url_r = await tk.get_current_url()
                    if next_url and next_url != (url_r.data or ""):
                        api_responses.clear()
                        await tk.goto(next_url, timeout=30000)
                        await tk.human_delay(200, 4000)
                        await tk.wait_for_load("networkidle", timeout=15000)
                        await tk.wait_for_selector(
                            "table, .list, ul li, [class*='list'], [class*='item'], .el-table",
                            timeout=10000,
                        )
                        for _ in range(random.randint(1, 2)):
                            await tk.scroll_down(random.randint(200, 500))
                            await tk.human_delay(120, 800)
                    else:
                        break

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
                # 换源搜索（最多尝试 2 个替代来源，防止无限循环）
                log_agent_action(self.name, "当前来源失败，尝试通过搜索引擎寻找替代来源")
                alt_urls = await self.search_for_urls(task_description, max_results=3, tk=tk)
                original_domain = "/".join(url.split("/")[:3]) if url else ""
                alt_urls = [u for u in alt_urls if original_domain not in u]

                for idx, alt_url in enumerate(alt_urls[:2], 1):
                    log_agent_action(self.name, f"尝试替代来源 ({idx}/2)", alt_url[:80])
                    try:
                        await tk.goto(alt_url, timeout=30000)
                        await tk.human_delay(500, 1500)
                        for _ in range(random.randint(1, 2)):
                            await tk.scroll_down(random.randint(200, 500))
                            await tk.human_delay(120, 800)
                        alt_config = await self.analyze_page_structure(tk, task_description)
                        if alt_config.get("item_selector"):
                            data = await self.extract_data_with_selectors(tk, alt_config, limit)
                        if data:
                            quality = self.validate_data_quality(data, task_description, limit)
                            if quality.get("valid"):
                                url = alt_url
                                config = alt_config
                                log_success(f"替代来源成功，从 {alt_url[:60]} 提取到 {len(data)} 条数据")
                                break
                            else:
                                log_warning(f"替代来源 {idx} 数据质量不符合要求")
                                data = []
                        else:
                            log_warning(f"替代来源 {idx} 未能提取到数据")
                    except Exception as alt_err:
                        log_warning(f"替代来源 {idx} 访问失败: {str(alt_err)[:80]}")
                        continue
                if not data:
                    log_warning("所有来源（包括 2 个替代来源）均未能提取到数据")

            return {
                "success": len(data) > 0, "data": data, "count": len(data),
                "source": url, "selectors_used": config,
            }
        except Exception as e:
            log_error(f"爬取失败: {e}")
            return {"success": False, "error": str(e), "data": [], "url": url}
        finally:
            await tk.close()

    async def scrape_hackernews(self, limit: int = 5) -> Dict[str, Any]:
        """兼容旧测试/旧调用方的 Hacker News 抓取入口。"""
        result = await self.smart_scrape(
            url="https://news.ycombinator.com",
            task_description=f"抓取 Hacker News 首页前 {limit} 条新闻的标题和链接",
            limit=limit,
        )
        if result.get("success"):
            for idx, item in enumerate(result.get("data", []), 1):
                if isinstance(item, dict):
                    item.setdefault("rank", idx)
        return result

    async def _try_next_page_via_toolkit(self, tk: BrowserToolkit) -> bool:
        """尝试点击分页控件翻到下一页"""
        next_page_selectors = [
            "a:has-text('下一页')", "button:has-text('下一页')",
            "a:has-text('Next')", "button:has-text('Next')",
            "a:has-text('下页')", "a:has-text('>')",
            "[class*='next']", "[class*='pager-next']",
            "a[aria-label='Next']", "button[aria-label='Next']",
            "a[aria-label='下一页']", "button[aria-label='下一页']",
            ".pagination .next", ".pager .next",
            "li.next > a", "li.next > button",
            ".ant-pagination-next:not(.ant-pagination-disabled) a",
            ".el-pagination .btn-next:not(:disabled)",
        ]
        for sel in next_page_selectors:
            vis_r = await tk.is_visible(sel)
            if not (vis_r.success and vis_r.data):
                continue
            # 检查是否禁用
            disabled_r = await tk.evaluate_js(
                "(sel) => { const el = document.querySelector(sel); return el && (el.disabled || el.classList.contains('disabled') || el.getAttribute('aria-disabled') === 'true'); }",
                sel,
            )
            if disabled_r.success and disabled_r.data:
                continue
            r = await tk.click(sel)
            if r.success:
                log_agent_action(self.name, "翻到下一页", sel)
                await tk.human_delay(500, 3000)
                await tk.wait_for_load("domcontentloaded", timeout=8000)
                return True
        return False

    # ── execute / process (LangGraph integration) ────────────

    async def execute_async(self, task: TaskItem, shared_memory: Dict[str, Any]) -> Dict[str, Any]:
        classify_failure, make_trace_step, evaluate_success_criteria, execute_fallback, MAX_FALLBACK_ATTEMPTS = _import_paod()

        params = task["params"]
        url = params.get("url", "")
        limit = params.get("limit", 10)
        task_description = task["description"]
        trace: List[Dict[str, Any]] = task.get("execution_trace", [])
        step_no = len(trace) + 1
        resolved_model = params.get("_resolved_model", "")

        trace.append(make_trace_step(step_no, "执行 smart_scrape", f"url={url}, limit={limit}", "", ""))
        runner = self
        if resolved_model:
            try:
                runner = WebWorker(llm_client=LLMClient(model=resolved_model))
                runner.fast_mode = self.fast_mode
                runner.block_heavy_resources = self.block_heavy_resources
                runner.static_fetch_enabled = self.static_fetch_enabled
            except Exception as e:
                log_warning(f"初始化任务专用模型失败: {e}，回退默认模型")
                runner = self

        result = await runner.smart_scrape(url, task_description, limit)
        trace[-1]["observation"] = f"success={result.get('success')}, count={result.get('count', 0)}"

        criteria = task.get("success_criteria", [])
        if result.get("success") and evaluate_success_criteria(criteria, result):
            trace[-1]["decision"] = "criteria_met → done"
            task["execution_trace"] = trace
            return result

        trace[-1]["decision"] = "criteria_not_met → try fallback"
        fb_index = 0
        while fb_index < MAX_FALLBACK_ATTEMPTS:
            fb = execute_fallback(task, fb_index, shared_memory)
            if fb is None:
                break
            fb_index += 1
            step_no += 1

            if fb["action"] == "switch_worker":
                trace.append(make_trace_step(step_no, f"switch_worker → {fb['target']}", "signal", "", "escalate"))
                task["execution_trace"] = trace
                result["_switch_worker"] = fb["target"]
                result["_switch_params"] = fb.get("param_patch", {})
                return result

            patch = fb.get("param_patch", {})
            patched_params = {**params, **patch}
            retry_url = patched_params.get("url", url)
            retry_limit = patched_params.get("limit", limit)
            trace.append(make_trace_step(step_no, f"retry #{fb_index}", f"url={retry_url}, patch={patch}", "", ""))

            result = await runner.smart_scrape(retry_url, task_description, retry_limit)
            trace[-1]["observation"] = f"success={result.get('success')}, count={result.get('count', 0)}"

            if result.get("success") and evaluate_success_criteria(criteria, result):
                trace[-1]["decision"] = "criteria_met → done"
                task["execution_trace"] = trace
                return result
            trace[-1]["decision"] = "still_failing → next fallback"

        if not result.get("success"):
            task["failure_type"] = classify_failure(result.get("error", ""))
        task["execution_trace"] = trace
        return result

    def execute(self, task: TaskItem, shared_memory: Dict[str, Any]) -> Dict[str, Any]:
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

        asyncio.run(_process_all())
        return state
