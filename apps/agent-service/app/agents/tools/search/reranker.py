"""切片 + Rerank 模型重排模块

将搜索结果的网页内容切片，通过 cross-encoder rerank 模型跨页面重排，
返回与 query 最相关的 top-K chunks。
"""

import logging

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

CHUNK_SIZE = 2000
CHUNK_OVERLAP = 200
RESULT_TOP_K = 5
RERANK_MODEL = "Qwen/Qwen3-Reranker-4B"


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
    """对搜索结果做切片级 rerank 模型重排。

    1. 对每个 result 的 content 切片
    2. 调用 SiliconFlow rerank API（cross-encoder）
    3. 按 relevance_score 排序 → 取 top_k
    4. 异常时 fallback 到每页 content 的前 CHUNK_SIZE 字符

    Args:
        query: 搜索查询
        results: [{title, link, content, ...}]
        top_k: 返回的 chunk 数

    Returns:
        [{title, link, content (=chunk), score}]
    """
    if not settings.siliconflow_api_key:
        logger.warning("SiliconFlow API key not configured, falling back to truncation")
        return _fallback(results, top_k)

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
        documents = [c["chunk"] for c in all_chunks]

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{settings.siliconflow_base_url}/rerank",
                headers={
                    "Authorization": f"Bearer {settings.siliconflow_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": RERANK_MODEL,
                    "query": query,
                    "documents": documents,
                    "top_n": top_k,
                },
            )
            resp.raise_for_status()
            data = resp.json()

        ranked = []
        for item in data.get("results", []):
            idx = item["index"]
            c = all_chunks[idx]
            ranked.append({
                "title": c["title"],
                "link": c["link"],
                "content": c["chunk"],
                "score": item.get("relevance_score", 0),
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
