"""
OmniCore 验证码自动处理模块
使用多模态 LLM (GPT-4V) 识别并自动完成验证码
"""
import base64
import asyncio
import random
from typing import Dict, Any, List, Optional, Tuple
from pathlib import Path
from playwright.async_api import Page

from openai import OpenAI
from config.settings import settings
from utils.logger import log_agent_action, log_success, log_error, log_warning


class CaptchaSolver:
    """
    验证码自动处理器
    支持：文字验证码、点选验证码、滑块验证码
    """

    def __init__(self):
        self.name = "CaptchaSolver"
        self.client = OpenAI(api_key=settings.OPENAI_API_KEY)
        self.vision_model = settings.VISION_MODEL

    async def detect_captcha(self, page: Page) -> Dict[str, Any]:
        """
        检测页面是否有验证码

        Returns:
            {
                "has_captcha": bool,
                "captcha_type": "text" | "click" | "slide" | "unknown",
                "captcha_element": selector or None
            }
        """
        title = await page.title()
        content = await page.content()
        url = page.url

        # 排除正常页面（搜索引擎首页等）
        normal_pages = [
            "百度一下",
            "google",
            "bing",
            "baidu.com",
            "google.com",
            "bing.com",
        ]
        for normal in normal_pages:
            if normal.lower() in title.lower() or normal.lower() in url.lower():
                # 检查是否真的有验证码元素
                captcha_elements = await page.query_selector_all("#cap-img, .captcha-img, #captcha")
                if not captcha_elements:
                    return {"has_captcha": False, "captcha_type": None}

        # 检测常见验证码特征 - 更严格的检测
        captcha_indicators = [
            "验证码",
            "captcha",
            "请输入验证",
            "安全验证",
            "人机验证",
        ]

        # 检查标题或页面内容是否包含验证码关键词
        has_captcha_keyword = any(ind in title.lower() or ind in content.lower()
                         for ind in captcha_indicators)

        # 同时检查是否有验证码输入框或图片
        has_captcha_element = False
        captcha_selectors = ["#cap-img", ".captcha-img", "img[alt*='验证码']", "#captcha"]
        for sel in captcha_selectors:
            elem = await page.query_selector(sel)
            if elem:
                has_captcha_element = True
                break

        has_captcha = has_captcha_keyword and has_captcha_element

        if not has_captcha:
            return {"has_captcha": False, "captcha_type": None}

        log_agent_action(self.name, "检测到验证码页面", title)

        # 尝试识别验证码类型
        captcha_type = "unknown"
        if "输入" in content and ("验证码" in content or "captcha" in content.lower()):
            captcha_type = "text"
        elif "点击" in content or "点选" in content:
            captcha_type = "click"
        elif "滑动" in content or "拖动" in content:
            captcha_type = "slide"

        return {
            "has_captcha": True,
            "captcha_type": captcha_type,
        }

    async def screenshot_captcha(self, page: Page) -> Tuple[bytes, Dict[str, int]]:
        """
        截取验证码区域截图

        Returns:
            (screenshot_bytes, {"x": x, "y": y, "width": w, "height": h})
        """
        # 先截取整个页面
        screenshot = await page.screenshot()

        # 尝试定位验证码区域
        captcha_selectors = [
            "#cap-img",
            ".captcha-img",
            "img[alt*='验证码']",
            ".verify-img",
            "#captcha",
        ]

        bounds = None
        for selector in captcha_selectors:
            try:
                elem = await page.query_selector(selector)
                if elem:
                    bounds = await elem.bounding_box()
                    if bounds:
                        log_agent_action(self.name, f"找到验证码元素", selector)
                        break
            except:
                continue

        return screenshot, bounds

    def analyze_captcha_with_vision(
        self,
        screenshot_base64: str,
        captcha_type: str = "unknown",
    ) -> Dict[str, Any]:
        """
        使用 GPT-4V 分析验证码

        Args:
            screenshot_base64: base64 编码的截图
            captcha_type: 验证码类型提示

        Returns:
            {
                "captcha_type": "text" | "click" | "slide",
                "solution": "文字内容" | [(x1,y1), (x2,y2)] | distance,
                "confidence": 0.95,
                "instructions": "操作说明"
            }
        """
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
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/png;base64,{screenshot_base64}"
                                }
                            }
                        ]
                    }
                ],
                max_tokens=500,
            )

            result_text = response.choices[0].message.content

            # 解析 JSON
            import json
            import re

            # 提取 JSON - 尝试多种方式
            result = None

            # 方式1: 提取 ```json ... ``` 块
            json_match = re.search(r'```json\s*(.*?)\s*```', result_text, re.DOTALL)
            if json_match:
                try:
                    result = json.loads(json_match.group(1))
                except:
                    pass

            # 方式2: 提取 { ... } 块
            if not result:
                json_match = re.search(r'\{[^{}]*\}', result_text, re.DOTALL)
                if json_match:
                    try:
                        result = json.loads(json_match.group(0))
                    except:
                        pass

            # 方式3: 直接解析整个响应
            if not result:
                try:
                    result = json.loads(result_text)
                except:
                    pass

            # 方式4: 从文本中提取关键信息
            if not result:
                # 尝试从文本中提取 solution
                solution_match = re.search(r'solution["\s:]+(["\']?)([^"\'}\n,]+)\1', result_text, re.IGNORECASE)
                if solution_match:
                    result = {
                        "captcha_type": "text",
                        "solution": solution_match.group(2).strip(),
                        "confidence": 0.7,
                        "instructions": "从文本提取"
                    }

            if not result:
                raise ValueError(f"无法解析响应: {result_text[:200]}")

            log_agent_action(
                self.name,
                f"识别结果: {result.get('captcha_type')}",
                f"solution: {result.get('solution')}"
            )
            return result

        except Exception as e:
            log_error(f"GPT-4V 识别失败: {e}")
            return {
                "captcha_type": "unknown",
                "solution": None,
                "confidence": 0,
                "instructions": f"识别失败: {str(e)}"
            }

    async def solve_text_captcha(
        self,
        page: Page,
        solution: str,
    ) -> bool:
        """
        解决文字验证码

        Args:
            page: Playwright 页面
            solution: 要输入的文字

        Returns:
            是否成功
        """
        log_agent_action(self.name, "输入文字验证码", solution)

        # 查找输入框
        input_selectors = [
            "input[name*='captcha']",
            "input[id*='captcha']",
            "input[placeholder*='验证码']",
            "#code",
            ".captcha-input",
            "input[type='text']",
        ]

        for selector in input_selectors:
            try:
                elem = await page.query_selector(selector)
                if elem:
                    # 先清空输入框
                    await elem.click()
                    await asyncio.sleep(random.uniform(0.1, 0.2))
                    await elem.fill("")  # 清空
                    await asyncio.sleep(random.uniform(0.1, 0.2))

                    # 模拟人类输入
                    for char in solution:
                        await elem.type(char, delay=random.randint(50, 150))

                    log_success(f"验证码已输入: {solution}")

                    # 查找并点击提交按钮
                    submit_selectors = [
                        "button[type='submit']",
                        "input[type='submit']",
                        "button:has-text('确定')",
                        "button:has-text('提交')",
                        "button:has-text('验证')",
                        ".submit-btn",
                    ]

                    for submit_sel in submit_selectors:
                        try:
                            submit_btn = await page.query_selector(submit_sel)
                            if submit_btn:
                                await asyncio.sleep(random.uniform(0.3, 0.6))
                                await submit_btn.click()
                                log_agent_action(self.name, "点击提交按钮")
                                return True
                        except:
                            continue

                    # 如果没找到按钮，尝试按回车
                    await page.keyboard.press("Enter")
                    return True

            except Exception as e:
                continue

        log_error("未找到验证码输入框")
        return False

    async def solve_click_captcha(
        self,
        page: Page,
        solution: str,
        bounds: Optional[Dict] = None,
    ) -> bool:
        """
        解决点选验证码

        Args:
            page: Playwright 页面
            solution: 点击说明或坐标
            bounds: 验证码区域边界

        Returns:
            是否成功
        """
        log_agent_action(self.name, "处理点选验证码", solution)

        # 如果 solution 包含坐标，直接点击
        # 否则需要再次调用 vision 获取具体坐标
        # 这里简化处理，假设需要点击验证码图片区域

        try:
            # 查找验证码图片
            img_selectors = [
                "#cap-img",
                ".captcha-img",
                "img[alt*='验证码']",
            ]

            for selector in img_selectors:
                elem = await page.query_selector(selector)
                if elem:
                    box = await elem.bounding_box()
                    if box:
                        # 点击图片中心（简化处理）
                        x = box["x"] + box["width"] / 2
                        y = box["y"] + box["height"] / 2
                        await page.mouse.click(x, y)
                        log_agent_action(self.name, f"点击位置", f"({x}, {y})")
                        return True

        except Exception as e:
            log_error(f"点选验证码处理失败: {e}")

        return False

    async def solve_slide_captcha(
        self,
        page: Page,
        distance: int,
    ) -> bool:
        """
        解决滑块验证码

        Args:
            page: Playwright 页面
            distance: 滑动距离

        Returns:
            是否成功
        """
        log_agent_action(self.name, "处理滑块验证码", f"距离: {distance}px")

        try:
            # 查找滑块
            slider_selectors = [
                ".slider",
                ".slide-btn",
                ".drag-btn",
                "[class*='slider']",
            ]

            for selector in slider_selectors:
                elem = await page.query_selector(selector)
                if elem:
                    box = await elem.bounding_box()
                    if box:
                        start_x = box["x"] + box["width"] / 2
                        start_y = box["y"] + box["height"] / 2

                        # 模拟人类滑动轨迹
                        await page.mouse.move(start_x, start_y)
                        await page.mouse.down()

                        # 分段滑动，模拟人类行为
                        steps = random.randint(10, 20)
                        for i in range(steps):
                            progress = (i + 1) / steps
                            # 添加一些随机抖动
                            offset_x = start_x + distance * progress + random.randint(-2, 2)
                            offset_y = start_y + random.randint(-2, 2)
                            await page.mouse.move(offset_x, offset_y)
                            await asyncio.sleep(random.uniform(0.01, 0.03))

                        await page.mouse.up()
                        log_success("滑块验证完成")
                        return True

        except Exception as e:
            log_error(f"滑块验证码处理失败: {e}")

        return False

    async def solve(self, page: Page, max_retries: int = 3) -> bool:
        """
        自动检测并解决验证码

        Args:
            page: Playwright 页面
            max_retries: 最大重试次数

        Returns:
            是否成功通过验证
        """
        for attempt in range(max_retries):
            log_agent_action(self.name, f"尝试解决验证码", f"第 {attempt + 1} 次")

            # 1. 检测验证码
            detection = await self.detect_captcha(page)
            if not detection["has_captcha"]:
                log_success("页面无验证码或已通过验证")
                return True

            # 2. 截图
            screenshot, bounds = await self.screenshot_captcha(page)
            screenshot_base64 = base64.b64encode(screenshot).decode()

            # 3. 用 GPT-4V 分析
            analysis = self.analyze_captcha_with_vision(
                screenshot_base64,
                detection.get("captcha_type", "unknown")
            )

            if analysis["confidence"] < 0.5:
                log_warning(f"识别置信度过低: {analysis['confidence']}")
                await self._try_refresh_captcha(page)
                await asyncio.sleep(1.5)
                continue

            # 4. 执行解决方案
            captcha_type = analysis["captcha_type"]
            solution = analysis["solution"]

            success = False
            # text 和 math 类型都是输入文字
            if captcha_type in ["text", "math"] and solution:
                success = await self.solve_text_captcha(page, str(solution))
            elif captcha_type == "click":
                success = await self.solve_click_captcha(page, str(solution), bounds)
            elif captcha_type == "slide" and solution:
                distance = int(solution) if str(solution).isdigit() else 200
                success = await self.solve_slide_captcha(page, distance)

            if success:
                # 等待页面响应和可能的导航
                await asyncio.sleep(2.5)

                # 等待页面稳定（可能发生了导航）
                try:
                    await page.wait_for_load_state("domcontentloaded", timeout=5000)
                except:
                    pass

                # 检查是否通过 - 如果页面已导航到新URL，说明验证通过
                try:
                    current_url = page.url
                    new_title = await page.title()
                    # 如果不再是验证码页面，说明通过了
                    if "验证" not in new_title and "captcha" not in new_title.lower():
                        log_success("验证码验证通过!")
                        return True

                    # 再次检测是否还有验证码
                    new_detection = await self.detect_captcha(page)
                    if not new_detection["has_captcha"]:
                        log_success("验证码验证通过!")
                        return True
                except Exception as e:
                    # 如果出现导航相关错误，可能是验证通过后页面跳转了
                    log_agent_action(self.name, "页面已导航，验证可能已通过")
                    return True

            log_warning(f"第 {attempt + 1} 次尝试失败，刷新验证码重试...")
            # 刷新验证码再重试
            await self._try_refresh_captcha(page)
            await asyncio.sleep(1.5)

        log_error(f"验证码解决失败，已重试 {max_retries} 次")
        return False

    async def _try_refresh_captcha(self, page: Page) -> bool:
        """尝试刷新验证码"""
        refresh_selectors = [
            "#cap-img",  # CNVD 的验证码图片本身可点击刷新
            "a:has-text('换一张')",
            "a:has-text('刷新')",
            ".refresh-captcha",
            "#refresh",
            "img[alt*='验证码']",
        ]

        for selector in refresh_selectors:
            try:
                elem = await page.query_selector(selector)
                if elem:
                    # 获取当前图片的 src（用于检测是否刷新成功）
                    old_src = None
                    if selector == "#cap-img" or "img" in selector:
                        old_src = await elem.get_attribute("src")

                    await elem.click()
                    log_agent_action(self.name, "刷新验证码")

                    # 等待新验证码加载
                    await asyncio.sleep(2)

                    # 如果是图片，等待 src 变化
                    if old_src:
                        for _ in range(5):
                            new_src = await elem.get_attribute("src")
                            if new_src != old_src:
                                break
                            await asyncio.sleep(0.5)

                    return True
            except:
                continue
        return False
