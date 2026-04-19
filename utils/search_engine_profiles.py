import base64
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs, quote_plus, unquote, urlparse


def _clean_host(value: str) -> str:
    return str(value or "").strip().lower().removeprefix("www.")


@dataclass(frozen=True)
class SearchEngineProfile:
    name: str
    domains: Tuple[str, ...]
    homepage: str
    search_url_template: str
    input_selectors: Tuple[str, ...]
    result_selectors: Tuple[str, ...]
    result_path_exact: Tuple[str, ...] = ()
    result_path_prefixes: Tuple[str, ...] = ()
    result_query_keys: Tuple[str, ...] = ("q",)
    redirect_path_exact: Tuple[str, ...] = ()
    redirect_path_prefixes: Tuple[str, ...] = ()
    redirect_query_keys: Tuple[str, ...] = ("uddg", "u", "url", "q", "target", "redirect")
    fallback_result_selectors: Tuple[str, ...] = ()  # tried when result_selectors all miss
    last_verified: str = ""  # date string, e.g. "2026-03-26"

    def build_search_url(self, query: str) -> str:
        return self.search_url_template.format(query=quote_plus(query))

    def matches_host(self, host: str) -> bool:
        normalized_host = _clean_host(host)
        return any(
            normalized_host == domain or normalized_host.endswith(f".{domain}")
            for domain in self.domains
        )

    def matches_results_path(self, path: str) -> bool:
        normalized_path = str(path or "").strip().lower() or "/"
        if normalized_path in self.result_path_exact:
            return True
        return any(normalized_path.startswith(prefix) for prefix in self.result_path_prefixes)

    def looks_like_results_url(self, url: str) -> bool:
        try:
            parsed = urlparse(str(url or ""))
        except Exception:
            return False
        if not self.matches_host(parsed.netloc):
            return False
        query_keys = {
            str(key or "").strip().lower()
            for key in parse_qs(parsed.query or "").keys()
            if str(key or "").strip()
        }
        if not query_keys.intersection(self.result_query_keys):
            return False
        return self.matches_results_path(parsed.path or "/")

    def is_redirect_url(self, url: str) -> bool:
        try:
            parsed = urlparse(str(url or ""))
        except Exception:
            return False
        if not self.matches_host(parsed.netloc):
            return False
        normalized_path = str(parsed.path or "").strip().lower() or "/"
        if normalized_path in self.redirect_path_exact:
            return True
        if any(normalized_path.startswith(prefix) for prefix in self.redirect_path_prefixes):
            return True
        query_keys = {
            str(key or "").strip().lower()
            for key in parse_qs(parsed.query or "").keys()
            if str(key or "").strip()
        }
        return bool(query_keys.intersection(self.redirect_query_keys))


_BING_PROFILE = SearchEngineProfile(
    name="Bing",
    domains=("bing.com",),
    homepage="https://www.bing.com",
    search_url_template="https://www.bing.com/search?q={query}",
    input_selectors=("input[name='q']", "#sb_form_q"),
    result_selectors=(
        "#b_results li.b_algo",
        "#b_results li.b_ans",
        "#b_results .b_algo",
        ".b_algo",
    ),
    fallback_result_selectors=(
        "#b_results li",
        "main ol li",
        "[data-tag='Organic'] li",
    ),
    last_verified="2026-03-26",
    result_path_prefixes=("/search",),
    redirect_path_prefixes=("/ck/a", "/alink/link"),
    redirect_query_keys=("uddg", "u", "url", "target", "redirect", "r"),
)

_BAIDU_PROFILE = SearchEngineProfile(
    name="Baidu",
    domains=("baidu.com",),
    homepage="https://www.baidu.com",
    search_url_template="https://www.baidu.com/s?wd={query}",
    input_selectors=("input[name='wd']", "#kw"),
    result_selectors=(
        "#content_left .result",
        "#content_left .c-container",
        "#content_left .result-op",
        "#content_left .xpath-log",
    ),
    fallback_result_selectors=(
        "#content_left > div",
        "[tpl] .c-container",
        "div[class^='result']",
    ),
    last_verified="2026-03-26",
    result_path_exact=("/s",),
    result_path_prefixes=("/s/",),
    result_query_keys=("wd", "word"),
    redirect_path_prefixes=("/link", "/from"),
    redirect_query_keys=("url", "target", "redirect"),
)

