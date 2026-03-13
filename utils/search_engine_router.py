"""
智能搜索引擎路由器
根据目标网站地域和任务内容，选择最合适的搜索引擎，避免跨境搜索触发风控
"""
import re
from typing import List, Optional
from urllib.parse import urlparse


class SearchEngineRouter:
    """搜索引擎智能路由"""

    # 国内域名后缀
    CN_DOMAINS = {".cn", ".com.cn", ".net.cn", ".org.cn", ".gov.cn", ".edu.cn"}

    # 国内常见网站域名
    CN_SITES = {
        "baidu.com", "taobao.com", "tmall.com", "jd.com", "qq.com",
        "weibo.com", "zhihu.com", "bilibili.com", "douban.com",
        "163.com", "sina.com.cn", "sohu.com", "ifeng.com",
        "csdn.net", "cnblogs.com", "oschina.net", "gitee.com",
        "aliyun.com", "tencent.com", "huawei.com", "xiaomi.com",
    }

    # 中文字符正则
    RE_CHINESE = re.compile(r'[\u4e00-\u9fff]')

    @classmethod
    def is_chinese_content(cls, text: str) -> bool:
        """判断文本是否包含中文"""
        if not text:
            return False
        chinese_chars = cls.RE_CHINESE.findall(text)
        return len(chinese_chars) > len(text) * 0.2  # 中文字符占比超过20%

    @classmethod
    def is_domestic_domain(cls, domain: str) -> bool:
        """判断是否为国内域名"""
        if not domain:
            return False

        domain_lower = domain.lower()

        # 检查是否以国内域名后缀结尾
        if any(domain_lower.endswith(suffix) for suffix in cls.CN_DOMAINS):
            return True

        # 检查是否为已知国内网站
        for cn_site in cls.CN_SITES:
            if domain_lower == cn_site or domain_lower.endswith(f".{cn_site}"):
                return True

        return False

    @classmethod
    def detect_target_region(cls, task_description: str, url_hint: str = "") -> str:
        """
        检测目标区域
        返回: "domestic" (国内) 或 "international" (国际)
        """
        # 1. 如果有明确的 URL 提示，优先根据域名判断
        if url_hint:
            try:
                parsed = urlparse(url_hint)
                domain = parsed.netloc or ""
                if domain and cls.is_domestic_domain(domain):
                    return "domestic"
                if domain and not cls.is_domestic_domain(domain):
                    return "international"
            except Exception:
                pass

        # 2. 从任务描述中提取域名提示
        domain_pattern = r'\b(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}\b'
        domains = re.findall(domain_pattern, task_description)
        for domain in domains:
            if cls.is_domestic_domain(domain):
                return "domestic"

        # 3. 根据任务描述的语言判断
        if cls.is_chinese_content(task_description):
            return "domestic"

        # 4. 默认国际
        return "international"

    @classmethod
    def get_search_engines(
        cls,
        task_description: str,
        url_hint: str = "",
        prefer_domestic: Optional[bool] = None
    ) -> List[str]:
        """
        获取推荐的搜索引擎列表（按优先级排序）

        Args:
            task_description: 任务描述
            url_hint: URL 提示（如果有）
            prefer_domestic: 强制偏好（True=国内, False=国际, None=自动检测）

        Returns:
            搜索引擎 URL 模板列表，{query} 为占位符
        """
        # 检测目标区域
        if prefer_domestic is None:
            region = cls.detect_target_region(task_description, url_hint)
            is_domestic = (region == "domestic")
        else:
            is_domestic = prefer_domestic

        if is_domestic:
            # 国内任务：优先百度，备选搜狗、360
            return [
                "https://www.baidu.com/s?wd={query}",
                "https://www.sogou.com/web?query={query}",
                "https://www.so.com/s?q={query}",
                "https://www.bing.com/search?q={query}",  # 备选国际引擎
            ]
        else:
            # 国际任务：优先 Google，备选 Bing、DuckDuckGo
            return [
                "https://www.google.com/search?q={query}&hl=en",
                "https://www.bing.com/search?q={query}",
                "https://duckduckgo.com/html/?q={query}",
            ]

    @classmethod
    def format_search_url(cls, template: str, query: str) -> str:
        """格式化搜索 URL"""
        from urllib.parse import quote_plus
        return template.replace("{query}", quote_plus(query))
