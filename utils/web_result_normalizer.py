"""
Generic structured-result normalization for web extraction workers.
"""
from __future__ import annotations

import re
import base64
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs, unquote, urlparse


FIELD_ALIASES: Dict[str, Set[str]] = {
    "title": {"title", "headline", "name", "subject", "topic", "question", "story", "article", "repo", "repository", "project", "标题", "名称", "主题", "仓库", "项目"},
    "url": {"url", "link", "href", "website", "address", "canonical_url", "target_url", "detail_url", "链接", "网址", "地址"},
    "summary": {"summary", "description", "snippet", "excerpt", "abstract", "content", "body", "details", "desc", "overview", "摘要", "描述", "简介", "正文", "内容", "概述"},
    "date": {"date", "time", "published", "publish_date", "created", "updated", "timestamp", "datetime", "日期", "时间", "发布时间", "更新时间"},
    "author": {"author", "owner", "creator", "publisher", "by", "作者", "发布者", "所有者"},
    "source": {"source", "site", "host", "domain", "来源", "站点", "网站", "域名"},
    "location": {"location", "city", "country", "place", "region", "地点", "城市", "国家", "地区"},
    "price": {"price", "cost", "amount", "价格", "金额", "售价"},
    "rating": {"rating", "score", "stars", "评分", "分数", "星级"},
    "comments": {"comments", "comment_count", "评论", "评论数"},
    "points": {"points", "likes", "stars", "积分", "点赞", "喜欢"},
}

STRICT_NOISE_TEXTS = {
    "home", "homepage", "login", "log in", "sign in", "register", "sign up",
    "menu", "next", "previous", "prev", "privacy", "terms",
    "contact", "about", "search", "more",
    "首页", "导航", "下一页", "上一页", "搜索", "更多",
    "登录", "注册", "隐私", "条款", "设置",
}

NOISE_URL_HINTS = (
    "javascript:", "/login", "/signin", "/sign-in", "/signup", "/sign-up", "/register",
    "/privacy", "/terms", "/contact", "/about", "/settings", "/preferences",
)

SEARCH_ENGINE_HOSTS = (
    "bing.com",
    "baidu.com",
    "google.com",
    "duckduckgo.com",
    "sogou.com",
    "so.com",
    "yahoo.com",
    "yandex.com",
)

SEARCH_INTERMEDIARY_PATH_HINTS = (
    "/search",
    "/images/search",
    "/image/search",
    "/videos/search",
    "/video/search",
    "/news/search",
    "/visualsearch",
    "/imgres",
    "/url",
    "/ck/a",
    "/copilotsearch",
    "/sorry",
    "/sorry/index",
)

SEARCH_INTERMEDIARY_QUERY_KEYS = {
    "q",
    "query",
    "form",
    "first",
    "idpp",
    "mediaurl",
    "thid",
    "expw",
    "exph",
    "view",
    "simid",
}

LIST_FILTER_QUERY_KEYS = {
    "page",
    "p",
    "offset",
    "start",
    "sort",
    "order",
    "filter",
    "filters",
    "facet",
    "facets",
    "tag",
    "tags",
    "category",
    "categories",
    "type",
    "tab",
    "library",
    "pipeline_tag",
    "provider",
    "q",
    "query",
    "search",
}

DETAIL_QUERY_KEYS = {
    "id",
    "item",
    "doc",
    "docid",
    "article",
    "article_id",
    "post",
    "story",
    "entry",
    "detail",
}

NAV_PATH_SEGMENTS = {
    "about",
    "account",
    "accounts",
    "blog",
    "contact",
    "docs",
    "documentation",
    "enterprise",
    "help",
    "join",
    "license",
    "login",
    "logout",
    "menu",
    "pricing",
    "privacy",
    "register",
    "search",
    "settings",
    "signin",
    "signup",
    "support",
    "terms",
}

