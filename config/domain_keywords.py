"""
通用搜索停用词配置
"""

SEARCH_STOPWORDS = {
    "en": [
        "a", "an", "and", "are", "as", "at", "be", "by", "current", "detail",
        "details", "extract", "find", "for", "from", "get", "give", "how",
        "in", "into", "latest", "most", "news", "of", "on", "or", "recent",
        "report", "reports", "search", "source", "sources", "statement",
        "statements", "that", "the", "their", "this", "to", "using", "verify",
        "with", "directly", "primary", "secondary", "preferred"
    ],
    "zh": [
        "一下", "一些", "使用", "信息", "内容", "分析", "声明", "报道",
        "搜索", "提取", "搜集", "最新", "最近", "材料", "核实", "来源",
        "请", "请你", "请帮我", "资料", "通过"
    ]
}

SEARCH_STOPWORDS_SET: set = set(
    word for words in SEARCH_STOPWORDS.values() for word in words
)
