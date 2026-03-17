"""
领域知识库 - 为特定任务提供领域专家知识
这些知识会动态注入到 Agent 的 prompt 中，而不是硬编码在代码逻辑里
"""
from typing import Dict, List, Any, Optional


class DomainKnowledge:
    """领域知识基类"""

    def __init__(self, domain: str):
        self.domain = domain

    def get_navigation_hints(self, task_description: str) -> str:
        """获取导航提示"""
        return ""

    def get_extraction_hints(self, task_description: str) -> str:
        """获取数据提取提示"""
        return ""

    def get_common_issues(self) -> str:
        """获取常见问题和解决方案"""
        return ""


class CNNVDKnowledge(DomainKnowledge):
    """CNNVD 漏洞库领域知识"""

    def __init__(self):
        super().__init__("cnnvd")
        self.official_domain = "www.cnnvd.org.cn"
        self.common_paths = [
            "/home/loophole",  # 漏洞列表
            "/web/vulnerability/querylist.tag",  # 查询列表
        ]

    def get_navigation_hints(self, task_description: str) -> str:
        return f"""
## CNNVD 网站导航提示

**官方域名**: {self.official_domain}

**常见路径**:
- 漏洞列表页: /home/loophole
- 查询页面: /web/vulnerability/querylist.tag

**导航策略**:
1. 如果在首页，寻找"漏洞库"、"安全公告"、"最新漏洞"等入口
2. 漏洞列表通常在表格中，每行包含：编号、名称、危害等级、发布时间
3. 可能需要点击"更多"或翻页按钮查看完整列表

**常见问题**:
- 首页可能没有直接显示漏洞列表，需要点击进入子页面
- 数据可能是动态加载的，需要等待加载完成
- 可能有弹窗或公告需要关闭
"""

    def get_extraction_hints(self, task_description: str) -> str:
        return """
## CNNVD 数据提取提示

**目标数据字段**:
- 漏洞编号 (CNNVD-XXXX-XXXXX)
- 漏洞名称
- 危害等级 (高危/中危/低危)
- 发布时间
- CVE 编号 (如果有)

**提取策略**:
1. 优先查找表格结构 (table, tbody, tr, td)
2. 每个漏洞通常是一行 (tr)
3. 如果没有表格，查找列表结构 (ul, li)
4. 注意区分导航链接和数据链接

**选择器建议**:
- 表格行: `table.list tr` 或 `tbody tr`
- 列表项: `ul.vulnerability-list li` 或 `.vuln-item`
"""

    def get_common_issues(self) -> str:
        return """
## CNNVD 常见问题

1. **重复点击同一元素**: 如果点击后页面没有变化，说明可能：
   - 点击的是当前页标签
   - 需要等待动态加载
   - 应该尝试其他导航方式

2. **找不到漏洞列表**:
   - 不要在首页反复搜索，首页通常只有入口
   - 点击"漏洞库"或"安全公告"进入子页面
   - 使用 URL 判断是否到达正确页面

3. **数据提取失败**:
   - 确认已经到达漏洞列表页面 (URL 包含 loophole 或 vulnerability)
   - 等待页面完全加载
   - 使用更宽松的选择器
"""


class HackerNewsKnowledge(DomainKnowledge):
    """Hacker News 领域知识"""

    def __init__(self):
        super().__init__("hackernews")
        self.official_domain = "news.ycombinator.com"

    def get_navigation_hints(self, task_description: str) -> str:
        return f"""
## Hacker News 导航提示

**官方域名**: {self.official_domain}

**页面结构**:
- 首页直接显示新闻列表，无需额外导航
- 每条新闻包含：标题、链接、评分、评论数

**导航策略**:
- 首页即是列表页，无需点击
- 翻页链接在底部 (More)
"""

    def get_extraction_hints(self, task_description: str) -> str:
        return """
## Hacker News 数据提取提示

**目标数据字段**:
- 标题 (title)
- 链接 (url)
- 评分 (points)
- 评论数 (comments)

**提取策略**:
1. 每条新闻是一个 `<tr class="athing">`
2. 标题在 `<span class="titleline">` 内的 `<a>` 标签
3. 评分和评论在下一行的 `<tr>`

**选择器建议**:
- 新闻行: `tr.athing`
- 标题: `.titleline a`
- 评分: `.score`
"""