CONTENT_FIELDS = {
    "title", "summary", "date", "author", "source", "location",
    "price", "rating", "comments", "points",
}


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def normalize_url_candidate(value: Any) -> str:
    normalized = normalize_text(value)
    if not normalized:
        return ""
    if not normalized.startswith(("http://", "https://")):
        return normalized

    if "›" in normalized or " > " in normalized:
        parts = [normalize_text(part) for part in re.split(r"\s*[›>]\s*", normalized) if normalize_text(part)]
        if parts:
            base = parts[0].split()[0]
            segments: List[str] = []
            for part in parts[1:]:
                token = normalize_text(part).split()[0].strip("/ ")
                if not token:
                    continue
                if re.search(r"[一-鿿]", token):
                    break
                if not re.fullmatch(r"[A-Za-z0-9._~!$&'()*+,;=:@%-]+(?:/[A-Za-z0-9._~!$&'()*+,;=:@%-]+)*", token):
                    break
                segments.extend(segment for segment in token.split("/") if segment)
            if segments:
                return base.rstrip("/") + "/" + "/".join(segments)
            return base

    if re.search(r"\s", normalized):
        return normalized.split()[0]
    return normalized


def tokenize_text(value: str) -> List[str]:
    return [
        token
        for token in re.findall(r"[\u4e00-\u9fff]{1,}|[A-Za-z0-9][A-Za-z0-9_+.-]{1,}", normalize_text(value).lower())
        if token
    ]


def canonical_field_name(field_name: str) -> str:
    normalized = normalize_text(field_name).lower().replace(" ", "_")
    if not normalized:
        return ""
    for canonical, aliases in FIELD_ALIASES.items():
        if normalized == canonical or normalized in aliases:
            return canonical
        if normalized.endswith("_url") and canonical == "url":
            return canonical
    return normalized


def infer_requested_fields(
    task_description: str,
    understanding: Optional[Dict[str, Any]] = None,
) -> List[str]:
    requested: List[str] = []
    seen: Set[str] = set()

    def _append(field_name: str) -> None:
        canonical = canonical_field_name(field_name)
        if not canonical or canonical in seen:
            return
        seen.add(canonical)
        requested.append(canonical)

    for field_name in (understanding or {}).get("key_fields", []) or []:
        _append(str(field_name))

    task_text = normalize_text(task_description).lower()
    for canonical, aliases in FIELD_ALIASES.items():
        tokens = aliases | {canonical}
        if canonical == "url":
            tokens |= {"url", "链接", "网址"}
        if any(token.lower() in task_text for token in tokens):
            _append(canonical)

    page_type = normalize_text((understanding or {}).get("page_type", "")).lower()
    if not requested and page_type in {"serp", "list", "table", "列表页", "搜索结果页"}:
        _append("title")
        _append("url")
    if not requested and page_type in {"detail", "详情页"}:
        _append("title")
        _append("summary")
        _append("url")
    if not requested:
        _append("title")
        _append("url")
    return requested


def looks_like_url(value: str) -> bool:
    normalized = normalize_url_candidate(value).lower()
    return normalized.startswith(("http://", "https://")) or normalized.startswith("mailto:")


def is_search_intermediary_url(value: str) -> bool:
    normalized = normalize_text(value)
    if not normalized:
        return False
    try:
        parsed = urlparse(normalized)
    except Exception:
        return False

    host = (parsed.netloc or "").lower()
    path = (parsed.path or "").lower()
    if not host or not any(host == domain or host.endswith(f'.{domain}') for domain in SEARCH_ENGINE_HOSTS):
        return False

    if any(path == hint or path.startswith(f"{hint}/") for hint in SEARCH_INTERMEDIARY_PATH_HINTS):
        return True

    query_keys = {str(key or '').strip().lower() for key in parse_qs(parsed.query or '').keys() if str(key or '').strip()}
    if path.startswith('/images/') or path.startswith('/video/') or path.startswith('/news/'):
        return True
    if query_keys & SEARCH_INTERMEDIARY_QUERY_KEYS and '/search' in path:
        return True
    if {'mediaurl', 'thid', 'idpp'} & query_keys:
        return True
    return False


def _path_segments(value: str) -> List[str]:
    try:
        parsed = urlparse(normalize_text(value))
    except Exception:
        return []
    return [segment for segment in parsed.path.split("/") if segment]


