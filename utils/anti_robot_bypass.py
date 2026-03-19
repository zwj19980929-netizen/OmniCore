"""
AntiRobotBypass — 通用反机器人验证绕过模块

感知并拟人化破解各种"验证你不是机器人"的挑战:
  - Bing 人机身份验证
  - Cloudflare Turnstile / Challenge / Under Attack Mode
  - reCAPTCHA v2 checkbox ("I'm not a robot")
  - hCaptcha checkbox
  - 通用 "verify you are human" 页面
  - Cookie consent / 弹窗干扰

策略: 不依赖 OCR，而是通过拟人化行为（自然鼠标轨迹、真实时序、
页面交互痕迹）让检测系统认为操作者是真人。
"""

import asyncio
import math
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from utils.logger import log_agent_action, log_success, log_error, log_warning


class ChallengeType(Enum):
    """识别到的验证挑战类型"""
    NONE = "none"
    CLOUDFLARE_TURNSTILE = "cloudflare_turnstile"
    CLOUDFLARE_CHALLENGE = "cloudflare_challenge"
    RECAPTCHA_CHECKBOX = "recaptcha_checkbox"
    HCAPTCHA_CHECKBOX = "hcaptcha_checkbox"
    BING_VERIFY = "bing_verify"
    GENERIC_CHECKBOX = "generic_checkbox"
    GENERIC_BUTTON = "generic_button"
    COOKIE_CONSENT = "cookie_consent"
    BLOCKED_PAGE = "blocked_page"


@dataclass
class ChallengeDetection:
    """检测结果"""
    challenge_type: ChallengeType = ChallengeType.NONE
    confidence: float = 0.0
    iframe_selector: str = ""
    action_selector: str = ""
    detail: str = ""