_DUCKDUCKGO_PROFILE = SearchEngineProfile(
    name="DuckDuckGo",
    domains=("duckduckgo.com",),
    homepage="https://duckduckgo.com",
    search_url_template="https://duckduckgo.com/?q={query}",
    input_selectors=("input[name='q']", "input[name='search']", "#searchbox_input"),
    result_selectors=(
        ".results .result",
        ".result",
        ".result__body",
        "[data-testid='result']",
    ),
    fallback_result_selectors=(
        "article[data-testid]",
        "ol li[data-layout]",
        "section ol li",
    ),
    last_verified="2026-03-26",
    result_path_exact=("/", "/html/", "/lite/"),
    result_path_prefixes=("/html", "/lite"),
    redirect_path_prefixes=("/l/",),
    redirect_query_keys=("uddg", "u", "url", "target", "redirect"),
)

_GOOGLE_PROFILE = SearchEngineProfile(
    name="Google",
    domains=("google.com",),
    homepage="https://www.google.com",
    search_url_template="https://www.google.com/search?q={query}",
    input_selectors=("input[name='q']", "textarea[name='q']", "#APjFqb"),
    result_selectors=(
        "#search .tF2Cxc",
        "#search .g",
        "#search [data-snc]",
        "#search .MjjYud",
        "[data-sokoban-container]",
    ),
    fallback_result_selectors=(
        "#rso > div",
        "main article",
        "main section > div > div > a",
        "#search > div > div",
    ),
    last_verified="2026-03-26",
    result_path_prefixes=("/search",),
    redirect_path_prefixes=("/url", "/imgres"),
    redirect_query_keys=("q", "url", "imgurl", "target", "redirect"),
)

_SOGOU_PROFILE = SearchEngineProfile(
    name="Sogou",
    domains=("sogou.com",),
    homepage="https://www.sogou.com",
    search_url_template="https://www.sogou.com/web?query={query}",
    input_selectors=("input[name='query']", "#upquery", "input[name='keyword']"),
    result_selectors=(
        ".results .vrwrap",
        ".results .rb",
        ".results .fb",
        ".vrwrap",
        ".rb",
    ),
    fallback_result_selectors=(
        "#main .results > div",
        ".results div[class]",
        "div[id^='sogou_result']",
    ),
    last_verified="2026-03-26",
    result_path_prefixes=("/web",),
    result_query_keys=("query", "keyword"),
    redirect_path_prefixes=("/link", "/web"),
    redirect_query_keys=("url", "u", "target", "redirect"),
)

# Bing 置于最末：其频繁触发机器人验证，作为兜底而非首选。
SEARCH_ENGINE_PROFILES: Tuple[SearchEngineProfile, ...] = (
    _DUCKDUCKGO_PROFILE,
    _BAIDU_PROFILE,
    _GOOGLE_PROFILE,
    _SOGOU_PROFILE,
    _BING_PROFILE,
)

GENERIC_SEARCH_INPUT_SELECTORS: Tuple[str, ...] = (
    "input[name='q']",
    "textarea[name='q']",
    "input[name='wd']",
    "input[name='query']",
    "input[type='search']",
    "form input[type='text']",
)

GENERIC_SEARCH_RESULT_SELECTORS: Tuple[str, ...] = (
    "#b_results li.b_algo",
    "#b_results li.b_ans",
    "#content_left .result",
    "#content_left .c-container",
    "#search .tF2Cxc",
    "#search .g",
    "#search .MjjYud",
    ".results .result",
    ".result",
    ".result__body",
    ".vrwrap",
    ".rb",
    ".results .fb",
    "[data-testid='result']",
    "[role='main'] article",
)

SEARCH_ENGINE_HOSTS: Tuple[str, ...] = tuple(
    domain
    for profile in SEARCH_ENGINE_PROFILES
    for domain in profile.domains
)


def iter_search_engine_profiles() -> Tuple[SearchEngineProfile, ...]:
    return SEARCH_ENGINE_PROFILES


def find_search_engine_profile(url_or_host: str) -> Optional[SearchEngineProfile]:
    value = str(url_or_host or "").strip()
    if not value:
        return None
    host = _clean_host(urlparse(value).netloc or value)
    if not host:
        return None
    for profile in SEARCH_ENGINE_PROFILES:
        if profile.matches_host(host):
            return profile
    return None


def is_search_engine_host(host: str) -> bool:
    return find_search_engine_profile(host) is not None


def is_search_engine_domain(url_or_host: str) -> bool:
    return find_search_engine_profile(url_or_host) is not None


def looks_like_search_results_url(url: str) -> bool:
    profile = find_search_engine_profile(url)
    return profile.looks_like_results_url(url) if profile else False