def score_detail_like_url(url: str, reference_url: str = "", title: str = "") -> int:
    normalized_url = normalize_text(url)
    if not normalized_url:
        return -5
    if is_search_intermediary_url(normalized_url):
        return -8

    try:
        parsed = urlparse(normalized_url)
    except Exception:
        return -5

    reference = urlparse(normalize_text(reference_url)) if reference_url else None
    query_keys = {str(key or "").strip().lower() for key in parse_qs(parsed.query or "").keys() if str(key or "").strip()}
    path_segments = [segment.lower() for segment in _path_segments(normalized_url)]
    base_segments = [segment.lower() for segment in _path_segments(reference_url)] if reference_url else []
    score = 0

    if normalized_url.startswith(("http://", "https://")):
        score += 1

    if reference and parsed.netloc and parsed.netloc != reference.netloc:
        score += 3

    if query_keys & DETAIL_QUERY_KEYS:
        score += 3
    if query_keys & LIST_FILTER_QUERY_KEYS:
        score -= 4

    path_value = (parsed.path or "").rstrip("/")
    base_path = (reference.path or "").rstrip("/") if reference else ""
    if path_value:
        if reference and path_value == base_path:
            score -= 2
            if parsed.query and not (query_keys & DETAIL_QUERY_KEYS):
                score -= 3
        elif len(path_segments) > len(base_segments):
            score += 3
        else:
            score += 1
    elif parsed.query:
        score -= 3

    if any(segment in NAV_PATH_SEGMENTS for segment in path_segments[-2:]):
        score -= 3

    if len(path_segments) >= 2 and not any(segment in NAV_PATH_SEGMENTS for segment in path_segments[-2:]):
        score += 1

    title_text = normalize_text(title)
    if len(title_text) >= 40:
        score += 2
    elif len(title_text) >= 18:
        score += 1

    return score


def _decode_redirect_url(value: str) -> str:
    href = normalize_url_candidate(value)
    if not href:
        return ""
    try:
        parsed = urlparse(href)
    except Exception:
        return href

    qs = parse_qs(parsed.query or "")
    candidate = (
        (qs.get("uddg") or [None])[0]
        or (qs.get("u") or [None])[0]
        or (qs.get("url") or [None])[0]
        or (qs.get("target") or [None])[0]
        or (qs.get("redirect") or [None])[0]
    )
    if not candidate:
        return href

    candidate = unquote(str(candidate))
    if candidate.startswith("http://") or candidate.startswith("https://"):
        return candidate
    if candidate.startswith("a1"):
        raw = candidate[2:]
        padding = "=" * ((4 - len(raw) % 4) % 4)
        try:
            decoded = base64.urlsafe_b64decode((raw + padding).encode("ascii")).decode("utf-8", errors="ignore")
        except Exception:
            decoded = ""
        if decoded.startswith(("http://", "https://")):
            return decoded
        if decoded.startswith("/") and parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}{decoded}"
    return href


def best_url_from_item(item: Dict[str, Any]) -> str:
    candidates: List[str] = []
    for key, value in item.items():
        key_name = canonical_field_name(key)
        normalized = normalize_url_candidate(value) if looks_like_url(value) or canonical_field_name(key) == "url" or key.endswith("_url") else normalize_text(value)
        if not normalized:
            continue
        if key_name == "url" or key.endswith("_url") or looks_like_url(normalized):
            candidates.append(normalized)
    decoded_candidates = [_decode_redirect_url(candidate) for candidate in candidates]
    for candidate in decoded_candidates:
        if candidate.startswith(("http://", "https://")) and not is_search_intermediary_url(candidate):
            return candidate
    for candidate in decoded_candidates:
        if candidate.startswith(("http://", "https://")):
            return candidate
    return decoded_candidates[0] if decoded_candidates else ""


def best_title_from_item(item: Dict[str, Any]) -> str:
    for key in ("title", "headline", "name"):
        value = normalize_text(item.get(key))
        if value and not looks_like_url(value):
            return value
    link_value = normalize_text(item.get("link"))
    if link_value and not looks_like_url(link_value):
        return link_value
    for key, value in item.items():
        if key == "index":
            continue
        normalized = normalize_text(value)
        if normalized and not looks_like_url(normalized):
            return normalized
    return ""


