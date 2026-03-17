"""
领域关键词和域名配置
集中管理所有领域特定的关键词、域名等配置，避免硬编码
"""
from typing import Dict, List, Tuple

# 天气查询相关关键词
WEATHER_KEYWORDS = {
    "en": [
        "weather", "forecast", "temperature", "humidity", "air quality",
        "aqi", "wind", "precipitation", "rain", "snow", "storm"
    ],
    "zh": [
        "天气", "天气预报", "气温", "湿度", "空气质量", "风力",
        "降水", "雨", "雪", "风暴", "预报"
    ]
}

# 天气状况词汇
WEATHER_CONDITIONS = {
    "zh": [
        "晴", "多云", "阴", "小雨", "中雨", "大雨", "暴雨", "雷阵雨",
        "阵雨", "雨夹雪", "小雪", "中雪", "大雪", "雾", "霾", "扬沙", "浮尘"
    ],
    "en": [
        "sunny", "cloudy", "overcast", "rain", "shower", "thunderstorm",
        "snow", "fog", "haze", "clear", "partly cloudy"
    ]
}

# 天气相关域名（中国常用）
WEATHER_DOMAINS = [
    "weather.com.cn",
    "moji.com",
    "tianqi.com",
    "weather.com",
    "accuweather.com"
]

# 搜索引擎列表
SEARCH_ENGINES = [
    "google", "bing", "duckduckgo", "baidu", "yandex", "yahoo", "sogou", "360"
]

# 新闻相关关键词
NEWS_KEYWORDS = {
    "en": [
        "news", "headline", "article", "story", "report", "breaking",
        "latest", "trending", "top stories"
    ],
    "zh": [
        "新闻", "头条", "报道", "文章", "最新", "热点", "突发"
    ]
}

# 通用搜索停用词（用于优化搜索查询）
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


def get_all_keywords(category: str) -> List[str]:
    """
    获取某个类别的所有关键词（合并所有语言）

    Args:
        category: 类别名称，如 "weather", "news"

    Returns:
        关键词列表
    """
    category_map = {
        "weather": WEATHER_KEYWORDS,
        "news": NEWS_KEYWORDS,
        "stopwords": SEARCH_STOPWORDS
    }

    if category not in category_map:
        return []

    keywords_dict = category_map[category]
    all_keywords = []
    for lang_keywords in keywords_dict.values():
        all_keywords.extend(lang_keywords)

    return all_keywords


def get_keywords_tuple(category: str) -> Tuple[str, ...]:
    """
    获取某个类别的所有关键词（返回元组，用于兼容旧代码）

    Args:
        category: 类别名称

    Returns:
        关键词元组
    """
    return tuple(get_all_keywords(category))


def is_weather_query(text: str) -> bool:
    """
    判断文本是否为天气查询

    Args:
        text: 待判断的文本

    Returns:
        是否为天气查询
    """
    text_lower = text.lower()
    weather_keywords = get_all_keywords("weather")
    return any(keyword in text_lower for keyword in weather_keywords)


def is_news_query(text: str) -> bool:
    """
    判断文本是否为新闻查询

    Args:
        text: 待判断的文本

    Returns:
        是否为新闻查询
    """
    text_lower = text.lower()
    news_keywords = get_all_keywords("news")
    return any(keyword in text_lower for keyword in news_keywords)


def get_domain_hints(category: str) -> List[str]:
    """
    获取某个类别的推荐域名

    Args:
        category: 类别名称，如 "weather"

    Returns:
        域名列表
    """
    domain_map = {
        "weather": WEATHER_DOMAINS
    }

    return domain_map.get(category, [])


# 为了兼容旧代码，导出元组版本
WEATHER_KEYWORDS_TUPLE = get_keywords_tuple("weather")
NEWS_KEYWORDS_TUPLE = get_keywords_tuple("news")
SEARCH_STOPWORDS_SET = set(get_all_keywords("stopwords"))
WEATHER_CONDITIONS_TUPLE = tuple(WEATHER_CONDITIONS["zh"] + WEATHER_CONDITIONS["en"])
WEATHER_DOMAINS_TUPLE = tuple(WEATHER_DOMAINS)
