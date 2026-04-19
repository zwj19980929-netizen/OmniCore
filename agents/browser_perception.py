"""
BrowserPerceptionLayer - Transforms raw browser state into structured PageObservation.

This is Layer 1 of the three-layer BrowserAgent architecture.
Responsibilities:
- Vision-based methods: describe page, extract data, check relevance, capture screenshot
- Semantic snapshot building and caching
- A11y tree extraction and merging
- Perceiver content integration
- Element extraction and filtering from snapshots
"""
import asyncio
import hashlib
import time
from typing import Any, Dict, List, Optional, Tuple

from config.settings import settings
from core.llm import LLMClient
from utils.accessibility_tree_extractor import AccessibilityTreeExtractor, AccessibleElement
from utils.browser_toolkit import BrowserToolkit, ToolkitResult
from utils.enhanced_page_perceiver import EnhancedPagePerceiver, PageContent
from utils.logger import log_agent_action, log_warning
from utils.prompt_manager import get_prompt
from utils.perception_scripts import SCRIPT_EXTRACT_INTERACTIVE_ELEMENTS
from utils.page_fingerprint import compute_page_hash, normalize_url_path
from utils.vision_cache import get_vision_cache, should_bypass_for_task
import utils.web_debug_recorder as web_debug_recorder

from agents.browser_agent import (
    PageElement,
    PageObservation,
    SearchResultCard,
    BrowserAction,
    TaskIntent,
    PageState,
)