def best_semantic_value(item: Dict[str, Any], canonical_field: str) -> str:
    candidates: List[str] = []
    for key, value in item.items():
        if key == "index":
            continue
        normalized = normalize_text(value)
        if not normalized:
            continue
        field_name = canonical_field_name(key)
        if field_name == canonical_field:
            if canonical_field == "url" and not looks_like_url(normalized):
                continue
            candidates.append(normalized)
    if not candidates:
        return ""
    if canonical_field == "summary":
        candidates.sort(key=len, reverse=True)
    return candidates[0]


def looks_like_detail_list_item(item: Dict[str, Any], reference_url: str = "") -> bool:
    if not isinstance(item, dict):
        return False
    title = best_title_from_item(item)
    url = best_url_from_item(item)
    if not title or len(title) < 4 or not url:
        return False
    return score_detail_like_url(url, reference_url=reference_url, title=title) >= 1


def is_noise_item(item: Dict[str, Any], requested_fields: List[str]) -> bool:
    title = normalize_text(item.get("title"))
    url = normalize_text(item.get("url", item.get("link", "")))
    summary = normalize_text(item.get("summary"))
    if not any(normalize_text(item.get(field)) for field in requested_fields if field != "url") and not url:
        return True

    title_lower = title.lower()
    if title_lower in STRICT_NOISE_TEXTS and not summary:
        return True
    # 通用特征：标题过短且无摘要，几乎都是 UI 碎片
    if len(title) <= 3 and not summary:
        return True

    if url:
        if is_search_intermediary_url(url):
            return True
        parsed = urlparse(url)
        url_lower = f"{parsed.path} {parsed.query}".lower()
        if any(token in url_lower for token in NOISE_URL_HINTS) and not summary:
            return True
        if parsed.path in {"", "/"} and title_lower in STRICT_NOISE_TEXTS:
            return True
    return False


def score_item(item: Dict[str, Any], requested_fields: List[str], task_description: str) -> int:
    score = 0
    task_tokens = set(tokenize_text(task_description))
    title = normalize_text(item.get("title"))
    summary = normalize_text(item.get("summary"))
    haystack = f"{title} {summary}".lower()
    if title:
        score += 3
    if normalize_text(item.get("url")):
        score += 2
    for field_name in requested_fields:
        if normalize_text(item.get(field_name)):
            score += 2
    if task_tokens:
        score += sum(1 for token in task_tokens if token in haystack)
    if is_noise_item(item, requested_fields):
        score -= 5
    return score


def canonicalize_item(item: Dict[str, Any], requested_fields: List[str]) -> Dict[str, Any]:
    normalized: Dict[str, Any] = {}
    if item.get("index") is not None:
        normalized["index"] = item.get("index")

    title = best_title_from_item(item)
    url = best_url_from_item(item)
    if title:
        normalized["title"] = title
    if url:
        normalized["url"] = url
        normalized["link"] = url

    for field_name in CONTENT_FIELDS:
        value = best_semantic_value(item, field_name)
        if value and value not in {title, url}:
            normalized[field_name] = value

    if not normalized.get("summary"):
        for fallback_key in ("description", "snippet", "content", "details", "text"):
            value = normalize_text(item.get(fallback_key))
            if value and value != normalized.get("title", ""):
                normalized["summary"] = value
                break

    for key, value in item.items():
        if key in normalized or key == "index":
            continue
        normalized_value = normalize_text(value)
        if not normalized_value:
            continue
        canonical_key = canonical_field_name(key)
        if canonical_key in {"title", "url"}:
            continue
        if normalized_value in {normalized.get("title", ""), normalized.get("url", ""), normalized.get("summary", "")}:
            continue
        if key.endswith("_url") and normalized.get("url"):
            continue
        normalized[key] = normalized_value

    if not normalized.get("link") and normalized.get("url"):
        normalized["link"] = normalized["url"]

    if "url" in requested_fields and normalized.get("url") and "link" not in normalized:
        normalized["link"] = normalized["url"]
    return normalized


