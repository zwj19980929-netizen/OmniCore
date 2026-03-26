"""
搜索引擎抽象层 - 提供多种搜索策略的统一接口
支持：API 搜索（优先）→ 原生搜索（备用）→ 直接 URL（降级）
"""
import os
import asyncio
from abc import ABC, abstractmethod
from typing import List, Dict, Optional, Any
from dataclasses import dataclass
from enum import Enum

import requests
from utils.logger import log_agent_action, log_error, log_warning, log_success


class SearchStrategy(Enum):
    """搜索策略"""
    API = "api"           # API 搜索（最可靠）
    NATIVE = "native"     # 原生页面搜索（备用）
    DIRECT = "direct"     # 直接 URL 访问（降级）


@dataclass
class SearchResult:
    """搜索结果"""
    title: str
    url: str
    snippet: str = ""
    rank: int = 0
    source: str = ""  # 来源：google, bing, github, etc.


@dataclass
class SearchResponse:
    """搜索响应"""
    success: bool
    results: List[SearchResult]
    strategy_used: SearchStrategy
    error: Optional[str] = None
    metadata: Dict[str, Any] = None


class SearchEngine(ABC):
    """搜索引擎抽象基类"""

    def __init__(self, name: str):
        self.name = name

    @abstractmethod
    async def search(self, query: str, max_results: int = 10) -> SearchResponse:
        """执行搜索"""
        pass


class SerpAPISearchEngine(SearchEngine):
    """SerpAPI 搜索引擎（推荐）"""

    def __init__(self):
        super().__init__("SerpAPI")
        self.api_key = os.getenv("SERPAPI_KEY")
        self.base_url = "https://serpapi.com/search"

    async def search(self, query: str, max_results: int = 10) -> SearchResponse:
        """使用 SerpAPI 搜索"""
        if not self.api_key:
            return SearchResponse(
                success=False,
                results=[],
                strategy_used=SearchStrategy.API,
                error="SERPAPI_KEY not configured"
            )

        try:
            log_agent_action("SerpAPI", f"搜索: {query[:50]}")

            params = {
                "q": query,
                "api_key": self.api_key,
                "num": max_results,
                "engine": "google",
            }

            # 使用 asyncio 运行同步请求
            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: requests.get(self.base_url, params=params, timeout=10)
            )

            if response.status_code != 200:
                return SearchResponse(
                    success=False,
                    results=[],
                    strategy_used=SearchStrategy.API,
                    error=f"API returned {response.status_code}"
                )

            data = response.json()
            organic_results = data.get("organic_results", [])

            results = []
            for i, item in enumerate(organic_results[:max_results], 1):
                results.append(SearchResult(
                    title=item.get("title", ""),
                    url=item.get("link", ""),
                    snippet=item.get("snippet", ""),
                    rank=i,
                    source="google"
                ))

            log_success(f"SerpAPI 搜索成功，找到 {len(results)} 个结果")

            return SearchResponse(
                success=True,
                results=results,
                strategy_used=SearchStrategy.API,
                metadata={"total": len(organic_results)}
            )

        except Exception as e:
            log_error(f"SerpAPI 搜索失败: {e}", exc_info=True)
            return SearchResponse(
                success=False,
                results=[],
                strategy_used=SearchStrategy.API,
                error=str(e)
            )


class GoogleCustomSearchEngine(SearchEngine):
    """Google Custom Search API"""

    def __init__(self):
        super().__init__("GoogleCustomSearch")
        self.api_key = os.getenv("GOOGLE_API_KEY")
        self.cx = os.getenv("GOOGLE_CX")  # Custom Search Engine ID
        self.base_url = "https://www.googleapis.com/customsearch/v1"

    async def search(self, query: str, max_results: int = 10) -> SearchResponse:
        """使用 Google Custom Search API"""
        if not self.api_key or not self.cx:
            return SearchResponse(
                success=False,
                results=[],
                strategy_used=SearchStrategy.API,
                error="GOOGLE_API_KEY or GOOGLE_CX not configured"
            )

        try:
            log_agent_action("GoogleCustomSearch", f"搜索: {query[:50]}")

            params = {
                "key": self.api_key,
                "cx": self.cx,
                "q": query,
                "num": min(max_results, 10),  # API 限制最多 10 个
            }

            loop = asyncio.get_event_loop()
            response = await loop.run_in_executor(
                None,
                lambda: requests.get(self.base_url, params=params, timeout=10)
            )

            if response.status_code != 200:
                return SearchResponse(
                    success=False,
                    results=[],
                    strategy_used=SearchStrategy.API,
                    error=f"API returned {response.status_code}"
                )

            data = response.json()
            items = data.get("items", [])

            results = []
            for i, item in enumerate(items, 1):
                results.append(SearchResult(
                    title=item.get("title", ""),
                    url=item.get("link", ""),
                    snippet=item.get("snippet", ""),
                    rank=i,
                    source="google"
                ))

            log_success(f"Google Custom Search 成功，找到 {len(results)} 个结果")

            return SearchResponse(
                success=True,
                results=results,
                strategy_used=SearchStrategy.API,
                metadata={"total": data.get("searchInformation", {}).get("totalResults", 0)}
            )

        except Exception as e:
            log_error(f"Google Custom Search 失败: {e}", exc_info=True)
            return SearchResponse(
                success=False,
                results=[],
                strategy_used=SearchStrategy.API,
                error=str(e)
            )