class AntiRobotBypass:
    """
    通用反机器人验证绕过器。
    接收 BrowserToolkit 实例，所有浏览器操作通过 toolkit 完成。
    """

    NAME = "AntiRobotBypass"

    # 每种挑战类型的最大重试次数
    MAX_RETRIES = 3

    # ── 检测规则 ──────────────────────────────────────────────

    # Cloudflare 特征
    CF_TURNSTILE_IFRAME_SELECTORS = [
        'iframe[src*="challenges.cloudflare.com"]',
        'iframe[src*="turnstile"]',
        'iframe[title*="Cloudflare"]',
    ]
    CF_CHALLENGE_SIGNALS = [
        "just a moment",
        "checking your browser",
        "enable javascript and cookies",
        "ray id",
        "cloudflare",
        "ddos protection",
        "attention required",
    ]

    # reCAPTCHA 特征
    RECAPTCHA_IFRAME_SELECTORS = [
        'iframe[src*="recaptcha/api2/anchor"]',
        'iframe[src*="recaptcha/enterprise/anchor"]',
        'iframe[title*="reCAPTCHA"]',
    ]

    # hCaptcha 特征
    HCAPTCHA_IFRAME_SELECTORS = [
        'iframe[src*="hcaptcha.com/captcha"]',
        'iframe[title*="hCaptcha"]',
        'iframe[data-hcaptcha-widget-id]',
    ]

    # Bing 验证特征
    BING_VERIFY_SIGNALS = [
        "人机身份验证",
        "验证你是真人",
        "请解决以下难题以继续",
        "请解决以下难题",
        "solve the following puzzle",
        "verify you are a human",
        "our systems have detected unusual traffic",
        "automated requests from your computer",
    ]
    BING_VERIFY_SELECTORS = [
        '#bnp_btn_accept',          # Bing "验证" 按钮
        '#b_notificationContainer', # Bing 通知容器
        'a[href*="bnp_"]',          # Bing 验证链接
        '#challenge-stage',         # Bing challenge 容器
    ]

    # 通用 "I'm not a robot" 特征
    GENERIC_CHECKBOX_SELECTORS = [
        'input[type="checkbox"][id*="robot"]',
        'input[type="checkbox"][id*="human"]',
        'input[type="checkbox"][id*="verify"]',
        'label[for*="robot"]',
        'label[for*="human"]',
        '.verify-checkbox',
        '#verify-checkbox',
    ]

    # 通用验证按钮特征
    GENERIC_BUTTON_TEXTS = [
        "i'm not a robot",
        "i am not a robot",
        "verify",
        "continue",
        "验证",
        "确认",
        "我不是机器人",
        "点击验证",
        "进行验证",
        "通过验证",
    ]

    # Cookie consent 弹窗特征
    COOKIE_CONSENT_SELECTORS = [
        '#onetrust-accept-btn-handler',
        '#CybotCookiebotDialogBodyLevelButtonLevelOptinAllowAll',
        'button[data-testid="cookie-accept"]',
        'button[id*="cookie"][id*="accept"]',
        'button[id*="consent"][id*="accept"]',
        '.cookie-consent-accept',
        '#accept-cookies',
        'button:has-text("Accept all")',
        'button:has-text("Accept cookies")',
        'button:has-text("同意")',
        'button:has-text("接受所有")',
        'button:has-text("全部接受")',
    ]

    def __init__(self, toolkit):
        self.toolkit = toolkit

    # ═══════════════════════════════════════════════════════════
    #  核心公开接口
    # ═══════════════════════════════════════════════════════════

    async def detect_and_bypass(self, max_retries: int = 3) -> bool:
        """
        主入口: 检测当前页面是否存在反机器人挑战，如果有就尝试绕过。
        返回 True 表示页面正常（无挑战或已绕过），False 表示绕过失败。
        """
        for attempt in range(max_retries):
            detection = await self.detect_challenge()

            if detection.challenge_type == ChallengeType.NONE:
                if attempt > 0:
                    log_success(f"{self.NAME}: 页面验证已通过")
                return True

            log_agent_action(
                self.NAME,
                f"检测到挑战 [{detection.challenge_type.value}]",
                f"置信度={detection.confidence:.0%}, 详情={detection.detail}",
            )

            bypassed = await self._dispatch_bypass(detection)
            if bypassed:
                # 等待页面跳转或刷新
                await self._wait_for_navigation_after_bypass()
                # 再次检测，确认是否真的通过了
                continue

            log_warning(
                f"{self.NAME}: 第 {attempt + 1}/{max_retries} 次绕过失败"
            )

        # 最后再检测一次
        final = await self.detect_challenge()
        if final.challenge_type == ChallengeType.NONE:
            log_success(f"{self.NAME}: 页面验证已通过")
            return True

        log_error(f"{self.NAME}: 绕过失败，挑战类型={final.challenge_type.value}")
        return False

    # ═══════════════════════════════════════════════════════════
    #  感知: 挑战类型检测
    # ═══════════════════════════════════════════════════════════

    async def detect_challenge(self) -> ChallengeDetection:
        """
        全面扫描当前页面，识别存在的反机器人验证挑战类型。
        按优先级从高到低检测。
        """
        tk = self.toolkit

        # 采集页面基本信息
        url = ""
        title = ""
        body_text = ""
        try:
            url_r = await tk.get_current_url()
            url = (url_r.data or "").lower() if url_r.success else ""
            title_r = await tk.get_title()
            title = (title_r.data or "").lower() if title_r.success else ""
            body_r = await tk.evaluate_js(
                "() => (document.body && document.body.innerText || '').slice(0, 5000)"
            )
            body_text = (body_r.data or "").lower() if body_r.success else ""
        except Exception:
            pass

        combined = f"{url} {title} {body_text}"

        # 1. Cloudflare Turnstile (iframe 内)
        det = await self._detect_cf_turnstile()
        if det.challenge_type != ChallengeType.NONE:
            return det

        # 2. Cloudflare Challenge Page (整页拦截)
        det = self._detect_cf_challenge(url, title, body_text)
        if det.challenge_type != ChallengeType.NONE:
            return det

        # 3. reCAPTCHA checkbox
        det = await self._detect_recaptcha()
        if det.challenge_type != ChallengeType.NONE:
            return det

        # 4. hCaptcha checkbox
        det = await self._detect_hcaptcha()
        if det.challenge_type != ChallengeType.NONE:
            return det

        # 5. Bing 人机验证
        det = await self._detect_bing_verify(url, title, body_text)
        if det.challenge_type != ChallengeType.NONE:
            return det

        # 6. Cookie consent 弹窗 (不算 challenge 但可能挡住内容)
        det = await self._detect_cookie_consent()
        if det.challenge_type != ChallengeType.NONE:
            return det

        # 7. 通用 checkbox
        det = await self._detect_generic_checkbox()
        if det.challenge_type != ChallengeType.NONE:
            return det

        # 8. 通用验证按钮
        det = await self._detect_generic_button(body_text)
        if det.challenge_type != ChallengeType.NONE:
            return det

        # 9. 通用被阻断页面（URL/title 包含关键词）
        det = self._detect_blocked_page(url, title, body_text)
        if det.challenge_type != ChallengeType.NONE:
            return det

        return ChallengeDetection()

    # ── 子检测器 ──────────────────────────────────────────────

    async def _detect_cf_turnstile(self) -> ChallengeDetection:
        tk = self.toolkit
        for sel in self.CF_TURNSTILE_IFRAME_SELECTORS:
            r = await tk.element_exists(sel)
            if r.success and r.data:
                return ChallengeDetection(
                    challenge_type=ChallengeType.CLOUDFLARE_TURNSTILE,
                    confidence=0.95,
                    iframe_selector=sel,
                    detail="Cloudflare Turnstile iframe detected",
                )
        return ChallengeDetection()

    def _detect_cf_challenge(self, url: str, title: str, body_text: str) -> ChallengeDetection:
        combined = f"{title} {body_text}"
        hits = sum(1 for sig in self.CF_CHALLENGE_SIGNALS if sig in combined)
        if hits >= 2 or ("cloudflare" in combined and "ray id" in combined):
            return ChallengeDetection(
                challenge_type=ChallengeType.CLOUDFLARE_CHALLENGE,
                confidence=min(0.5 + hits * 0.15, 0.95),
                detail=f"Cloudflare challenge page (matched {hits} signals)",
            )
        return ChallengeDetection()

    async def _detect_recaptcha(self) -> ChallengeDetection:
        tk = self.toolkit
        for sel in self.RECAPTCHA_IFRAME_SELECTORS:
            r = await tk.element_exists(sel)
            if r.success and r.data:
                return ChallengeDetection(
                    challenge_type=ChallengeType.RECAPTCHA_CHECKBOX,
                    confidence=0.95,
                    iframe_selector=sel,
                    detail="reCAPTCHA iframe detected",
                )
        return ChallengeDetection()

    async def _detect_hcaptcha(self) -> ChallengeDetection:
        tk = self.toolkit
        for sel in self.HCAPTCHA_IFRAME_SELECTORS:
            r = await tk.element_exists(sel)
            if r.success and r.data:
                return ChallengeDetection(
                    challenge_type=ChallengeType.HCAPTCHA_CHECKBOX,
                    confidence=0.95,
                    iframe_selector=sel,
                    detail="hCaptcha iframe detected",
                )
        return ChallengeDetection()

    async def _detect_bing_verify(self, url: str, title: str, body_text: str) -> ChallengeDetection:
        tk = self.toolkit
        combined = f"{title} {body_text}"

        # URL/文本信号
        text_match = any(sig in combined for sig in self.BING_VERIFY_SIGNALS)
        is_bing = "bing.com" in url

        if not (text_match or is_bing):
            return ChallengeDetection()

        # 检查 Bing 验证专属元素
        for sel in self.BING_VERIFY_SELECTORS:
            r = await tk.element_exists(sel)
            if r.success and r.data:
                return ChallengeDetection(
                    challenge_type=ChallengeType.BING_VERIFY,
                    confidence=0.9,
                    action_selector=sel,
                    detail=f"Bing verify element: {sel}",
                )

        if text_match and is_bing:
            return ChallengeDetection(
                challenge_type=ChallengeType.BING_VERIFY,
                confidence=0.75,
                detail="Bing verify text signals detected",
            )

        return ChallengeDetection()

    async def _detect_cookie_consent(self) -> ChallengeDetection:
        tk = self.toolkit
        for sel in self.COOKIE_CONSENT_SELECTORS:
            try:
                r = await tk.is_visible(sel)
                if r.success and r.data:
                    return ChallengeDetection(
                        challenge_type=ChallengeType.COOKIE_CONSENT,
                        confidence=0.9,
                        action_selector=sel,
                        detail=f"Cookie consent: {sel}",
                    )
            except Exception:
                continue
        return ChallengeDetection()

    async def _detect_generic_checkbox(self) -> ChallengeDetection:
        tk = self.toolkit
        for sel in self.GENERIC_CHECKBOX_SELECTORS:
            try:
                r = await tk.is_visible(sel)
                if r.success and r.data:
                    return ChallengeDetection(
                        challenge_type=ChallengeType.GENERIC_CHECKBOX,
                        confidence=0.8,
                        action_selector=sel,
                        detail=f"Generic verify checkbox: {sel}",
                    )
            except Exception:
                continue
        return ChallengeDetection()

    async def _detect_generic_button(self, body_text: str) -> ChallengeDetection:
        """通过 JS 在页面上查找包含验证关键词的可见按钮"""
        tk = self.toolkit
        try:
            result = await tk.evaluate_js("""() => {
                const keywords = [
                    "i'm not a robot", "i am not a robot", "not a robot",
                    "verify you are human", "verify you're human",
                    "验证", "我不是机器人", "点击验证", "进行验证", "通过验证",
                ];
                const candidates = document.querySelectorAll(
                    'button, input[type="button"], input[type="submit"], a.btn, a.button, [role="button"]'
                );
                for (const el of candidates) {
                    const style = window.getComputedStyle(el);
                    if (!style || style.display === 'none' || style.visibility === 'hidden') continue;
                    const rect = el.getBoundingClientRect();
                    if (rect.width < 10 || rect.height < 10) continue;
                    const text = (el.innerText || el.value || el.textContent || '').toLowerCase().trim();
                    if (!text) continue;
                    for (const kw of keywords) {
                        if (text.includes(kw)) {
                            // 返回可用于定位的选择器
                            if (el.id) return { found: true, selector: '#' + CSS.escape(el.id), text: text };
                            const name = el.getAttribute('name');
                            if (name) return { found: true, selector: el.tagName.toLowerCase() + '[name="' + name + '"]', text: text };
                            return { found: true, selector: '', text: text, keyword: kw };
                        }
                    }
                }
                return { found: false };
            }""")
            if result.success and isinstance(result.data, dict) and result.data.get("found"):
                sel = result.data.get("selector", "")
                kw = result.data.get("keyword", "") or result.data.get("text", "")
                return ChallengeDetection(
                    challenge_type=ChallengeType.GENERIC_BUTTON,
                    confidence=0.75,
                    action_selector=sel,
                    detail=f"Generic verify button: '{kw}'",
                )
        except Exception:
            pass
        return ChallengeDetection()

    def _detect_blocked_page(self, url: str, title: str, body_text: str) -> ChallengeDetection:
        combined = f"{url} {title} {body_text}"
        blocked_tokens = [
            "unusual traffic", "robot check", "access denied",
            "forbidden", "blocked", "异常流量", "人机身份验证",
            "验证码", "安全验证", "访问受限",
        ]
        blocked_url = any(t in url for t in ["/sorry", "/captcha", "/verify", "/challenge", "/blocked", "/forbidden"])
        blocked_text = sum(1 for t in blocked_tokens if t in combined)

        if blocked_url or blocked_text >= 2:
            return ChallengeDetection(
                challenge_type=ChallengeType.BLOCKED_PAGE,
                confidence=min(0.5 + blocked_text * 0.1, 0.9),
                detail=f"Blocked page (url_match={blocked_url}, text_hits={blocked_text})",
            )
        return ChallengeDetection()

    # ═══════════════════════════════════════════════════════════
    #  绕过: 分发到具体处理器
    # ═══════════════════════════════════════════════════════════

    async def _dispatch_bypass(self, detection: ChallengeDetection) -> bool:
        handlers = {
            ChallengeType.CLOUDFLARE_TURNSTILE: self._bypass_cf_turnstile,
            ChallengeType.CLOUDFLARE_CHALLENGE: self._bypass_cf_challenge,
            ChallengeType.RECAPTCHA_CHECKBOX: self._bypass_recaptcha_checkbox,
            ChallengeType.HCAPTCHA_CHECKBOX: self._bypass_hcaptcha_checkbox,
            ChallengeType.BING_VERIFY: self._bypass_bing_verify,
            ChallengeType.COOKIE_CONSENT: self._bypass_cookie_consent,
            ChallengeType.GENERIC_CHECKBOX: self._bypass_generic_checkbox,
            ChallengeType.GENERIC_BUTTON: self._bypass_generic_button,
            ChallengeType.BLOCKED_PAGE: self._bypass_blocked_page,
        }
        handler = handlers.get(detection.challenge_type)
        if handler:
            return await handler(detection)
        return False

    # ═══════════════════════════════════════════════════════════
    #  具体绕过实现
    # ═══════════════════════════════════════════════════════════

    async def _bypass_cf_turnstile(self, det: ChallengeDetection) -> bool:
        """
        Cloudflare Turnstile 绕过:
        1. 先制造人类行为痕迹（滚动、鼠标移动）
        2. 切入 iframe
        3. 找到 checkbox 并以自然轨迹点击
        """
        tk = self.toolkit
        log_agent_action(self.NAME, "绕过 Cloudflare Turnstile")

        # 制造人类活动痕迹
        await self._simulate_human_presence()

        # 切入 turnstile iframe
        r = await tk.switch_to_iframe(det.iframe_selector)
        if not r.success:
            log_warning(f"{self.NAME}: 无法切入 Turnstile iframe: {r.error}")
            return False

        try:
            # Turnstile checkbox 通常在 iframe 内
            checkbox_selectors = [
                'input[type="checkbox"]',
                '.ctp-checkbox-label',
                '#cf-stage',
                'label',
                'div[role="checkbox"]',
            ]
            for sel in checkbox_selectors:
                exists = await tk.element_exists(sel)
                if exists.success and exists.data:
                    await self._human_click_element(sel)
                    await asyncio.sleep(random.uniform(2.0, 4.0))
                    return True

            # 有时 Turnstile 自动通过（只要行为够人类），等一会儿
            await asyncio.sleep(random.uniform(3.0, 6.0))
            return True
        finally:
            await tk.exit_iframe()

    async def _bypass_cf_challenge(self, det: ChallengeDetection) -> bool:
        """
        Cloudflare Challenge Page 绕过:
        这种是整页 JS challenge，不需要点击，只需等待 + 制造人类行为。
        Cloudflare 会在后台验证浏览器 JS 环境，通过后自动跳转。
        """
        tk = self.toolkit
        log_agent_action(self.NAME, "等待 Cloudflare Challenge 自动通过")

        # 制造人类行为
        await self._simulate_human_presence()

        # Cloudflare challenge 通常需要 3-8 秒
        for i in range(12):
            await asyncio.sleep(random.uniform(1.0, 2.0))
            # 偶尔动动鼠标
            if random.random() < 0.4:
                await self._random_mouse_wander()

            # 检查是否已跳转
            title_r = await tk.get_title()
            title = (title_r.data or "").lower() if title_r.success else ""
            if not any(sig in title for sig in ["just a moment", "checking", "cloudflare"]):
                log_success(f"{self.NAME}: Cloudflare Challenge 已通过")
                return True

            # 检查是否出现了 Turnstile widget
            for sel in self.CF_TURNSTILE_IFRAME_SELECTORS:
                exists = await tk.element_exists(sel)
                if exists.success and exists.data:
                    return await self._bypass_cf_turnstile(
                        ChallengeDetection(
                            challenge_type=ChallengeType.CLOUDFLARE_TURNSTILE,
                            iframe_selector=sel,
                        )
                    )

        return False

    async def _bypass_recaptcha_checkbox(self, det: ChallengeDetection) -> bool:
        """
        reCAPTCHA v2 checkbox 绕过:
        切入 iframe，找到 "I'm not a robot" checkbox，用自然轨迹点击。
        注意: 如果触发了图片挑战，那就回退到视觉识别（不在这个模块处理）。
        """
        tk = self.toolkit
        log_agent_action(self.NAME, "绕过 reCAPTCHA checkbox")

        await self._simulate_human_presence()

        r = await tk.switch_to_iframe(det.iframe_selector)
        if not r.success:
            log_warning(f"{self.NAME}: 无法切入 reCAPTCHA iframe")
            return False

        try:
            checkbox_sel = '#recaptcha-anchor, .recaptcha-checkbox-border, .recaptcha-checkbox'
            exists = await tk.element_exists(checkbox_sel)
            if not (exists.success and exists.data):
                return False

            await self._human_click_element(checkbox_sel)
            await asyncio.sleep(random.uniform(2.0, 4.0))

            # 检查是否勾选成功（aria-checked）
            checked = await tk.evaluate_js("""() => {
                const anchor = document.querySelector('#recaptcha-anchor');
                return anchor && anchor.getAttribute('aria-checked') === 'true';
            }""")
            if checked.success and checked.data:
                log_success(f"{self.NAME}: reCAPTCHA checkbox 已勾选")
                return True

            # 可能触发了图片挑战 — 这里只返回 False，让上层决定
            log_warning(f"{self.NAME}: reCAPTCHA 可能触发了图片挑战")
            return False
        finally:
            await tk.exit_iframe()

    async def _bypass_hcaptcha_checkbox(self, det: ChallengeDetection) -> bool:
        """hCaptcha checkbox 绕过，逻辑类似 reCAPTCHA"""
        tk = self.toolkit
        log_agent_action(self.NAME, "绕过 hCaptcha checkbox")

        await self._simulate_human_presence()

        r = await tk.switch_to_iframe(det.iframe_selector)
        if not r.success:
            log_warning(f"{self.NAME}: 无法切入 hCaptcha iframe")
            return False

        try:
            checkbox_sel = '#checkbox, .check'
            exists = await tk.element_exists(checkbox_sel)
            if not (exists.success and exists.data):
                return False

            await self._human_click_element(checkbox_sel)
            await asyncio.sleep(random.uniform(2.0, 4.0))
            return True
        finally:
            await tk.exit_iframe()

    async def _bypass_bing_verify(self, det: ChallengeDetection) -> bool:
        """
        Bing 人机验证绕过:
        Bing 的验证通常是一个简单的按钮点击 + 行为检测。
        策略:
        1. 制造大量人类行为痕迹
        2. 找到并点击验证按钮
        3. 如果有 Turnstile/reCAPTCHA，转发给对应处理器
        """
        tk = self.toolkit
        log_agent_action(self.NAME, "绕过 Bing 人机验证")

        # 大量人类行为
        await self._simulate_human_presence(intensity="high")

        # 检查是否嵌入了第三方验证 (Bing 有时用 Cloudflare Turnstile)
        for sel in self.CF_TURNSTILE_IFRAME_SELECTORS:
            exists = await tk.element_exists(sel)
            if exists.success and exists.data:
                return await self._bypass_cf_turnstile(
                    ChallengeDetection(
                        challenge_type=ChallengeType.CLOUDFLARE_TURNSTILE,
                        iframe_selector=sel,
                    )
                )

        # 尝试点击 Bing 专属验证元素
        if det.action_selector:
            await self._human_click_element(det.action_selector)
            await asyncio.sleep(random.uniform(2.0, 4.0))
            return True

        # 通过 JS 寻找所有可点击的验证按钮
        found = await tk.evaluate_js("""() => {
            const keywords = ['验证', 'verify', 'continue', 'accept', '确认', '继续'];
            const elements = document.querySelectorAll('button, a, input[type="button"], input[type="submit"], [role="button"]');
            for (const el of elements) {
                const style = window.getComputedStyle(el);
                if (!style || style.display === 'none' || style.visibility === 'hidden') continue;
                const rect = el.getBoundingClientRect();
                if (rect.width < 10 || rect.height < 10) continue;
                const text = (el.innerText || el.value || el.textContent || '').toLowerCase().trim();
                for (const kw of keywords) {
                    if (text.includes(kw)) {
                        return {
                            found: true,
                            x: rect.x + rect.width / 2,
                            y: rect.y + rect.height / 2,
                            text: text,
                        };
                    }
                }
            }
            return { found: false };
        }""")

        if found.success and isinstance(found.data, dict) and found.data.get("found"):
            x = found.data["x"]
            y = found.data["y"]
            await self._human_click_at(x, y)
            await asyncio.sleep(random.uniform(2.0, 4.0))
            return True

        # 最后尝试: 等一会儿看看会不会自动通过
        await asyncio.sleep(random.uniform(3.0, 5.0))
        return True

    async def _bypass_cookie_consent(self, det: ChallengeDetection) -> bool:
        """Cookie consent 弹窗: 直接点击接受"""
        tk = self.toolkit
        if det.action_selector:
            log_agent_action(self.NAME, "关闭 Cookie consent 弹窗")
            await self._human_click_element(det.action_selector)
            await asyncio.sleep(random.uniform(0.5, 1.5))
            return True
        return False

    async def _bypass_generic_checkbox(self, det: ChallengeDetection) -> bool:
        """通用 checkbox: 人类化点击"""
        tk = self.toolkit
        if det.action_selector:
            log_agent_action(self.NAME, "点击通用验证 checkbox")
            await self._simulate_human_presence()
            await self._human_click_element(det.action_selector)
            await asyncio.sleep(random.uniform(1.5, 3.0))
            return True
        return False

    async def _bypass_generic_button(self, det: ChallengeDetection) -> bool:
        """通用验证按钮: 人类化点击"""
        tk = self.toolkit
        log_agent_action(self.NAME, "点击通用验证按钮", det.detail)

        await self._simulate_human_presence()

        if det.action_selector:
            await self._human_click_element(det.action_selector)
        else:
            # 没有精确选择器时，通过 keyword 在 JS 中定位并点击
            kw = det.detail.split("'")[1] if "'" in det.detail else "verify"
            await tk.evaluate_js(f"""(kw) => {{
                const els = document.querySelectorAll('button, a, input[type="button"], input[type="submit"], [role="button"]');
                for (const el of els) {{
                    const style = window.getComputedStyle(el);
                    if (!style || style.display === 'none' || style.visibility === 'hidden') continue;
                    const text = (el.innerText || el.value || '').toLowerCase();
                    if (text.includes(kw)) {{
                        el.click();
                        return true;
                    }}
                }}
                return false;
            }}""", kw)

        await asyncio.sleep(random.uniform(2.0, 4.0))
        return True

    async def _bypass_blocked_page(self, det: ChallengeDetection) -> bool:
        """
        通用被阻断页面: 没有明确的挑战类型。
        策略: 制造人类行为 → 刷新页面 → 看是否放行。
        """
        tk = self.toolkit
        log_agent_action(self.NAME, "尝试绕过通用阻断页面")

        await self._simulate_human_presence(intensity="high")
        await asyncio.sleep(random.uniform(2.0, 4.0))

        # 刷新页面
        await tk.refresh()
        await tk.wait_for_load("domcontentloaded", timeout=10000)
        await asyncio.sleep(random.uniform(1.0, 2.0))
        return True

    # ═══════════════════════════════════════════════════════════
    #  拟人化行为模拟
    # ═══════════════════════════════════════════════════════════

    async def _simulate_human_presence(self, intensity: str = "normal") -> None:
        """
        模拟人类在页面上的存在感。
        通过产生真实的鼠标移动、滚动、停顿，让行为检测系统认为这是真人。

        intensity:
          "low"    — 快速扫一下（1-2 动作）
          "normal" — 正常浏览行为（3-5 动作）
          "high"   — 重度模拟（6-10 动作，更多随机性）
        """
        tk = self.toolkit
        actions = {"low": (1, 2), "normal": (3, 5), "high": (6, 10)}
        lo, hi = actions.get(intensity, (3, 5))
        count = random.randint(lo, hi)

        for i in range(count):
            action = random.choices(
                ["move", "scroll", "pause", "micro_move"],
                weights=[0.35, 0.25, 0.2, 0.2],
                k=1,
            )[0]

            if action == "move":
                await self._random_mouse_wander()
            elif action == "scroll":
                pixels = random.randint(50, 300) * random.choice([1, -1])
                if pixels > 0:
                    await tk.scroll_down(pixels)
                else:
                    await tk.scroll_up(abs(pixels))
            elif action == "pause":
                await asyncio.sleep(random.uniform(0.3, 1.5))
            elif action == "micro_move":
                # 小幅度鼠标抖动，模拟手持鼠标时的微动
                await self._micro_mouse_jitter()

            await asyncio.sleep(random.uniform(0.1, 0.5))

    async def _random_mouse_wander(self) -> None:
        """鼠标自然游走，使用贝塞尔曲线而非直线"""
        tk = self.toolkit
        try:
            dims = await tk.evaluate_js(
                "() => ({w: window.innerWidth, h: window.innerHeight})"
            )
            if not (dims.success and dims.data):
                return
            w = dims.data["w"]
            h = dims.data["h"]
            # 目标点: 避开边缘
            target_x = random.randint(int(w * 0.1), int(w * 0.9))
            target_y = random.randint(int(h * 0.1), int(h * 0.9))
            await self._bezier_mouse_move(target_x, target_y, steps=random.randint(15, 35))
        except Exception:
            pass

    async def _micro_mouse_jitter(self) -> None:
        """微小鼠标抖动，模拟人手的不稳定"""
        tk = self.toolkit
        try:
            for _ in range(random.randint(2, 5)):
                dx = random.uniform(-3, 3)
                dy = random.uniform(-3, 3)
                # 获取当前位置然后小幅移动
                dims = await tk.evaluate_js(
                    "() => ({w: window.innerWidth, h: window.innerHeight})"
                )
                if dims.success and dims.data:
                    x = random.randint(200, dims.data["w"] - 200)
                    y = random.randint(200, dims.data["h"] - 200)
                    await tk.mouse_move_to(x + dx, y + dy)
                    await asyncio.sleep(random.uniform(0.02, 0.08))
        except Exception:
            pass

    async def _bezier_mouse_move(
        self, target_x: float, target_y: float, steps: int = 25
    ) -> None:
        """
        用二次贝塞尔曲线模拟自然的鼠标移动轨迹。
        人类移动鼠标不是直线，而是有弧度的曲线，且速度先快后慢。
        """
        tk = self.toolkit

        # 起始点: viewport 中间附近的一个随机点
        start_x = random.randint(200, 800)
        start_y = random.randint(200, 600)

        # 控制点: 在起始和目标之间的随机偏移位置（产生弧度）
        ctrl_x = (start_x + target_x) / 2 + random.uniform(-150, 150)
        ctrl_y = (start_y + target_y) / 2 + random.uniform(-100, 100)

        for i in range(steps + 1):
            t = i / steps
            # 缓动函数: ease-out (先快后慢，更像人类)
            t_eased = 1 - (1 - t) ** 2.5

            # 二次贝塞尔曲线
            x = (1 - t_eased) ** 2 * start_x + 2 * (1 - t_eased) * t_eased * ctrl_x + t_eased ** 2 * target_x
            y = (1 - t_eased) ** 2 * start_y + 2 * (1 - t_eased) * t_eased * ctrl_y + t_eased ** 2 * target_y

            # 加入微小噪声
            x += random.uniform(-1.5, 1.5)
            y += random.uniform(-1.5, 1.5)

            await tk.mouse_move_to(x, y)

            # 移动间隔: 模拟真实速度变化
            base_delay = 0.005 + 0.02 * (1 - abs(t - 0.5) * 2)  # 中间快，两端慢
            await asyncio.sleep(base_delay + random.uniform(0, 0.01))

    async def _human_click_element(self, selector: str) -> None:
        """
        以拟人方式点击元素:
        1. 获取元素位置
        2. 贝塞尔曲线移动到元素附近
        3. 在元素范围内随机偏移（不总是正中心）
        4. 短暂停顿后点击
        """
        tk = self.toolkit

        box_r = await tk.get_bounding_box(selector)
        if box_r.success and box_r.data:
            box = box_r.data
            # 点击位置: 中心附近随机偏移（人不会每次都点正中心）
            x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            await self._human_click_at(x, y)
        else:
            # 没拿到 bounding box，用 Playwright 的 click
            await tk.click(selector)

    async def _human_click_at(self, x: float, y: float) -> None:
        """拟人化坐标点击: 移动 → 停顿 → 按下 → 短暂保持 → 释放"""
        tk = self.toolkit

        # 贝塞尔曲线移过去
        await self._bezier_mouse_move(x, y, steps=random.randint(18, 30))

        # 到达后短暂停顿（人会确认一下再点）
        await asyncio.sleep(random.uniform(0.05, 0.25))

        # mousedown → 保持 → mouseup (人的点击有保持时间)
        await tk.mouse_down_at(x, y)
        await asyncio.sleep(random.uniform(0.04, 0.12))
        await tk.mouse_up()

        # 点击后的微小移动（手指释放时会有）
        await asyncio.sleep(random.uniform(0.05, 0.15))
        await tk.mouse_move_to(x + random.uniform(-2, 2), y + random.uniform(-2, 2))

    # ═══════════════════════════════════════════════════════════
    #  工具方法
    # ═══════════════════════════════════════════════════════════

    async def _wait_for_navigation_after_bypass(self) -> None:
        """绕过操作后等待页面变化"""
        tk = self.toolkit
        try:
            await tk.wait_for_load("domcontentloaded", timeout=8000)
        except Exception:
            pass
        await asyncio.sleep(random.uniform(1.0, 2.5))