_WEATHER_TASK_HINTS = re.compile(
    r"天气|气温|温度|湿度|风力|weather|forecast|temperature|humidity|wind",
    re.IGNORECASE,
)

_WEATHER_PATTERNS: List[Tuple[str, "re.Pattern[str]"]] = [
    ("weather", re.compile(r"(多云|晴|阴|小雨|中雨|大雨|暴雨|雷阵雨|雪|小雪|中雪|大雪|雾|霾|cloudy|sunny|rainy|overcast|rain|snow|fog)", re.IGNORECASE)),
    ("temperature", re.compile(r"(-?\d+(?:\.\d+)?/-?\d+(?:\.\d+)?°[CF])")),
    ("wind", re.compile(r"(\d+-\d+级)")),
    ("humidity", re.compile(r"(?:湿度|humidity)\s*(\d+%)", re.IGNORECASE)),
    ("aqi", re.compile(r"(?:AQI|空气质量指数)\s*(\d+)", re.IGNORECASE)),
]


def _extract_weather_fields(item: Dict[str, Any]) -> None:
    """Try to extract structured weather fields from text-based items."""
    text = item.get("text", "") or item.get("summary", "") or item.get("title", "")
    if not text:
        return
    for field_name, pattern in _WEATHER_PATTERNS:
        if field_name not in item:
            m = pattern.search(text)
            if m:
                item[field_name] = m.group(1)


def normalize_web_results(
    results: List[Dict[str, Any]],
    task_description: str,
    *,
    limit: int,
    understanding: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    requested_fields = infer_requested_fields(task_description, understanding or {})
    is_weather_task = bool(_WEATHER_TASK_HINTS.search(task_description or ""))
    cleaned: List[Tuple[int, Dict[str, Any]]] = []
    seen: Set[Tuple[str, str]] = set()

    for raw_item in results or []:
        if not isinstance(raw_item, dict):
            continue
        item = canonicalize_item(raw_item, requested_fields)
        if not item:
            continue
        if is_weather_task:
            _extract_weather_fields(item)
        if is_noise_item(item, requested_fields):
            continue
        key = (
            normalize_text(item.get("title", "")).lower()[:160],
            normalize_text(item.get("url", item.get("link", ""))).lower()[:240],
        )
        if key in seen:
            continue
        seen.add(key)
        cleaned.append((score_item(item, requested_fields, task_description), item))

    cleaned.sort(key=lambda entry: entry[0], reverse=True)

    # 通用相关性阈值：砍掉得分 <= 0 的条目（无标题、无URL、或被噪音惩罚的）
    # 如果砍完不足 2 条则降级保底，避免空结果
    above_threshold = [(score, item) for score, item in cleaned if score > 0]
    if len(above_threshold) >= 2:
        cleaned = above_threshold

    final_items = [item for _, item in cleaned[:limit]]
    for idx, item in enumerate(final_items, 1):
        item["index"] = idx
    return final_items


def normalize_search_cards(
    cards: List[Dict[str, Any]],
    task_description: str,
    *,
    limit: int,
    understanding: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    raw_results: List[Dict[str, Any]] = []
    scan_limit = min(len(cards), max(limit * 4, limit + 6, 12)) if cards else 0
    for idx, card in enumerate(cards[:scan_limit], 1):
        if not isinstance(card, dict):
            continue
        link = normalize_text(card.get("link"))
        title = normalize_text(card.get("title"))
        if not title or not link:
            continue
        raw_results.append(
            {
                "index": idx,
                "title": title,
                "url": link,
                "link": link,
                "source": normalize_text(card.get("source")),
                "date": normalize_text(card.get("date")),
                "summary": normalize_text(card.get("snippet")),
                "target_ref": normalize_text(card.get("target_ref")),
                "target_selector": normalize_text(card.get("target_selector")),
            }
        )
    return normalize_web_results(
        raw_results,
        task_description,
        limit=limit,
        understanding=understanding,
    )
