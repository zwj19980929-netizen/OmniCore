"""
文本语义相关度提取模块
基于智谱 Embedding API 实现：全文分块 → 向量化 → 与任务意图做余弦相似度 → 取 top-k 相关块

用途：从大段网页文本中提取与用户任务最相关的部分，替代硬截断。
"""
from __future__ import annotations

import asyncio
import hashlib
import math
import re
import time
import threading
from typing import List, Optional, Tuple

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


# ── 通用提取逻辑 ─────────────────────────────────────────────────────────

def _extract_with_embeddings(
    full_text: str,
    query: str,
    chunks: List[str],
    embeddings: List[List[float]],
    top_k: int,
    max_chars: Optional[int],
) -> str:
    query_vec = embeddings[0]
    chunk_vecs = embeddings[1:]

    scored: List[Tuple[int, float]] = []
    for i, cvec in enumerate(chunk_vecs):
        sim = _cosine_similarity(query_vec, cvec)
        scored.append((i, sim))

    scored.sort(key=lambda x: x[1], reverse=True)

    actual_k = min(top_k, len(scored))
    top_indices = sorted([idx for idx, _ in scored[:actual_k]])
    selected = [chunks[i] for i in top_indices]

    result = "\n\n".join(selected)
    if max_chars and len(result) > max_chars:
        result = result[:max_chars]

    if actual_k > 0:
        logger.info(
            f"语义匹配: {len(full_text)} 字 → {len(result)} 字 "
            f"(top-{actual_k}/{len(chunks)} 块, "
            f"相似度范围 {scored[actual_k - 1][1]:.3f}~{scored[0][1]:.3f})"
        )

    return result


def _prepare_and_check(
    full_text: str,
    query: str,
    top_k: int = None,
    max_chars: int = None,
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

    chunks = _chunk_text(full_text)
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
) -> str:
    early, chunks, top_k = _prepare_and_check(full_text, query, top_k, max_chars)
    if early is not None:
        return early

    all_texts = [query] + chunks
    ck = _cache_key(all_texts)
    cached = _cache_get(ck)

    if cached is not None:
        logger.info(f"Embedding 缓存命中 ({len(chunks)} 块)")
        return _extract_with_embeddings(full_text, query, chunks, cached, top_k, max_chars)

    t0 = time.time()
    try:
        embeddings = _call_zhipu_embedding_sync(all_texts)
    except Exception as e:
        logger.warning(f"语义匹配失败，降级为截断: {e}")
        min_len = settings.RELEVANCE_MIN_TEXT_LENGTH
        return full_text[:max_chars] if max_chars else full_text[:min_len * 2]

    logger.info(f"Embedding 完成: {len(chunks)} 个块, 耗时 {time.time() - t0:.2f}s")
    _cache_set(ck, embeddings)
    return _extract_with_embeddings(full_text, query, chunks, embeddings, top_k, max_chars)


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
) -> str:
    early, chunks, top_k = _prepare_and_check(full_text, query, top_k, max_chars)
    if early is not None:
        return early

    all_texts = [query] + chunks
    ck = _cache_key(all_texts)
    cached = _cache_get(ck)

    if cached is not None:
        logger.info(f"Embedding 缓存命中 ({len(chunks)} 块)")
        return _extract_with_embeddings(full_text, query, chunks, cached, top_k, max_chars)

    t0 = time.time()
    try:
        embeddings = await _call_zhipu_embedding_async(all_texts)
    except Exception as e:
        logger.warning(f"语义匹配失败，降级为截断: {e}")
        min_len = settings.RELEVANCE_MIN_TEXT_LENGTH
        return full_text[:max_chars] if max_chars else full_text[:min_len * 2]

    logger.info(f"Embedding 完成: {len(chunks)} 个块, 耗时 {time.time() - t0:.2f}s")
    _cache_set(ck, embeddings)
    return _extract_with_embeddings(full_text, query, chunks, embeddings, top_k, max_chars)


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