class WeatherKnowledge(DomainKnowledge):
    """天气查询领域知识"""

    def __init__(self):
        super().__init__("weather")
        self.recommended_sites = [
            "weather.com.cn",  # 中国天气网
            "tianqi.2345.com",  # 2345天气
            "moji.com",  # 墨迹天气
        ]

    def get_navigation_hints(self, task_description: str) -> str:
        return f"""
## 天气网站导航提示

**推荐网站**: {', '.join(self.recommended_sites)}

**导航策略**:
1. 天气网站首页通常直接显示天气信息
2. 可能需要输入城市名称或选择城市
3. 数据通常在页面顶部或中心位置

**常见问题**:
- 可能需要关闭广告弹窗
- 数据可能是动态加载的
- 注意区分当前天气和预报天气
"""

    def get_extraction_hints(self, task_description: str) -> str:
        return """
## 天气数据提取提示

**目标数据字段**:
- 温度 (temperature)
- 天气状况 (condition: 晴/多云/雨等)
- 湿度 (humidity)
- 风力 (wind)
- 空气质量 (AQI)

**提取策略**:
1. 查找包含温度数字的元素 (如 "15°C")
2. 查找天气图标或文字描述
3. 湿度、风力通常在详细信息区域

**关键词识别**:
- 温度: 包含 "°C", "℃", "度"
- 湿度: 包含 "湿度", "humidity", "%"
- 风力: 包含 "风", "wind", "级"
- 空气质量: 包含 "AQI", "空气质量", "良", "优"
"""


class DomainKnowledgeRegistry:
    """领域知识注册表"""

    def __init__(self):
        self.knowledge_base: Dict[str, DomainKnowledge] = {
            "cnnvd": CNNVDKnowledge(),
            "hackernews": HackerNewsKnowledge(),
            "weather": WeatherKnowledge(),
        }

    def detect_domain(self, task_description: str, url: str = "") -> Optional[str]:
        """检测任务所属领域"""
        text = f"{task_description} {url}".lower()

        # CNNVD
        if any(keyword in text for keyword in ["cnnvd", "漏洞", "vulnerability", "cnnvd.org.cn"]):
            return "cnnvd"

        # Hacker News
        if any(keyword in text for keyword in ["hacker news", "hackernews", "news.ycombinator"]):
            return "hackernews"

        # Weather
        if any(keyword in text for keyword in ["天气", "weather", "forecast", "气温", "temperature"]):
            return "weather"

        return None

    def get_knowledge(self, domain: str) -> Optional[DomainKnowledge]:
        """获取领域知识"""
        return self.knowledge_base.get(domain)

    def get_hints_for_task(self, task_description: str, url: str = "", hint_type: str = "all") -> str:
        """为任务获取领域提示"""
        domain = self.detect_domain(task_description, url)
        if not domain:
            return ""

        knowledge = self.get_knowledge(domain)
        if not knowledge:
            return ""

        hints = []

        if hint_type in ["all", "navigation"]:
            nav_hints = knowledge.get_navigation_hints(task_description)
            if nav_hints:
                hints.append(nav_hints)

        if hint_type in ["all", "extraction"]:
            ext_hints = knowledge.get_extraction_hints(task_description)
            if ext_hints:
                hints.append(ext_hints)

        if hint_type in ["all", "issues"]:
            issue_hints = knowledge.get_common_issues()
            if issue_hints:
                hints.append(issue_hints)

        if hints:
            return "\n\n".join([
                "=" * 80,
                "🎓 **领域专家知识** (Domain Expert Knowledge)",
                "=" * 80,
                *hints,
                "=" * 80,
            ])

        return ""


# 全局实例
domain_knowledge_registry = DomainKnowledgeRegistry()


# 便捷函数
def get_domain_hints(task_description: str, url: str = "", hint_type: str = "all") -> str:
    """获取领域提示的便捷函数"""
    return domain_knowledge_registry.get_hints_for_task(task_description, url, hint_type)
