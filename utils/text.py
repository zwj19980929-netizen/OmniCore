"""
文本清洗工具
统一处理非法代理字符，避免记忆检索和 JSON 序列化因脏输入崩溃。
"""
from typing import Any


_REPLACEMENT_CHAR = "\uFFFD"


def sanitize_text(value: str) -> str:
    """
    替换字符串中的非法代理字符。

    Python 的 str 允许包含孤立的代理项，但很多下游库在 UTF-8 编码时会直接报错。
    这里统一替换成 U+FFFD，保留文本长度和大致位置，便于排障。
    """
    if not isinstance(value, str) or not value:
        return value

    if not any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        return value

    return "".join(
        _REPLACEMENT_CHAR if 0xD800 <= ord(char) <= 0xDFFF else char
        for char in value
    )


def sanitize_value(value: Any) -> Any:
    """
    递归清洗嵌套结构中的字符串。
    """
    if isinstance(value, str):
        return sanitize_text(value)

    if isinstance(value, list):
        return [sanitize_value(item) for item in value]

    if isinstance(value, tuple):
        return tuple(sanitize_value(item) for item in value)

    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            clean_key = sanitize_text(key) if isinstance(key, str) else key
            cleaned[clean_key] = sanitize_value(item)
        return cleaned

    return value
