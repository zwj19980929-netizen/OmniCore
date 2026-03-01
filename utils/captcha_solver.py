"""
OmniCore 验证码自动处理模块
使用多模态 LLM (GPT-4V) 识别并自动完成验证码
通过 BrowserToolkit 执行所有浏览器操作
"""
import base64
import asyncio
import random
from typing import Dict, Any, List, Optional, Tuple
from pathlib import Path

from openai import OpenAI
from config.settings import settings
from utils.logger import log_agent_action, log_success, log_error, log_warning


class CaptchaSolver:
    """
    验证码自动处理器
    支持：文字验证码、点选验证码、滑块验证码
    接收 BrowserToolkit 实例，所有浏览器操作通过 toolkit 完成。
    兼容旧接口：不传 toolkit 时退化为独立实例（但不推荐）。
    """

    def __init__(self, toolkit=None):
        self.name = "CaptchaSolver"
        self.client = OpenAI(api_key=settings.OPENAI_API_KEY)
        self.vision_model = settings.VISION_MODEL
        self.toolkit = toolkit  # BrowserToolkit instance

    async def detect_captcha(self, page=None) -> Dict[str, Any]:
        """
        检测页面是否有验证码。
        优先使用 self.toolkit，page 参数保留用于向后兼容。
        """
        tk = self.toolkit

        title_r = await tk.get_title()
        html_r = await tk.get_page_html()
        url_r = await tk.get_current_url()
        title = title_r.data or ""
        content = html_r.data or ""
        url = url_r.data or ""

        # 排除正常页面（搜索引擎首页等）
        normal_pages = ["百度一下", "google", "bing", "baidu.com", "google.com", "bing.com"]
        for normal in normal_pages:
            if normal.lower() in title.lower() or normal.lower() in url.lower():
                exists = await tk.element_exists("#cap-img, .captcha-img, #captcha")
                if not exists.data:
                    return {"has_captcha": False, "captcha_type": None}

        # 检测常见验证码特征
        captcha_indicators = ["验证码", "captcha", "请输入验证", "安全验证", "人机验证"]
        has_captcha_keyword = any(
            ind in title.lower() or ind in content.lower() for ind in captcha_indicators
        )

        has_captcha_element = False
        captcha_selectors = ["#cap-img", ".captcha-img", "img[alt*='验证码']", "#captcha"]
        for sel in captcha_selectors:
            exists = await tk.element_exists(sel)
            if exists.data:
                has_captcha_element = True
                break

        if not (has_captcha_keyword and has_captcha_element):
            return {"has_captcha": False, "captcha_type": None}

        log_agent_action(self.name, "检测到验证码页面", title)

        captcha_type = "unknown"
        if "输入" in content and ("验证码" in content or "captcha" in content.lower()):
            captcha_type = "text"
        elif "点击" in content or "点选" in content:
            captcha_type = "click"
        elif "滑动" in content or "拖动" in content:
            captcha_type = "slide"

        return {"has_captcha": True, "captcha_type": captcha_type}

    async def screenshot_captcha(self, page=None) -> Tuple[bytes, Optional[Dict[str, int]]]:
        """截取验证码区域截图"""
        tk = self.toolkit
        shot_r = await tk.screenshot()
        screenshot = shot_r.data if shot_r.success else b""

        captcha_selectors = [
            "#cap-img", ".captcha-img", "img[alt*='验证码']", ".verify-img", "#captcha",
        ]
        bounds = None
        for selector in captcha_selectors:
            box_r = await tk.get_bounding_box(selector)
            if box_r.success and box_r.data:
                log_agent_action(self.name, "找到验证码元素", selector)
                bounds = box_r.data
                break

        return screenshot, bounds

    def analyze_captcha_with_vision(
        self,
        screenshot_base64: str,
        captcha_type: str = "unknown",
    ) -> Dict[str, Any]:
        """使用 GPT-4V 分析验证码（纯 API 调用，不碰浏览器）"""
        log_agent_action(self.name, "调用 GPT-4V 识别验证码")

        prompt = """请识别这张图片中显示的文字或数字。

图片中可能包含：
1. 扭曲变形的字母和数字组合
2. 简单的数学算式（如加减法）

请返回 JSON 格式：
```json
{
    "captcha_type": "text",
    "solution": "你看到的内容",
    "confidence": 0.9,
    "instructions": "描述"
}
```

说明：
- 如果是字母数字，直接写出你看到的字符
- 如果是数学算式，写出计算结果
- confidence 表示识别把握度"""

        try:
            response = self.client.chat.completions.create(
                model=self.vision_model,
                messages=[{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{screenshot_base64}"}},
                    ],
                }],
                max_tokens=500,
            )

            result_text = response.choices[0].message.content
            import json
            import re

            result = None
            json_match = re.search(r'```json\s*(.*?)\s*```', result_text, re.DOTALL)
            if json_match:
                try:
                    result = json.loads(json_match.group(1))
                except Exception:
                    pass
            if not result:
                json_match = re.search(r'\{[^{}]*\}', result_text, re.DOTALL)
                if json_match:
                    try:
                        result = json.loads(json_match.group(0))
                    except Exception:
                        pass
            if not result:
                try:
                    result = json.loads(result_text)
                except Exception:
                    pass
            if not result:
                solution_match = re.search(r'solution["\s:]+(["\']?)([^"\'}\n,]+)\1', result_text, re.IGNORECASE)
                if solution_match:
                    result = {
                        "captcha_type": "text",
                        "solution": solution_match.group(2).strip(),
                        "confidence": 0.7,
                        "instructions": "从文本提取",
                    }
            if not result:
                raise ValueError(f"无法解析响应: {result_text[:200]}")

            log_agent_action(self.name, f"识别结果: {result.get('captcha_type')}", f"solution: {result.get('solution')}")
            return result
        except Exception as e:
            log_error(f"GPT-4V 识别失败: {e}")
            return {"captcha_type": "unknown", "solution": None, "confidence": 0, "instructions": f"识别失败: {str(e)}"}

    async def solve_text_captcha(self, solution: str, page=None) -> bool:
        """解决文字验证码"""
        tk = self.toolkit
        log_agent_action(self.name, "输入文字验证码", solution)

        input_selectors = [
            "input[name*='captcha']", "input[id*='captcha']",
            "input[placeholder*='验证码']", "#code", ".captcha-input", "input[type='text']",
        ]

        for selector in input_selectors:
            exists = await tk.element_exists(selector)
            if not exists.data:
                continue
            await tk.click(selector)
            await tk.human_delay(100, 200)
            await tk.clear_input(selector)
            await tk.human_delay(100, 200)
            await tk.type_text(selector, solution, delay=random.randint(50, 150))
            log_success(f"验证码已输入: {solution}")

            submit_selectors = [
                "button[type='submit']", "input[type='submit']",
                "button:has-text('确定')", "button:has-text('提交')",
                "button:has-text('验证')", ".submit-btn",
            ]
            for submit_sel in submit_selectors:
                exists_s = await tk.element_exists(submit_sel)
                if exists_s.data:
                    await tk.human_delay(300, 600)
                    r = await tk.click(submit_sel)
                    if r.success:
                        log_agent_action(self.name, "点击提交按钮")
                        return True

            await tk.press_key("Enter")
            return True

        log_error("未找到验证码输入框")
        return False

    async def solve_click_captcha(self, solution: str, bounds: Optional[Dict] = None, page=None) -> bool:
        """解决点选验证码"""
        tk = self.toolkit
        log_agent_action(self.name, "处理点选验证码", solution)

        try:
            img_selectors = ["#cap-img", ".captcha-img", "img[alt*='验证码']"]
            for selector in img_selectors:
                box_r = await tk.get_bounding_box(selector)
                if box_r.success and box_r.data:
                    box = box_r.data
                    x = box["x"] + box["width"] / 2
                    y = box["y"] + box["height"] / 2
                    await tk.mouse_click_at(x, y)
                    log_agent_action(self.name, "点击位置", f"({x}, {y})")
                    return True
        except Exception as e:
            log_error(f"点选验证码处理失败: {e}")
        return False

    async def solve_slide_captcha(self, distance: int, page=None) -> bool:
        """解决滑块验证码"""
        tk = self.toolkit
        log_agent_action(self.name, "处理滑块验证码", f"距离: {distance}px")

        try:
            slider_selectors = [".slider", ".slide-btn", ".drag-btn", "[class*='slider']"]
            for selector in slider_selectors:
                box_r = await tk.get_bounding_box(selector)
                if not (box_r.success and box_r.data):
                    continue
                box = box_r.data
                start_x = box["x"] + box["width"] / 2
                start_y = box["y"] + box["height"] / 2

                await tk.mouse_down_at(start_x, start_y)
                steps = random.randint(10, 20)
                for i in range(steps):
                    progress = (i + 1) / steps
                    offset_x = start_x + distance * progress + random.randint(-2, 2)
                    offset_y = start_y + random.randint(-2, 2)
                    await tk.mouse_move_to(offset_x, offset_y)
                    await asyncio.sleep(random.uniform(0.01, 0.03))
                await tk.mouse_up()
                log_success("滑块验证完成")
                return True
        except Exception as e:
            log_error(f"滑块验证码处理失败: {e}")
        return False

    async def solve(self, max_retries: int = 3, page=None) -> bool:
        """自动检测并解决验证码"""
        tk = self.toolkit

        for attempt in range(max_retries):
            log_agent_action(self.name, "尝试解决验证码", f"第 {attempt + 1} 次")

            detection = await self.detect_captcha()
            if not detection["has_captcha"]:
                log_success("页面无验证码或已通过验证")
                return True

            screenshot, bounds = await self.screenshot_captcha()
            screenshot_base64 = base64.b64encode(screenshot).decode()

            analysis = self.analyze_captcha_with_vision(
                screenshot_base64, detection.get("captcha_type", "unknown")
            )

            if analysis["confidence"] < 0.5:
                log_warning(f"识别置信度过低: {analysis['confidence']}")
                await self._try_refresh_captcha()
                await asyncio.sleep(1.5)
                continue

            captcha_type = analysis["captcha_type"]
            solution = analysis["solution"]

            success = False
            if captcha_type in ["text", "math"] and solution:
                success = await self.solve_text_captcha(str(solution))
            elif captcha_type == "click":
                success = await self.solve_click_captcha(str(solution), bounds)
            elif captcha_type == "slide" and solution:
                distance = int(solution) if str(solution).isdigit() else 200
                success = await self.solve_slide_captcha(distance)

            if success:
                await asyncio.sleep(2.5)
                await tk.wait_for_load("domcontentloaded", timeout=5000)

                try:
                    title_r = await tk.get_title()
                    new_title = title_r.data or ""
                    if "验证" not in new_title and "captcha" not in new_title.lower():
                        log_success("验证码验证通过!")
                        return True

                    new_detection = await self.detect_captcha()
                    if not new_detection["has_captcha"]:
                        log_success("验证码验证通过!")
                        return True
                except Exception:
                    log_agent_action(self.name, "页面已导航，验证可能已通过")
                    return True

            log_warning(f"第 {attempt + 1} 次尝试失败，刷新验证码重试...")
            await self._try_refresh_captcha()
            await asyncio.sleep(1.5)

        log_error(f"验证码解决失败，已重试 {max_retries} 次")
        return False

    async def _try_refresh_captcha(self, page=None) -> bool:
        """尝试刷新验证码"""
        tk = self.toolkit
        refresh_selectors = [
            "#cap-img", "a:has-text('换一张')", "a:has-text('刷新')",
            ".refresh-captcha", "#refresh", "img[alt*='验证码']",
        ]

        for selector in refresh_selectors:
            exists = await tk.element_exists(selector)
            if not exists.data:
                continue

            old_src = None
            if selector == "#cap-img" or "img" in selector:
                attr_r = await tk.get_attribute(selector, "src")
                old_src = attr_r.data if attr_r.success else None

            r = await tk.click(selector)
            if not r.success:
                continue
            log_agent_action(self.name, "刷新验证码")
            await asyncio.sleep(2)

            if old_src:
                for _ in range(5):
                    new_attr = await tk.get_attribute(selector, "src")
                    if new_attr.success and new_attr.data != old_src:
                        break
                    await asyncio.sleep(0.5)
            return True
        return False
