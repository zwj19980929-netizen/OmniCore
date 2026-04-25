import re
from urllib.parse import urlparse

_URL_PATTERN = re.compile(
    r"(?:https?|file)://[A-Za-z0-9:/?#\[\]@!$&()*+,;=._~%'-]+",
    re.IGNORECASE,
)
_URL_TRAILING_PUNCT = ".,);]}>\"'，。！？；：、）］】｝〉》」』”’"


def sanitize_extracted_url(value: str) -> str:
    match = _URL_PATTERN.search(str(value or ""))
    if not match:
        return ""

    candidate = match.group(0).rstrip(_URL_TRAILING_PUNCT)
    parsed = urlparse(candidate)
    scheme = parsed.scheme.lower()
    if scheme in {"http", "https"}:
        if not parsed.netloc:
            return ""
        return candidate
    if scheme == "file":
        if not parsed.path:
            return ""
        return candidate
    return ""


def extract_first_url(text: str) -> str:
    return sanitize_extracted_url(text)


def extract_all_urls(text: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in _URL_PATTERN.finditer(str(text or "")):
        candidate = sanitize_extracted_url(match.group(0))
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        urls.append(candidate)
    return urls
