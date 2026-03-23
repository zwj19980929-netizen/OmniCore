"""
文本语义相关度提取模块
混合评分：Embedding 语义相似度 + 关键词匹配，从大段网页文本中提取与用户任务最相关的部分。

评分策略：
1. 混合评分 = α × embedding_sim + (1-α) × keyword_score，解决纯向量区分度不够的问题
2. 动态 chunk_size：根据 page_type 自适应（list 页更细粒度）
3. 关键词提取：中文 n-gram + 英文 token + 数字，纯通用，无领域硬编码
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
import threading
from typing import Dict, List, Optional, Set, Tuple

import requests

from config.settings import settings
from utils.logger import logger

# 可选 numpy 加速
try:
    import numpy as np
    _HAS_NUMPY = True
except ImportError:
    _HAS_NUMPY = False


# ── Embedding 缓存 ──────────────────────────────────────────────────────

_cache_lock = threading.Lock()
_embedding_cache: dict[str, tuple[float, list[list[float]]]] = {}
_CACHE_TTL = 60.0
_CACHE_MAX_SIZE = 128


def _cache_key(texts: list[str]) -> str:
    raw = "\n".join(texts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> Optional[list[list[float]]]:
    with _cache_lock:
        entry = _embedding_cache.get(key)
        if entry is None:
            return None
        ts, vecs = entry
        if time.time() - ts > _CACHE_TTL:
            del _embedding_cache[key]
            return None
        return vecs


def _cache_set(key: str, vecs: list[list[float]]) -> None:
    with _cache_lock:
        if len(_embedding_cache) >= _CACHE_MAX_SIZE:
            oldest_key = min(_embedding_cache, key=lambda k: _embedding_cache[k][0])
            del _embedding_cache[oldest_key]
        _embedding_cache[key] = (time.time(), vecs)


# ── 关键词提取 ────────────────────────────────────────────────────────────

# CJK 字符检测
_CJK_RE = re.compile(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff]')
# 中文分词（简易：按连续中文字符提取 2~6 gram）
_ZH_WORD_RE = re.compile(r'[\u4e00-\u9fff]{2,6}')
_EN_WORD_RE = re.compile(r'[a-zA-Z][a-zA-Z0-9]*(?:\s+[a-zA-Z][a-zA-Z0-9]*)?', re.ASCII)
_NUM_RE = re.compile(r'\d+(?:\.\d+)?')


def _extract_query_keywords(query: str) -> Set[str]:
    """从 query 中提取关键词集合（中文词 + 英文词 + 数字）。"""
    keywords: Set[str] = set()

    # 提取英文词/短语
    for m in _EN_WORD_RE.finditer(query):
        word = m.group().strip().lower()
        if len(word) >= 2:
            keywords.add(word)

    # 提取中文词
    for m in _ZH_WORD_RE.finditer(query):
        keywords.add(m.group())

    # 提取数字
    for m in _NUM_RE.finditer(query):
        keywords.add(m.group())

    return keywords


def _compute_keyword_score(chunk: str, keywords: Set[str]) -> float:
    """计算 chunk 的关键词匹配得分 (0~1)。"""
    if not keywords:
        return 0.0

    chunk_lower = chunk.lower()
    matched = 0
    total = len(keywords)

    for kw in keywords:
        if kw.lower() in chunk_lower:
            matched += 1

    return matched / total if total > 0 else 0.0


# ── 文本分块 ─────────────────────────────────────────────────────────────

_SPLIT_RE = re.compile(r'(?:\r?\n){2,}|(?<=[。！？.!?\n])\s+')


def _chunk_text(
    text: str,
    chunk_size: int = None,
    overlap: int = None,
) -> List[str]:
    chunk_size = chunk_size or settings.RELEVANCE_CHUNK_SIZE
    overlap = overlap or settings.RELEVANCE_CHUNK_OVERLAP

    # 防止 overlap >= chunk_size 导致死循环
    if overlap >= chunk_size:
        overlap = chunk_size // 4

    paragraphs = _SPLIT_RE.split(text.strip())
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    chunks: List[str] = []
    current = ""

    for para in paragraphs:
        if current and len(current) + len(para) + 1 <= chunk_size:
            current += "\n" + para
        else:
            if current:
                chunks.append(current)
            if len(para) > chunk_size:
                start = 0
                while start < len(para):
                    end = start + chunk_size
                    chunks.append(para[start:end])
                    start += chunk_size - overlap
                current = ""
            else:
                current = para

    if current:
        chunks.append(current)

    return chunks


def _get_chunk_size_for_page_type(page_type: str) -> int:
    """根据页面类型动态选择 chunk_size。列表/搜索页用更细粒度。"""
    base = settings.RELEVANCE_CHUNK_SIZE
    if page_type in ("list", "serp"):
        return max(64, base // 2)  # 列表页减半（默认 128）
    return base


# ── 智谱 Embedding API ──────────────────────────────────────────────────

_ZHIPU_API_URL = "https://open.bigmodel.cn/api/paas/v4/embeddings"
_ZHIPU_BATCH_LIMIT = 16
_MAX_RETRIES = 2
_RETRY_BACKOFF = 1.0


def _call_zhipu_embedding_sync(texts: List[str], api_key: str = None) -> List[List[float]]:
    """同步调用智谱 Embedding API，带重试。"""
    api_key = api_key or settings.ZHIPU_API_KEY
    if not api_key or api_key == "your-zhipu-api-key":
        raise ValueError("ZHIPU_API_KEY 未配置，无法使用语义匹配功能")

    model = settings.ZHIPU_EMBEDDING_MODEL
    all_embeddings: List[List[float]] = [[] for _ in range(len(texts))]

    for batch_start in range(0, len(texts), _ZHIPU_BATCH_LIMIT):
        batch = texts[batch_start: batch_start + _ZHIPU_BATCH_LIMIT]
        last_error = None

        for attempt in range(_MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    _ZHIPU_API_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={"model": model, "input": batch},
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()

                for item in data.get("data", []):
                    idx = item.get("index", 0)
                    all_embeddings[batch_start + idx] = item.get("embedding", [])
                last_error = None
                break
            except requests.RequestException as e:
                last_error = e
                if attempt < _MAX_RETRIES:
                    wait = _RETRY_BACKOFF * (2 ** attempt)
                    logger.warning(f"智谱 Embedding 第{attempt + 1}次重试 ({wait:.1f}s): {e}")
                    time.sleep(wait)

        if last_error is not None:
            logger.error(f"智谱 Embedding API 调用失败 (已重试{_MAX_RETRIES}次): {last_error}")
            raise last_error

    return all_embeddings


async def _call_zhipu_embedding_async(texts: List[str], api_key: str = None) -> List[List[float]]:
    """异步包装：在线程中执行同步调用，避免阻塞事件循环。"""
    return await asyncio.to_thread(_call_zhipu_embedding_sync, texts, api_key)


# ── 向量相似度 ───────────────────────────────────────────────────────────

def _cosine_similarity(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    if _HAS_NUMPY:
        va, vb = np.array(a), np.array(b)
        norm_a, norm_b = np.linalg.norm(va), np.linalg.norm(vb)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(va, vb) / (norm_a * norm_b))
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ── 相关度分数缓存（供外部同步读取） ────────────────────────────────────

_score_lock = threading.Lock()
_relevance_scores: dict[str, tuple[float, float]] = {}  # text_hash → (timestamp, max_score)
_SCORE_TTL = 120.0
_SCORE_MAX_SIZE = 64


def _text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _store_relevance_score(full_text: str, max_score: float) -> None:
    key = _text_hash(full_text)
    with _score_lock:
        if len(_relevance_scores) >= _SCORE_MAX_SIZE:
            oldest = min(_relevance_scores, key=lambda k: _relevance_scores[k][0])
            del _relevance_scores[oldest]
        _relevance_scores[key] = (time.time(), max_score)


def get_relevance_score(full_text: str) -> Optional[float]:
    """
    获取最近一次对该文本计算的最高相关度分数。
    由 extract_relevant_text* 系列函数自动写入，外部可同步读取。
    无缓存或已过期返回 None。
    """
    key = _text_hash(full_text)
    with _score_lock:
        entry = _relevance_scores.get(key)
        if entry is None:
            return None
        ts, score = entry
        if time.time() - ts > _SCORE_TTL:
            del _relevance_scores[key]
            return None
        return score


# ── 混合评分提取逻辑 ─────────────────────────────────────────────────────

# 混合权重：embedding_sim * α + keyword_score * (1-α)
_HYBRID_ALPHA = 0.65


def _extract_with_embeddings(
    full_text: str,
    query: str,
    chunks: List[str],
    embeddings: List[List[float]],
    top_k: int,
    max_chars: Optional[int],
    keywords: Optional[Set[str]] = None,
) -> str:
    query_vec = embeddings[0]
    chunk_vecs = embeddings[1:]

    scored: List[Tuple[int, float, float, float]] = []  # (index, hybrid_score, emb_sim, kw_score)
    for i, cvec in enumerate(chunk_vecs):
        emb_sim = _cosine_similarity(query_vec, cvec)

        kw_score = 0.0
        if keywords:
            kw_score = _compute_keyword_score(chunks[i], keywords)

        # 混合得分
        if keywords:
            hybrid = _HYBRID_ALPHA * emb_sim + (1 - _HYBRID_ALPHA) * kw_score
        else:
            hybrid = emb_sim

        scored.append((i, hybrid, emb_sim, kw_score))

    scored.sort(key=lambda x: x[1], reverse=True)

    actual_k = min(top_k, len(scored))
    top_indices = sorted([idx for idx, _, _, _ in scored[:actual_k]])
    selected = [chunks[i] for i in top_indices]

    result = "\n\n".join(selected)
    if max_chars and len(result) > max_chars:
        result = result[:max_chars]

    if actual_k > 0:
        top_entry = scored[0]
        bottom_entry = scored[actual_k - 1]
        # 缓存最高相关度分数，供 goal_satisfied 等外部逻辑同步读取
        _store_relevance_score(full_text, top_entry[1])
        detail = (
            f"语义匹配: {len(full_text)} 字 → {len(result)} 字 "
            f"(top-{actual_k}/{len(chunks)} 块, "
            f"混合分 {bottom_entry[1]:.3f}~{top_entry[1]:.3f}, "
            f"emb {bottom_entry[2]:.3f}~{top_entry[2]:.3f}"
        )
        if keywords:
            detail += f", kw {bottom_entry[3]:.3f}~{top_entry[3]:.3f}"
        detail += ")"
        logger.info(detail)

    return result


def _prepare_and_check(
    full_text: str,
    query: str,
    top_k: int = None,
    max_chars: int = None,
    page_type: str = "",
) -> Tuple[Optional[str], List[str], int]:
    """
    共用的前置检查。
    返回 (early_result, chunks, top_k)。
    如果 early_result 不为 None，说明无需做 embedding 直接返回。
    """
    top_k = top_k or settings.RELEVANCE_TOP_K
    min_len = settings.RELEVANCE_MIN_TEXT_LENGTH

    if not full_text or len(full_text) <= min_len:
        return (full_text or ""), [], top_k

    if not query:
        return (full_text[:max_chars] if max_chars else full_text), [], top_k

    # 根据 page_type 动态选择 chunk_size
    chunk_size = _get_chunk_size_for_page_type(page_type)
    overlap = min(settings.RELEVANCE_CHUNK_OVERLAP, chunk_size - 1)

    chunks = _chunk_text(full_text, chunk_size=chunk_size, overlap=overlap)
    if not chunks:
        return (full_text[:max_chars] if max_chars else full_text), [], top_k

    if len(chunks) <= top_k:
        result = "\n".join(chunks)
        return (result[:max_chars] if max_chars else result), [], top_k

    return None, chunks, top_k


# ── 同步入口 ─────────────────────────────────────────────────────────────

def extract_relevant_text(
    full_text: str,
    query: str,
    top_k: int = None,
    max_chars: int = None,
    page_type: str = "",
) -> str:
    early, chunks, top_k = _prepare_and_check(full_text, query, top_k, max_chars, page_type)
    if early is not None:
        return early

    keywords = _extract_query_keywords(query)

    all_texts = [query] + chunks
    ck = _cache_key(all_texts)
    cached = _cache_get(ck)

    if cached is not None:
        logger.info(f"Embedding 缓存命中 ({len(chunks)} 块)")
        return _extract_with_embeddings(
            full_text, query, chunks, cached, top_k, max_chars,
            keywords=keywords,
        )

    t0 = time.time()
    try:
        embeddings = _call_zhipu_embedding_sync(all_texts)
    except Exception as e:
        logger.warning(f"语义匹配失败，降级为截断: {e}")
        min_len = settings.RELEVANCE_MIN_TEXT_LENGTH
        return full_text[:max_chars] if max_chars else full_text[:min_len * 2]

    logger.info(f"Embedding 完成: {len(chunks)} 个块, 耗时 {time.time() - t0:.2f}s")
    _cache_set(ck, embeddings)
    return _extract_with_embeddings(
        full_text, query, chunks, embeddings, top_k, max_chars,
        keywords=keywords,
    )


def extract_relevant_text_safe(
    full_text: str,
    query: str,
    fallback_limit: int = 4000,
    **kwargs,
) -> str:
    """同步安全包装：key 未配置或异常时降级截断。"""
    api_key = settings.ZHIPU_API_KEY
    if not api_key or api_key == "your-zhipu-api-key":
        return (full_text or "")[:fallback_limit]
    try:
        return extract_relevant_text(full_text, query, **kwargs)
    except Exception as e:
        logger.warning(f"语义匹配异常，降级截断: {e}")
        return (full_text or "")[:fallback_limit]


# ── 异步入口 ─────────────────────────────────────────────────────────────

async def extract_relevant_text_async(
    full_text: str,
    query: str,
    top_k: int = None,
    max_chars: int = None,
    page_type: str = "",
) -> str:
    early, chunks, top_k = _prepare_and_check(full_text, query, top_k, max_chars, page_type)
    if early is not None:
        return early

    keywords = _extract_query_keywords(query)

    all_texts = [query] + chunks
    ck = _cache_key(all_texts)
    cached = _cache_get(ck)

    if cached is not None:
        logger.info(f"Embedding 缓存命中 ({len(chunks)} 块)")
        return _extract_with_embeddings(
            full_text, query, chunks, cached, top_k, max_chars,
            keywords=keywords,
        )

    t0 = time.time()
    try:
        embeddings = await _call_zhipu_embedding_async(all_texts)
    except Exception as e:
        logger.warning(f"语义匹配失败，降级为截断: {e}")
        min_len = settings.RELEVANCE_MIN_TEXT_LENGTH
        return full_text[:max_chars] if max_chars else full_text[:min_len * 2]

    logger.info(f"Embedding 完成: {len(chunks)} 个块, 耗时 {time.time() - t0:.2f}s")
    _cache_set(ck, embeddings)
    return _extract_with_embeddings(
        full_text, query, chunks, embeddings, top_k, max_chars,
        keywords=keywords,
    )


async def extract_relevant_text_safe_async(
    full_text: str,
    query: str,
    fallback_limit: int = 4000,
    **kwargs,
) -> str:
    """异步安全包装：key 未配置或异常时降级截断。不阻塞事件循环。"""
    api_key = settings.ZHIPU_API_KEY
    if not api_key or api_key == "your-zhipu-api-key":
        return (full_text or "")[:fallback_limit]
    try:
        return await extract_relevant_text_async(full_text, query, **kwargs)
    except Exception as e:
        logger.warning(f"语义匹配异常，降级截断: {e}")
        return (full_text or "")[:fallback_limit]
