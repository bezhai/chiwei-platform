"""切片 + Embedding 重排模块

将搜索结果的网页内容切片，通过 embedding cosine similarity 跨页面重排，
返回与 query 最相关的 top-K chunks。
"""

import asyncio
import logging

import numpy as np

from app.agents.clients import create_client
from app.agents.infra.embedding import InstructionBuilder, Modality

logger = logging.getLogger(__name__)

CHUNK_SIZE = 2000
CHUNK_OVERLAP = 200
RESULT_TOP_K = 5
MAX_CONCURRENT_EMBEDS = 10


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """按段落边界切片，带 overlap。

    优先在段落边界（双换行）切分，fallback 到单换行，最后硬切。
    """
    if len(text) <= chunk_size:
        return [text] if text.strip() else []

    chunks: list[str] = []
    start = 0

    while start < len(text):
        end = start + chunk_size

        if end >= len(text):
            chunk = text[start:]
            if chunk.strip():
                chunks.append(chunk)
            break

        # 在 chunk_size 范围内找最后一个段落边界
        slice_ = text[start:end]
        split_pos = slice_.rfind("\n\n")
        if split_pos == -1 or split_pos < chunk_size // 2:
            split_pos = slice_.rfind("\n")
        if split_pos == -1 or split_pos < chunk_size // 2:
            split_pos = chunk_size

        chunk = text[start : start + split_pos]
        if chunk.strip():
            chunks.append(chunk)

        start = start + split_pos - overlap
        if start < 0:
            start = 0

    return chunks


async def rerank_chunks(
    query: str,
    results: list[dict],
    top_k: int = RESULT_TOP_K,
) -> list[dict]:
    """对搜索结果做切片级 embedding 重排。

    1. 对每个 result 的 content 切片
    2. 并发 embed query + 所有 chunks
    3. cosine similarity 排序 → 取 top_k
    4. 异常时 fallback 到每页 content 的前 CHUNK_SIZE 字符

    Args:
        query: 搜索查询
        results: [{title, link, content, ...}]
        top_k: 返回的 chunk 数

    Returns:
        [{title, link, content (=chunk), score}]
    """
    # 构建所有 chunks
    all_chunks: list[dict] = []
    for r in results:
        content = r.get("content", "")
        if not content:
            continue
        chunks = chunk_text(content)
        for idx, chunk in enumerate(chunks):
            all_chunks.append({
                "title": r.get("title", ""),
                "link": r.get("link", ""),
                "chunk": chunk,
                "chunk_idx": idx,
            })

    if not all_chunks:
        return _fallback(results, top_k)

    try:
        # 构建 embedding instructions
        query_instructions = InstructionBuilder.for_query(
            target_modality=Modality.TEXT,
            instruction="为这个搜索查询生成表示以用于检索相关网页内容片段",
        )
        corpus_instructions = InstructionBuilder.for_corpus(modality=Modality.TEXT)

        sem = asyncio.Semaphore(MAX_CONCURRENT_EMBEDS)

        async def _embed(text: str, instructions: str) -> list[float]:
            async with sem:
                async with await create_client("embedding-model") as client:
                    return await client.embed(text=text, instructions=instructions)

        # 并发 embed query + 所有 chunks
        tasks = [_embed(query, query_instructions)]
        for c in all_chunks:
            tasks.append(_embed(c["chunk"], corpus_instructions))

        embeddings = await asyncio.gather(*tasks)

        query_vec = np.array(embeddings[0])
        chunk_vecs = np.array(embeddings[1:])

        # cosine similarity
        query_norm = np.linalg.norm(query_vec)
        chunk_norms = np.linalg.norm(chunk_vecs, axis=1)
        # 避免除零
        chunk_norms = np.where(chunk_norms == 0, 1e-10, chunk_norms)
        similarities = chunk_vecs @ query_vec / (chunk_norms * query_norm)

        # 排序取 top_k
        top_indices = np.argsort(similarities)[::-1][:top_k]

        ranked = []
        for idx in top_indices:
            c = all_chunks[idx]
            ranked.append({
                "title": c["title"],
                "link": c["link"],
                "content": c["chunk"],
                "score": float(similarities[idx]),
            })

        return ranked

    except Exception:
        logger.exception("rerank_chunks failed, falling back to truncation")
        return _fallback(results, top_k)


def _fallback(results: list[dict], top_k: int) -> list[dict]:
    """异常时 fallback：返回每页 content 的前 CHUNK_SIZE 字符。"""
    fallback_results = []
    for r in results[:top_k]:
        content = r.get("content", "") or r.get("snippet", "")
        fallback_results.append({
            "title": r.get("title", ""),
            "link": r.get("link", ""),
            "content": content[:CHUNK_SIZE],
        })
    return fallback_results