class BrowserPerceptionLayer:
    """Transforms raw browser state into structured PageObservation.

    Encapsulates all perception-related logic: semantic snapshots, a11y tree
    extraction, perceiver integration, vision description, element filtering,
    and snapshot caching.
    """

    def __init__(
        self,
        toolkit: BrowserToolkit,
        llm_client_getter,
        a11y_extractor: AccessibilityTreeExtractor,
        page_perceiver: EnhancedPagePerceiver,
        agent_name: str = "BrowserAgent",
    ):
        self.toolkit = toolkit
        self._get_llm = llm_client_getter
        self.a11y_extractor = a11y_extractor
        self.page_perceiver = page_perceiver
        self.name = agent_name

        # Vision LLM (lazy init)
        self._vision_llm: Optional[LLMClient] = None
        self._vision_llm_attempted = False
        self._vision_llm_unavailable_logged = False

        # Snapshot caching
        self._snapshot_version: int = 0
        self._last_snapshot_hash: str = ""
        self._last_observation: Optional[PageObservation] = None
        self._last_semantic_snapshot: Dict[str, Any] = {}

        # Track URL for new-page vision trigger
        self._last_vision_url: str = ""

        # Task description for vision-cache bypass decisions (B3).
        # Owner (BrowserAgent) sets this at the start of each run.
        self.current_task: str = ""

    # ── toolkit call helper ──────────────────────────────────

    async def _call_toolkit(self, method_name: str, *args: Any, **kwargs: Any) -> ToolkitResult:
        method = getattr(self.toolkit, method_name, None)
        if not callable(method):
            return ToolkitResult(success=False, error=f"{method_name} unavailable")
        try:
            result = await method(*args, **kwargs)
        except Exception as exc:
            return ToolkitResult(success=False, error=str(exc))
        if isinstance(result, ToolkitResult):
            return result
        return ToolkitResult(success=True, data=result)

    # ── basic page info ──────────────────────────────────────

    async def get_current_url(self, fallback: str = "") -> str:
        result = await self._call_toolkit("get_current_url")
        if result.success and result.data is not None:
            value = str(result.data or "").strip()
            if value:
                return value
        for attr_name in ("current_url", "_current_url", "_url"):
            value = str(getattr(self.toolkit, attr_name, "") or "").strip()
            if value:
                return value
        return str(fallback or "")

    async def get_title(self, fallback: str = "") -> str:
        result = await self._call_toolkit("get_title")
        if result.success and result.data is not None:
            value = str(result.data or "").strip()
            if value:
                return value
        for attr_name in ("title", "_title"):
            value = str(getattr(self.toolkit, attr_name, "") or "").strip()
            if value:
                return value
        return str(fallback or "")

    async def get_page_html(self) -> str:
        result = await self._call_toolkit("get_page_html")
        if result.success and result.data is not None:
            return str(result.data or "")
        for attr_name in ("html", "_html"):
            value = getattr(self.toolkit, attr_name, None)
            if value:
                return str(value)
        return ""

    # ── Vision LLM ───────────────────────────────────────────

    def get_vision_llm(self) -> Optional[LLMClient]:
        if self._vision_llm is not None:
            return self._vision_llm
        if self._vision_llm_attempted:
            return None
        self._vision_llm_attempted = True
        try:
            self._vision_llm = LLMClient.for_vision()
            return self._vision_llm
        except Exception as exc:
            if not self._vision_llm_unavailable_logged:
                log_warning(f"vision llm unavailable: {exc}")
                self._vision_llm_unavailable_logged = True
            return None

    # ── Semantic snapshot ────────────────────────────────────

    async def build_fallback_semantic_snapshot(self) -> Dict[str, Any]:
        """Build a fallback semantic snapshot when toolkit.semantic_snapshot is unavailable."""
        from agents.browser_agent import BrowserAgent
        # Delegate to the existing implementation on a temporary basis.
        # The full method body is in browser_agent.py and will be
        # migrated here in a future pass.
        # TODO: Move _build_fallback_semantic_snapshot body here fully.
        # For now, we keep it as a delegation placeholder that the
        # orchestrator calls.
        raise NotImplementedError("Use orchestrator._build_fallback_semantic_snapshot")

    async def get_semantic_snapshot(self) -> Dict[str, Any]:
        """Get the semantic snapshot from toolkit, with fallback."""
        if hasattr(self.toolkit, "semantic_snapshot"):
            snapshot_r = await self._call_toolkit("semantic_snapshot", max_elements=80, include_cards=True)
            if snapshot_r.success and isinstance(snapshot_r.data, dict):
                self._last_semantic_snapshot = snapshot_r.data
                web_debug_recorder.write_json("browser_semantic_snapshot", self._last_semantic_snapshot)

                if web_debug_recorder.is_enabled():
                    log_warning(f"[DEBUG] ========== Semantic Snapshot ==========")
                    log_warning(f"[DEBUG] page_type: {self._last_semantic_snapshot.get('page_type', 'unknown')}")
                    log_warning(f"[DEBUG] page_stage: {self._last_semantic_snapshot.get('page_stage', 'unknown')}")
                    log_warning(f"[DEBUG] elements: {len(self._last_semantic_snapshot.get('elements', []))}")
                    log_warning(f"[DEBUG] cards: {len(self._last_semantic_snapshot.get('cards', []))}")
                    log_warning(f"[DEBUG] collections: {len(self._last_semantic_snapshot.get('collections', []))}")
                    log_warning(f"[DEBUG] ====================================")

                return self._last_semantic_snapshot
            else:
                log_warning(f"semantic_snapshot main path failed: {snapshot_r.error or 'unknown error'}")

        # Fallback will be handled by orchestrator
        return self._last_semantic_snapshot

    # ── Snapshot caching & hash ──────────────────────────────

    def compute_snapshot_hash(self, snapshot: Dict[str, Any]) -> str:
        """Hash of url + element count + first 5 element names for cache invalidation."""
        url = str(snapshot.get("url", "") or "")
        elements = snapshot.get("elements") or []
        elem_count = len(elements)
        first_names = "|".join(
            str(e.get("text", "") or "")[:30]
            for e in elements[:5]
            if isinstance(e, dict)
        )
        raw = f"{url}|{elem_count}|{first_names}"
        return hashlib.md5(raw.encode("utf-8", errors="ignore")).hexdigest()

    def invalidate_cache(self) -> None:
        """Invalidate the observation cache (call after state-changing actions)."""
        self._last_snapshot_hash = ""
        self._last_observation = None

    # ── A11y merging ─────────────────────────────────────────

    def merge_a11y_into_snapshot(
        self, snapshot: Dict[str, Any], a11y_elements: List[AccessibleElement]
    ) -> None:
        """Enrich snapshot with a11y elements."""
        if not a11y_elements:
            return

        js_elements = snapshot.get("elements") or []
        js_by_selector: Dict[str, Dict] = {}
        for el in js_elements:
            sel = str(el.get("selector", "") or "")
            if sel:
                js_by_selector[sel] = el

        merged_elements: List[Dict[str, Any]] = []
        for idx, ae in enumerate(a11y_elements[:80]):
            js_match = js_by_selector.get(ae.selector, {})
            merged_elements.append({
                "ref": ae.ref,
                "role": ae.role,
                "tag": ae.tag or js_match.get("tag", ""),
                "type": ae.role,
                "text": ae.name,
                "href": ae.attributes.get("href", "") or js_match.get("href", ""),
                "value": ae.attributes.get("value", "") or js_match.get("value", ""),
                "label": ae.name,
                "placeholder": ae.attributes.get("placeholder", "") or js_match.get("placeholder", ""),
                "selector": ae.selector or js_match.get("selector", ""),
                "visible": ae.is_visible,
                "enabled": True,
                "region": ae.region or js_match.get("region", "body"),
                "parent_ref": ae.parent_ref,
                "bbox": ae.bbox or js_match.get("bbox", {}),
            })

        if len(merged_elements) >= max(len(js_elements) // 3, 3):
            snapshot["elements"] = merged_elements
        snapshot["a11y_element_count"] = len(a11y_elements)

    def merge_perceiver_content(
        self, snapshot: Dict[str, Any], page_content: PageContent
    ) -> None:
        """Enrich snapshot with perceiver's headings, text blocks, and summary."""
        if page_content.main_headings:
            snapshot["headings"] = [
                {"level": f"h{min(i, 3)}", "text": h}
                for i, h in enumerate(page_content.main_headings, 1)
            ]

        existing_blocks = snapshot.get("visible_text_blocks") or []
        existing_texts = {str(b.get("text", "") or "")[:60] for b in existing_blocks if isinstance(b, dict)}
        for block in page_content.text_blocks:
            if block[:60] not in existing_texts:
                existing_blocks.append({"kind": "p", "text": block[:320], "selector": "", "parent_ref": ""})
                existing_texts.add(block[:60])
        snapshot["visible_text_blocks"] = existing_blocks[:20]

        if page_content.page_summary:
            snapshot["page_summary"] = page_content.page_summary

    # ── URL comparison for new-page detection ───────────────

    @staticmethod
    def _normalize_url_for_compare(url: str) -> str:
        """Strip fragment and trailing slash for URL comparison."""
        import re
        normalized = re.sub(r"#.*$", "", str(url or "").strip())
        return normalized.rstrip("/").lower()

    def _is_new_url(self, current_url: str) -> bool:
        """Check if current URL differs from last URL where vision was triggered."""
        if not current_url:
            return False
        normalized = self._normalize_url_for_compare(current_url)
        return normalized != self._last_vision_url

    # ── Complexity score ─────────────────────────────────────

    def compute_complexity_score(
        self, snapshot: Dict[str, Any], a11y_elements: List[AccessibleElement]
    ) -> float:
        """Ratio of unnamed/ambiguous elements -- high score means poor perception."""
        elements = a11y_elements or []
        if not elements:
            elements_data = snapshot.get("elements") or []
            if not elements_data:
                return 1.0
            unnamed = sum(1 for e in elements_data if not str(e.get("text", "") or "").strip())
            return unnamed / max(len(elements_data), 1)
        unnamed = sum(1 for e in elements if not e.name.strip())
        return unnamed / max(len(elements), 1)

    # ── Vision description ───────────────────────────────────

    async def get_vision_description(self, page) -> str:
        """Take screenshot and describe via vision model."""
        if not self._vision_llm and not self._vision_llm_attempted:
            self._vision_llm_attempted = True
            try:
                self._vision_llm = LLMClient.for_vision()
            except Exception:
                if not self._vision_llm_unavailable_logged:
                    log_warning("vision LLM unavailable for perception")
                    self._vision_llm_unavailable_logged = True
                return ""

        if not self._vision_llm:
            return ""

        try:
            screenshot_bytes = await page.screenshot(type="jpeg", quality=50, full_page=False)
            response = self._vision_llm.chat_with_image(
                text="Describe this webpage briefly: layout, main content area, key interactive elements, any modals/overlays. Be concise (2-3 sentences).",
                image=screenshot_bytes,
                temperature=0.2,
                max_tokens=300,
            )
            content = getattr(response, "content", None)
            if isinstance(content, str):
                return content.strip()[:500]
            return str(content or "").strip()[:500]
        except Exception as exc:
            log_warning(f"vision description failed: {exc}")
            return ""

    # ── Vision-based data extraction ─────────────────────────

    async def extract_data_with_vision(
        self,
        task: str,
        task_intent: TaskIntent,
        snapshot: Optional[Dict[str, Any]] = None,
        derive_primary_query_fn=None,
    ) -> List[Dict[str, str]]:
        """Screenshot -> vision model -> extract structured data (fallback when DOM extraction fails)."""
        vision_llm = self.get_vision_llm()
        if vision_llm is None:
            return []

        screenshot_r = await self.toolkit.screenshot(full_page=False)
        if not screenshot_r.success or not screenshot_r.data:
            return []

        query = task_intent.query
        if not query and derive_primary_query_fn:
            query = derive_primary_query_fn(task)
        fields = ", ".join(f"{k}: {v}" for k, v in task_intent.fields.items()) if task_intent.fields else "auto-detect"
        page_type = str((snapshot or {}).get("page_type", "unknown"))

        prompt_template = get_prompt("browser_vision_data_extraction", "")
        if prompt_template:
            prompt = prompt_template.format(task=task, query=query, fields=fields, page_type=page_type)
        else:
            prompt = (
                f"Extract data from this webpage screenshot.\n"
                f"Task: {task}\nQuery: {query}\nFields: {fields}\n"
                f"Return JSON: {{\"found\": true/false, \"items\": [{{...}}]}}"
            )

        try:
            response = await asyncio.to_thread(
                vision_llm.chat_with_image, prompt, screenshot_r.data, 0.2, 2000,
            )
            web_debug_recorder.write_text("vision_data_extraction_prompt", prompt)
            web_debug_recorder.write_text("vision_data_extraction_response", response.content)

            parsed = vision_llm.parse_json_response(response)
            if not parsed.get("found"):
                return []
            items = parsed.get("items") or parsed.get("data") or []
            if isinstance(items, dict):
                items = [items]
            if not isinstance(items, list):
                return []
            result = []
            for item in items:
                if isinstance(item, dict):
                    result.append({str(k): str(v) for k, v in item.items()})
            log_agent_action(self.name, f"vision extraction ok: {len(result)} items")
            return result
        except Exception as exc:
            log_warning(f"vision data extraction failed: {exc}")
            return []

    # ── Vision relevance check ───────────────────────────────

    async def check_relevance(
        self,
        task: str,
        query: str,
        snapshot: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str]:
        """Screenshot -> vision model -> determine if page contains task-relevant info."""
        vision_llm = self.get_vision_llm()
        if vision_llm is None:
            return (False, "")

        screenshot_r = await self.toolkit.screenshot(full_page=False)
        if not screenshot_r.success or not screenshot_r.data:
            return (False, "")

        prompt_template = get_prompt("browser_vision_relevance_check", "")
        if prompt_template:
            prompt = prompt_template.format(task=task, query=query or task)
        else:
            prompt = (
                f"Does this webpage contain information relevant to: '{query or task}'?\n"
                f"Return JSON: {{\"relevant\": true/false, \"confidence\": 0.0-1.0, \"summary\": \"...\"}}"
            )

        try:
            response = await asyncio.to_thread(
                vision_llm.chat_with_image, prompt, screenshot_r.data, 0.2, 500,
            )
            web_debug_recorder.write_text("vision_relevance_prompt", prompt)
            web_debug_recorder.write_text("vision_relevance_response", response.content)

            parsed = vision_llm.parse_json_response(response)
            relevant = bool(parsed.get("relevant", False))
            confidence = float(parsed.get("confidence", 0.0))
            summary = str(parsed.get("summary", ""))
            if relevant and confidence >= 0.5:
                log_agent_action(self.name, f"vision: page relevant: {summary[:80]}")
                return (True, summary)
            return (False, summary)
        except Exception as exc:
            log_warning(f"vision relevance check failed: {exc}")
            return (False, "")

    # ── Capture screenshot ───────────────────────────────────

    async def capture_screenshot(self) -> Optional[bytes]:
        """Capture current page screenshot for Critic visual verification."""
        try:
            sc = await self.toolkit.screenshot(full_page=False)
            return sc.data if sc.success else None
        except Exception:
            return None

    # ── iframe element extraction ────────────────────────────

    async def _extract_iframe_elements(self) -> List[Dict[str, Any]]:
        """Extract interactive elements from all accessible non-main frames.

        Elements are tagged with region="iframe" and carry a frame_url field
        so the decision layer can distinguish them from main-frame elements.
        Cross-origin frames that block JS execution are silently skipped.
        """
        page = getattr(self.toolkit, '_page', None)
        if not page:
            return []
        iframe_elements: List[Dict[str, Any]] = []
        for frame in page.frames:
            if frame == page.main_frame or frame.is_detached():
                continue
            try:
                raw = await frame.evaluate(SCRIPT_EXTRACT_INTERACTIVE_ELEMENTS)
                if not isinstance(raw, list):
                    continue
                for el in raw:
                    if isinstance(el, dict):
                        el["region"] = "iframe"
                        el["frame_url"] = frame.url
                        iframe_elements.append(el)
            except Exception:
                continue  # cross-origin / detached frames may raise
        return iframe_elements

    # ── Unified observe pipeline ─────────────────────────────

    async def observe(self, get_snapshot_fn) -> PageObservation:
        """
        Unified page observation: JS snapshot + a11y tree + perceiver + vision.

        Args:
            get_snapshot_fn: async callable that returns the semantic snapshot dict.
                             This is provided by the orchestrator since the fallback
                             snapshot builder still lives there.
        """
        snapshot = await get_snapshot_fn()

        # Snapshot caching: check if page changed
        snap_hash = self.compute_snapshot_hash(snapshot)
        if snap_hash == self._last_snapshot_hash and self._last_observation is not None:
            self._snapshot_version += 1
            self._last_observation.snapshot_version = self._snapshot_version
            log_agent_action(self.name, "observe", "cache_hit")
            return self._last_observation

        self._last_snapshot_hash = snap_hash
        self._snapshot_version += 1

        # A11y tree extraction
        a11y_elements: List[AccessibleElement] = []
        page = getattr(self.toolkit, '_page', None)
        if page:
            try:
                a11y_elements = await self.a11y_extractor.extract_tree(page)
                if a11y_elements:
                    self.merge_a11y_into_snapshot(snapshot, a11y_elements)
                    log_agent_action(self.name, "observe", f"a11y_elements={len(a11y_elements)}")
            except Exception as exc:
                log_warning(f"a11y extraction failed: {exc}")

        # iframe element extraction
        try:
            iframe_elems = await self._extract_iframe_elements()
            if iframe_elems:
                existing = snapshot.get("elements") or []
                snapshot["elements"] = list(existing) + iframe_elems
                log_agent_action(self.name, "observe", f"iframe_elements={len(iframe_elems)}")
        except Exception as exc:
            log_warning(f"iframe element extraction failed: {exc}")

        # B4: attach frames/tabs metadata so the decision prompt can surface
        # SWITCH_IFRAME / SWITCH_TAB actions. Both enumerations are best-effort
        # and gate behind config flags.
        if settings.BROWSER_IFRAME_ENABLED:
            try:
                frames_r = await self._call_toolkit("list_frames", include_main=True)
                if frames_r.success and isinstance(frames_r.data, list):
                    snapshot["available_frames"] = frames_r.data
            except Exception as exc:
                log_warning(f"list_frames failed: {exc}")
        if settings.BROWSER_TAB_MANAGEMENT_ENABLED:
            try:
                tabs_r = await self._call_toolkit("list_tabs")
                if tabs_r.success and isinstance(tabs_r.data, list):
                    snapshot["available_tabs"] = tabs_r.data
            except Exception as exc:
                log_warning(f"list_tabs failed: {exc}")

        # Perceiver content
        page_content: Optional[PageContent] = None
        headings: List[Dict[str, str]] = []
        if page:
            try:
                page_content = await self.page_perceiver.perceive_page(page, snapshot=snapshot)
                if page_content:
                    self.merge_perceiver_content(snapshot, page_content)
                    headings = snapshot.get("headings") or []
            except Exception as exc:
                log_warning(f"perceiver extraction failed: {exc}")

        # Vision perception (conditional)
        vision_description = ""
        if settings.VISION_PERCEPTION_ENABLED and page:
            complexity = self.compute_complexity_score(snapshot, a11y_elements)
            page_type = str(snapshot.get("page_type", "") or "")
            # Detect new page by comparing current URL with last vision URL
            current_url = str(snapshot.get("url", "") or "")
            is_new_page = settings.VISION_ON_NEW_PAGE and self._is_new_url(current_url)
            need_vision = (
                is_new_page
                or complexity > settings.VISION_PERCEPTION_COMPLEXITY_THRESHOLD
                or page_type == "unknown"
            )
            if need_vision:
                bypass = should_bypass_for_task(self.current_task)
                cache = None if bypass else get_vision_cache()
                page_hash = ""
                cache_hit = False
                if cache is not None:
                    try:
                        page_hash = compute_page_hash(current_url, snapshot)
                    except Exception as exc:
                        log_warning(f"page hash failed: {exc}")
                        page_hash = ""
                    if page_hash:
                        cached = cache.get(page_hash)
                        if cached is not None and cached.description:
                            vision_description = cached.description
                            cache_hit = True
                            if current_url:
                                self._last_vision_url = self._normalize_url_for_compare(current_url)
                            log_agent_action(
                                self.name,
                                "observe",
                                f"vision_cache_hit hash={page_hash[:8]} hits={cached.hit_count}",
                            )
                if not cache_hit:
                    try:
                        vision_description = await self.get_vision_description(page)
                        if vision_description:
                            if current_url:
                                self._last_vision_url = self._normalize_url_for_compare(current_url)
                            trigger = "new_page" if is_new_page else ("complexity" if complexity > settings.VISION_PERCEPTION_COMPLEXITY_THRESHOLD else "unknown_type")
                            log_agent_action(self.name, "observe", f"vision_len={len(vision_description)} trigger={trigger}")
                            if cache is not None and page_hash:
                                cache.set(page_hash, vision_description, normalize_url_path(current_url))
                    except Exception as exc:
                        log_warning(f"vision perception failed: {exc}")

        # Apply versioned refs
        version = self._snapshot_version
        for elem in snapshot.get("elements") or []:
            if isinstance(elem, dict) and elem.get("ref"):
                elem["ref"] = f"{version}:{elem['ref']}"
        for card in snapshot.get("cards") or []:
            if isinstance(card, dict):
                if card.get("ref"):
                    card["ref"] = f"{version}:{card['ref']}"
                if card.get("target_ref"):
                    card["target_ref"] = f"{version}:{card['target_ref']}"
        for ctrl in snapshot.get("controls") or []:
            if isinstance(ctrl, dict) and ctrl.get("ref"):
                ctrl["ref"] = f"{version}:{ctrl['ref']}"
        # Update toolkit ref_map with versioned keys
        if hasattr(self.toolkit, '_semantic_ref_map'):
            old_map = dict(self.toolkit._semantic_ref_map)
            new_map = {}
            for k, v in old_map.items():
                new_map[f"{version}:{k}"] = v
                new_map[k] = v  # keep unversioned for backward compat
            self.toolkit._semantic_ref_map = new_map

        observation = PageObservation(
            snapshot=snapshot,
            a11y_elements=a11y_elements,
            page_content=page_content,
            vision_description=vision_description,
            snapshot_version=self._snapshot_version,
            timestamp=time.time(),
            headings=headings,
        )
        self._last_observation = observation
        return observation

    # ── Element conversion helpers ───────────────────────────

    @staticmethod
    def elements_from_snapshot(snapshot: Dict[str, Any]) -> List[PageElement]:
        """Convert snapshot element dicts to PageElement list."""
        elements: List[PageElement] = []
        for index, item in enumerate((snapshot.get("elements", []) or [])[:60]):
            if not isinstance(item, dict):
                continue
            elements.append(
                PageElement(
                    index=int(item.get("index", index)),
                    tag=str(item.get("tag", "") or ""),
                    text=str(item.get("text", "") or ""),
                    element_type=str(item.get("type", item.get("role", "")) or ""),
                    selector=str(item.get("selector", "") or ""),
                    ref=str(item.get("ref", "") or ""),
                    role=str(item.get("role", "") or ""),
                    attributes={
                        "href": str(item.get("href", "") or ""),
                        "value": str(item.get("value", "") or ""),
                        "placeholder": str(item.get("placeholder", "") or ""),
                        "labelText": str(item.get("label", "") or ""),
                        "ariaLabel": str(item.get("label", "") or ""),
                    },
                    is_visible=bool(item.get("visible", True)),
                    is_clickable=bool(item.get("enabled", True)),
                    parent_ref=str(item.get("parent_ref", "") or ""),
                    region=str(item.get("region", "") or ""),
                )
            )
        return elements

    @staticmethod
    def cards_from_snapshot(snapshot: Optional[Dict[str, Any]]) -> List[SearchResultCard]:
        """Convert snapshot card dicts to SearchResultCard list."""
        cards: List[SearchResultCard] = []
        for item in (snapshot or {}).get("cards", []) or []:
            if not isinstance(item, dict):
                continue
            cards.append(
                SearchResultCard(
                    ref=str(item.get("ref", "") or ""),
                    title=str(item.get("title", "") or ""),
                    target_ref=str(item.get("target_ref", "") or ""),
                    target_selector=str(item.get("target_selector", "") or ""),
                    link=str(item.get("link", "") or ""),
                    raw_link=str(item.get("raw_link", "") or ""),
                    target_url=str(item.get("target_url", "") or ""),
                    snippet=str(item.get("snippet", "") or ""),
                    source=str(item.get("source", "") or ""),
                    host=str(item.get("host", "") or ""),
                    date=str(item.get("date", "") or ""),
                    rank=int(item.get("rank", 0) or 0),
                )
            )
        return cards

    # ── Snapshot accessors ───────────────────────────────────

    @staticmethod
    def get_snapshot_blocked_signals(snapshot: Optional[Dict[str, Any]]) -> List[str]:
        signals: List[str] = []
        for item in (snapshot or {}).get("blocked_signals", []) or []:
            text = ""
            if isinstance(item, dict):
                text = str(item.get("text", "") or item.get("signal", "") or "").strip()
            else:
                text = str(item or "").strip()
            if text and text not in signals:
                signals.append(text)
        return signals

    @staticmethod
    def get_snapshot_visible_text_blocks(snapshot: Optional[Dict[str, Any]]) -> List[str]:
        blocks: List[str] = []
        for item in (snapshot or {}).get("visible_text_blocks", []) or []:
            text = ""
            if isinstance(item, dict):
                text = str(item.get("text", "") or "").strip()
            else:
                text = str(item or "").strip()
            if text:
                blocks.append(text)
        return blocks

    @staticmethod
    def get_snapshot_main_text(snapshot: Optional[Dict[str, Any]]) -> str:
        return str((snapshot or {}).get("main_text", "") or "").strip()

    @staticmethod
    def get_snapshot_affordances(snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        affordances = (snapshot or {}).get("affordances", {}) or {}
        return affordances if isinstance(affordances, dict) else {}

    @staticmethod
    def collections_from_snapshot(snapshot: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        collections: List[Dict[str, Any]] = []
        for item in (snapshot or {}).get("collections", []) or []:
            if not isinstance(item, dict):
                continue
            collections.append(
                {
                    "ref": str(item.get("ref", "") or ""),
                    "kind": str(item.get("kind", "") or ""),
                    "item_count": int(item.get("item_count", 0) or 0),
                    "sample_items": [
                        str(sample or "")
                        for sample in (item.get("sample_items", []) or [])[:5]
                        if str(sample or "").strip()
                    ],
                }
            )
        return collections

    @property
    def snapshot_version(self) -> int:
        return self._snapshot_version

    @property
    def last_observation(self) -> Optional[PageObservation]:
        return self._last_observation

    @last_observation.setter
    def last_observation(self, value: Optional[PageObservation]):
        self._last_observation = value

    @property
    def last_semantic_snapshot(self) -> Dict[str, Any]:
        return self._last_semantic_snapshot

    @last_semantic_snapshot.setter
    def last_semantic_snapshot(self, value: Dict[str, Any]):
        self._last_semantic_snapshot = value