def get_search_result_selectors(url_or_host: str, include_generic: bool = True) -> List[str]:
    selectors: List[str] = []
    profile = find_search_engine_profile(url_or_host)
    if profile:
        selectors.extend(profile.result_selectors)
        selectors.extend(profile.fallback_result_selectors)
    if include_generic:
        selectors.extend(GENERIC_SEARCH_RESULT_SELECTORS)
    deduped: List[str] = []
    for selector in selectors:
        if selector not in deduped:
            deduped.append(selector)
    return deduped


def get_search_input_selectors(url_or_host: str = "", include_generic: bool = True) -> List[str]:
    selectors: List[str] = []
    profile = find_search_engine_profile(url_or_host)
    if profile:
        selectors.extend(profile.input_selectors)
    if include_generic:
        selectors.extend(GENERIC_SEARCH_INPUT_SELECTORS)
    deduped: List[str] = []
    for selector in selectors:
        if selector not in deduped:
            deduped.append(selector)
    return deduped


def build_direct_search_urls(query: str, profiles: Optional[Sequence[SearchEngineProfile]] = None) -> List[Tuple[SearchEngineProfile, str]]:
    chosen_profiles = tuple(profiles or SEARCH_ENGINE_PROFILES)
    return [(profile, profile.build_search_url(query)) for profile in chosen_profiles]


def _decode_candidate_value(candidate: str) -> str:
    value = str(candidate or "").strip()
    if not value:
        return ""
    for _ in range(2):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    if value.startswith("http"):
        return value
    if value.startswith("a1"):
        raw = value[2:]
        padding = "=" * ((4 - len(raw) % 4) % 4)
        try:
            decoded = base64.urlsafe_b64decode((raw + padding).encode("ascii")).decode("utf-8", errors="ignore")
        except Exception:
            return ""
        return decoded if decoded.startswith("http") else ""
    return ""


def decode_search_redirect_url(href: str) -> str:
    if not href:
        return ""
    try:
        parsed = urlparse(href)
    except Exception:
        return href
    profile = find_search_engine_profile(parsed.netloc)
    if not profile or not profile.is_redirect_url(href):
        return href
    query_map = parse_qs(parsed.query or "")
    for key in profile.redirect_query_keys:
        for candidate in query_map.get(key, []) or []:
            decoded = _decode_candidate_value(candidate)
            if decoded:
                return decoded
    return href


async def validate_selectors(toolkit: Any, profile: SearchEngineProfile) -> Dict[str, Any]:
    """Check which selectors of a profile actually match on the current page.

    Evaluates each primary and fallback selector via the toolkit's JS engine
    and returns a health report.  Logs a warning when the primary hit-rate
    drops below 50 %.

    Args:
        toolkit: A BrowserToolkit instance (must expose ``evaluate_js``).
        profile: The SearchEngineProfile whose selectors to validate.

    Returns:
        {
            "primary_matched": [...],   # primary selectors that found ≥1 element
            "primary_failed":  [...],   # primary selectors that found 0 elements
            "fallback_matched": [...],  # fallback selectors that found ≥1 element
            "health_score": 0.0-1.0,   # fraction of primary selectors matched
        }
    """
    all_selectors = list(profile.result_selectors) + list(profile.fallback_result_selectors)
    if not all_selectors:
        return {
            "primary_matched": [],
            "primary_failed": [],
            "fallback_matched": [],
            "health_score": 1.0,
        }

    result = await toolkit.evaluate_js(
        """(selectors) => {
            const querySelectorAllDeep = (selector, root, depth) => {
                if ((depth = depth || 0) > 10) return [];
                root = root || document;
                const results = Array.from(root.querySelectorAll(selector));
                for (const el of root.querySelectorAll('*')) {
                    if (el.shadowRoot) results.push(...querySelectorAllDeep(selector, el.shadowRoot, depth + 1));
                }
                return results;
            };
            return selectors.map(sel => ({ sel, count: querySelectorAllDeep(sel).length }));
        }""",
        all_selectors,
    )
    counts: List[Dict[str, Any]] = result.data if (result and result.success and isinstance(result.data, list)) else []

    primary_set = set(profile.result_selectors)
    primary_matched: List[str] = []
    primary_failed: List[str] = []
    fallback_matched: List[str] = []

    for item in counts:
        sel = item.get("sel", "")
        count = int(item.get("count", 0) or 0)
        if sel in primary_set:
            (primary_matched if count > 0 else primary_failed).append(sel)
        elif count > 0:
            fallback_matched.append(sel)

    total_primary = len(profile.result_selectors)
    health_score = len(primary_matched) / total_primary if total_primary else 1.0

    return {
        "primary_matched": primary_matched,
        "primary_failed": primary_failed,
        "fallback_matched": fallback_matched,
        "health_score": health_score,
    }