class DirectURLSearchEngine(SearchEngine):
    """直接 URL 访问策略（降级方案）"""

    def __init__(self):
        super().__init__("DirectURL")
        # 已知网站的搜索 URL 模板
        self.url_templates = {
            "github": "https://github.com/search?q={query}&type=repositories",
            "stackoverflow": "https://stackoverflow.com/search?q={query}",
            "wikipedia": "https://en.wikipedia.org/w/index.php?search={query}",
            "pypi": "https://pypi.org/search/?q={query}",
            "npm": "https://www.npmjs.com/search?q={query}",
        }

    async def search(self, query: str, max_results: int = 10) -> SearchResponse:
        """根据查询内容推断目标网站，直接访问"""
        log_agent_action("DirectURL", f"分析查询: {query[:50]}")

        # 推断目标网站
        target_site = self._infer_target_site(query)

        if target_site and target_site in self.url_templates:
            url = self.url_templates[target_site].format(query=query)
            log_success(f"推断目标网站: {target_site}, URL: {url}")

            return SearchResponse(
                success=True,
                results=[SearchResult(
                    title=f"Search on {target_site}",
                    url=url,
                    snippet=f"Direct search URL for {target_site}",
                    rank=1,
                    source=target_site
                )],
                strategy_used=SearchStrategy.DIRECT,
                metadata={"target_site": target_site}
            )

        # 无法推断时应明确失败，而不是伪装成 Google 搜索成功。
        # 否则上层会把“无法推断站点”误当成可访问结果页继续导航。
        log_warning("无法推断目标网站，DirectURL 降级失败")
        return SearchResponse(
            success=False,
            results=[],
            strategy_used=SearchStrategy.DIRECT,
            error="Could not infer target site for direct URL fallback",
            metadata={"fallback": True}
        )

    def _infer_target_site(self, query: str) -> Optional[str]:
        """根据查询内容推断目标网站（仅匹配用户明确提及的站名）"""
        query_lower = query.lower()
        for site in self.url_templates:
            if site in query_lower:
                return site
        return None


class SearchEngineManager:
    """搜索引擎管理器 - 多策略降级"""

    def __init__(self):
        self.engines = {
            SearchStrategy.API: [
                SerpAPISearchEngine(),
                GoogleCustomSearchEngine(),
            ],
            SearchStrategy.DIRECT: [
                DirectURLSearchEngine(),
            ]
        }

    async def search(
        self,
        query: str,
        max_results: int = 10,
        strategies: List[SearchStrategy] = None
    ) -> SearchResponse:
        """
        执行搜索，按策略优先级降级

        Args:
            query: 搜索查询
            max_results: 最大结果数
            strategies: 策略列表，默认 [API, DIRECT]

        Returns:
            SearchResponse
        """
        if strategies is None:
            strategies = [SearchStrategy.API, SearchStrategy.DIRECT]

        log_agent_action("SearchEngineManager", f"开始搜索: {query[:50]}")

        for strategy in strategies:
            engines = self.engines.get(strategy, [])

            for engine in engines:
                log_agent_action("SearchEngineManager", f"尝试 {engine.name}")

                response = await engine.search(query, max_results)

                if response.success and response.results:
                    log_success(
                        f"搜索成功: {engine.name}, "
                        f"策略: {strategy.value}, "
                        f"结果数: {len(response.results)}"
                    )
                    return response

                log_warning(f"{engine.name} 失败: {response.error}")

        # 所有策略都失败
        log_error("所有搜索策略都失败")
        return SearchResponse(
            success=False,
            results=[],
            strategy_used=SearchStrategy.API,
            error="All search strategies failed"
        )
