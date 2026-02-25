import logging
from datetime import datetime
from typing import Any

import numpy as np
from qdrant_client import QdrantClient
from qdrant_client.http import models
from qdrant_client.http.models import (
    Distance,
    ExtendedPointId,
    Filter,
    PointStruct,
    Prefetch,
    SparseIndexParams,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from app.config.config import settings

logger = logging.getLogger(__name__)


class QdrantService:
    def __init__(self):
        self.client = QdrantClient(
            host=settings.qdrant_service_host,
            port=settings.qdrant_service_port,
            api_key=settings.qdrant_service_api_key,
            prefer_grpc=False,
            https=False,
        )

    async def create_collection(self, collection_name: str, vector_size: int) -> bool:
        """创建新的集合"""
        try:
            self.client.create_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
            )
            return True
        except Exception as e:
            logger.warning(f"创建集合失败: {str(e)}")
            return False

    async def upsert_vectors(
        self,
        collection: str,
        vectors: list[list[float]],
        ids: list[ExtendedPointId],
        payloads: list[dict[str, Any]] | None = None,
    ) -> bool:
        """插入或更新向量"""
        try:
            self.client.upsert(
                collection_name=collection,
                points=models.Batch(
                    ids=ids, vectors=vectors, payloads=payloads or [{}] * len(vectors)
                ),
            )
            return True
        except Exception as e:
            logger.error(f"插入向量失败: {str(e)}")
            return False

    async def search_vectors(
        self, collection_name: str, query_vector: list[float], limit: int = 10
    ) -> list[dict[str, Any]]:
        """搜索最相似的向量"""
        try:
            results = self.client.search(
                collection_name=collection_name, query_vector=query_vector, limit=limit
            )
            return [
                {"id": hit.id, "score": hit.score, "payload": hit.payload}
                for hit in results
            ]
        except Exception as e:
            logger.error(f"搜索向量失败: {str(e)}")
            return []

    async def search_vectors_with_score_boost(
        self,
        collection_name: str,
        query_vector: list[float],
        query_filter: Filter | None = None,
        limit: int = 10,
        score_threshold: float = 0.8,
        time_boost_factor: float = 0.1,
    ) -> list[dict[str, Any]]:
        """搜索向量并应用时间权重提升

        Args:
            collection_name: 集合名称
            query_vector: 查询向量
            query_filter: 查询过滤器
            limit: 返回结果数量限制
            score_threshold: 相似度阈值
            time_boost_factor: 时间权重因子，值越大时间权重影响越大

        Returns:
            List[Dict[str, Any]]: 搜索结果列表，每个结果包含id、score和payload
        """
        try:
            # 获取原始搜索结果
            results = self.client.search(
                collection_name=collection_name,
                query_vector=query_vector,
                query_filter=query_filter,
                limit=limit * 2,  # 获取更多结果以便后续重排序
            )

            if not results:
                return []

            current_time = datetime.now().timestamp()

            # 对每个结果应用时间权重
            weighted_results = []
            for hit in results:
                # 获取原始相似度分数
                original_score = hit.score

                # 如果原始分数低于阈值，跳过
                if original_score < score_threshold:
                    logger.info(f"原始相似度分数低于阈值，跳过: {original_score}")
                    continue

                # 获取消息时间戳
                payload_obj = getattr(hit, "payload", None)
                if payload_obj is not None and hasattr(payload_obj, "get"):
                    msg_timestamp = payload_obj.get("timestamp", current_time)  # type: ignore[no-any-return]
                else:
                    msg_timestamp = current_time
                if isinstance(msg_timestamp, str):
                    try:
                        msg_timestamp = float(msg_timestamp)
                    except (ValueError, TypeError):
                        logger.warning(
                            f"无效的时间戳格式: {msg_timestamp}，使用当前时间"
                        )
                        msg_timestamp = current_time

                # 计算时间差（小时）
                time_diff_hours = (current_time - msg_timestamp) / 3600

                # 计算时间权重（越早的消息权重越高）
                # 使用指数衰减函数：exp(-time_diff_hours * time_boost_factor)
                time_weight = np.exp(-time_diff_hours * time_boost_factor)

                # 时间权重只用于排序，不影响原始分数
                sort_score = original_score + time_weight * time_boost_factor

                logger.info(
                    f"消息时间戳: {msg_timestamp}, 时间差(小时): {time_diff_hours}, "
                    f"时间权重: {time_weight}, 排序分数: {sort_score}, 原始分数: {original_score}"
                )

                weighted_results.append(
                    {
                        "id": hit.id,
                        "score": original_score,  # 保持原始相似度分数
                        "payload": hit.payload,
                        "sort_score": sort_score,  # 添加排序分数
                        "time_weight": time_weight,
                    }
                )

            # 按相似度和时间权重双参数排序
            weighted_results.sort(
                key=lambda x: (x["score"], x["time_weight"]), reverse=True
            )

            # 返回前limit个结果
            return weighted_results[:limit]

        except Exception as e:
            logger.error(f"带权重搜索向量失败: {str(e)}")
            return []

    async def delete_collection(self, collection_name: str) -> bool:
        """删除集合"""
        try:
            self.client.delete_collection(collection_name=collection_name)
            return True
        except Exception as e:
            logger.error(f"删除集合失败: {str(e)}")
            return False

    async def create_hybrid_collection(
        self,
        collection_name: str,
        dense_size: int = 1024,
    ) -> bool:
        """创建支持 Dense + Sparse 双向量的混合集合

        Args:
            collection_name: 集合名称
            dense_size: Dense 向量维度，默认 1024
        """
        try:
            self.client.create_collection(
                collection_name=collection_name,
                vectors_config={
                    "dense": VectorParams(size=dense_size, distance=Distance.COSINE),
                },
                sparse_vectors_config={
                    "sparse": SparseVectorParams(
                        index=SparseIndexParams(on_disk=False),
                    ),
                },
            )
            return True
        except Exception as e:
            logger.warning(f"创建混合集合失败: {str(e)}")
            return False

    async def upsert_hybrid_vectors(
        self,
        collection_name: str,
        point_id: str,
        dense_vector: list[float],
        sparse_indices: list[int],
        sparse_values: list[float],
        payload: dict[str, Any],
    ) -> bool:
        """插入混合向量（Dense + Sparse）

        Args:
            collection_name: 集合名称
            point_id: 点 ID
            dense_vector: Dense 向量
            sparse_indices: Sparse 向量索引
            sparse_values: Sparse 向量值
            payload: 元数据
        """
        try:
            point = PointStruct(
                id=point_id,
                vector={
                    "dense": dense_vector,
                    "sparse": SparseVector(
                        indices=sparse_indices,
                        values=sparse_values,
                    ),
                },
                payload=payload,
            )
            self.client.upsert(
                collection_name=collection_name,
                points=[point],
            )
            return True
        except Exception as e:
            logger.error(f"插入混合向量失败: {str(e)}")
            return False

    async def hybrid_search(
        self,
        collection_name: str,
        dense_vector: list[float],
        sparse_indices: list[int],
        sparse_values: list[float],
        query_filter: Filter | None = None,
        limit: int = 10,
        prefetch_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """混合搜索（Dense + Sparse，使用 RRF 融合）

        Args:
            collection_name: 集合名称
            dense_vector: Dense 查询向量
            sparse_indices: Sparse 查询向量索引
            sparse_values: Sparse 查询向量值
            query_filter: 过滤条件
            limit: 返回结果数量
            prefetch_limit: 预取数量，默认为 limit * 5

        Returns:
            搜索结果列表
        """
        try:
            prefetch_count = prefetch_limit or limit * 5

            # 使用 prefetch + RRF 融合
            results = self.client.query_points(
                collection_name=collection_name,
                prefetch=[
                    Prefetch(
                        query=dense_vector,
                        using="dense",
                        limit=prefetch_count,
                        filter=query_filter,
                    ),
                    Prefetch(
                        query=SparseVector(
                            indices=sparse_indices,
                            values=sparse_values,
                        ),
                        using="sparse",
                        limit=prefetch_count,
                        filter=query_filter,
                    ),
                ],
                query=models.FusionQuery(fusion=models.Fusion.RRF),
                limit=limit,
            )

            return [
                {"id": point.id, "score": point.score, "payload": point.payload}
                for point in results.points
            ]
        except Exception as e:
            logger.error(f"混合搜索失败: {str(e)}")
            return []


# 创建单例实例
qdrant_service = QdrantService()


async def init_qdrant_collections():
    """初始化所有必要的 QDrant 集合"""
    try:
        # 创建消息召回混合向量集合（Dense + Sparse）
        recall_result = await qdrant_service.create_hybrid_collection(
            collection_name="messages_recall", dense_size=1024
        )
        if recall_result:
            logger.info("Qdrant 消息召回混合向量集合创建成功")
        else:
            logger.warning("Qdrant 消息召回混合向量集合可能已存在")

        # 创建消息聚类向量集合，向量维度为1024（火山引擎多模态模型）
        cluster_result = await qdrant_service.create_collection(
            collection_name="messages_cluster", vector_size=1024
        )
        if cluster_result:
            logger.info("Qdrant 消息聚类向量集合创建成功")
        else:
            logger.warning("Qdrant 消息聚类向量集合可能已存在")
    except Exception as e:
        logger.error(f"初始化QDrant集合失败: {str(e)}")
