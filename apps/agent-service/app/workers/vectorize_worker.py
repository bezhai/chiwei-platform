"""
向量化 Worker - 消费 RabbitMQ 中的向量化任务

启动命令：
    uv run python -m app.workers.vectorize_worker
"""

import asyncio
import json
import logging
import signal
import uuid

from aio_pika.abc import AbstractIncomingMessage
from inner_shared.logger import setup_logging

from app.agents import InstructionBuilder, create_client
from app.clients.image_client import image_client
from app.clients.rabbitmq import (
    VECTORIZE,
    RabbitMQClient,
    _current_lane,
    _lane_queue,
)
from app.clients.redis import AsyncRedisClient
from app.orm.crud.message import (
    get_message_by_id,
    update_vector_status,
)
from app.orm.crud.message import (
    scan_pending_messages as _crud_scan_pending,
)
from app.orm.models import ConversationMessage
from app.services.content_parser import parse_content
from app.services.download_permission import check_group_allows_download
from app.services.qdrant import qdrant_service
from app.workers.error_handling import cron_error_handler, mq_error_handler

logger = logging.getLogger(__name__)

# 并发配置
CONCURRENCY_LIMIT = 10  # 并发处理数量

# 控制 worker 运行状态
_running = True

# 并发信号量
_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    """获取或创建信号量（延迟初始化，确保在事件循环中创建）"""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    return _semaphore


def _handle_signal(signum, frame):
    """处理终止信号"""
    global _running
    logger.info(f"收到信号 {signum}，准备优雅退出...")
    _running = False


async def vectorize_message(message: ConversationMessage) -> bool:
    """
    向量化消息内容并写入 Qdrant

    写入两个集合：
    1. messages_recall: 混合向量（Dense + Sparse），用于混合检索
    2. messages_cluster: 聚类向量，用于消息聚类

    Returns:
        bool: True 表示成功处理，False 表示内容为空需跳过
    """
    # 1. 解析消息内容：提取文本和图片keys
    parsed = parse_content(message.content)
    image_keys = parsed.image_keys
    text_content = parsed.render()

    # 2. 判断是否为空内容（文本为空且无图片）
    if not text_content and not image_keys:
        logger.info(f"消息 {message.message_id} 内容为空，跳过向量化")
        return False

    # 3. 权限检查：限制下载的群跳过图片下载
    if image_keys:
        allows_download = await check_group_allows_download(
            message.chat_id, message.chat_type
        )
        if not allows_download:
            logger.debug(
                f"群 {message.chat_id} 不允许下载资源，跳过 {len(image_keys)} 张图片"
            )
            image_keys = []

    # 4. 批量获取图片（优先从 TOS 缓存，fallback 到飞书下载）
    image_base64_list: list[str] = []
    if image_keys:
        tasks = [
            image_client.download_image_as_base64(key, message.message_id, "chiwei")
            for key in image_keys
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        image_base64_list = [r for r in results if isinstance(r, str) and r]

    # 5. 下载后二次空检查：图片全部下载失败且无文本时跳过
    if not text_content and not image_base64_list:
        logger.info(
            f"消息 {message.message_id} 图片下载失败或被跳过且无文本，跳过向量化"
        )
        return False

    # 6. 生成向量
    modality = InstructionBuilder.detect_input_modality(text_content, image_base64_list)
    corpus_instructions = InstructionBuilder.for_corpus(modality)
    cluster_instructions = InstructionBuilder.for_cluster(
        target_modality=modality,
        instruction="Retrieve semantically similar content",
    )

    async with await create_client("embedding-model") as client:
        # 并行生成混合向量和聚类向量
        hybrid_task = client.embed_hybrid(
            text=text_content or None,
            image_base64_list=image_base64_list or None,
            instructions=corpus_instructions,
        )
        cluster_task = client.embed(
            text=text_content or None,
            image_base64_list=image_base64_list or None,
            instructions=cluster_instructions,
        )
        hybrid_embedding, cluster_vector = await asyncio.gather(
            hybrid_task, cluster_task
        )

    # 7. 生成向量ID
    vector_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, message.message_id))

    # 8. 准备payload
    hybrid_payload = {
        "message_id": message.message_id,
        "user_id": message.user_id,
        "chat_id": message.chat_id,
        "timestamp": message.create_time,
        "root_message_id": message.root_message_id,
        "original_text": text_content,
    }
    cluster_payload = {
        "message_id": message.message_id,
        "user_id": message.user_id,
        "chat_id": message.chat_id,
        "timestamp": message.create_time,
    }

    # 9. 并行写入两个集合
    hybrid_upsert = qdrant_service.upsert_hybrid_vectors(
        collection_name="messages_recall",
        point_id=vector_id,
        dense_vector=hybrid_embedding.dense,
        sparse_indices=hybrid_embedding.sparse.indices,
        sparse_values=hybrid_embedding.sparse.values,
        payload=hybrid_payload,
    )
    cluster_upsert = qdrant_service.upsert_vectors(
        collection="messages_cluster",
        vectors=[cluster_vector],
        ids=[vector_id],
        payloads=[cluster_payload],
    )
    await asyncio.gather(hybrid_upsert, cluster_upsert)
    return True


async def process_message(message_id: str) -> None:
    """处理单条消息（带并发控制）"""
    async with _get_semaphore():
        try:
            # 1. 从数据库获取完整消息
            message = await get_message_by_id(message_id)
            if not message:
                logger.warning(f"消息 {message_id} 不存在，跳过")
                return

            # 2. 检查状态，已处理过的直接跳过
            if message.vector_status in ("completed", "skipped"):
                logger.debug(
                    f"消息 {message_id} 已处理（{message.vector_status}），跳过"
                )
                return

            # 3. 执行向量化
            success = await vectorize_message(message)

            # 4. 根据结果更新状态
            if success:
                await update_vector_status(message_id, "completed")
                logger.info(f"消息 {message_id} 向量化完成")
            else:
                await update_vector_status(message_id, "skipped")
                logger.info(f"消息 {message_id} 内容为空，已跳过")

        except Exception as e:
            logger.error(f"消息 {message_id} 向量化失败: {e}")
            await update_vector_status(message_id, "failed")


@mq_error_handler()
async def handle_vectorize(message: AbstractIncomingMessage) -> None:
    """RabbitMQ 消费回调"""
    async with message.process(requeue=False):
        body = json.loads(message.body)
        message_id = body.get("message_id")
        if not message_id:
            logger.warning("收到无 message_id 的向量化消息，跳过")
            return
        await process_message(message_id)


async def start_vectorize_consumer() -> None:
    """连接 RabbitMQ 并消费向量化队列"""
    client = RabbitMQClient.get_instance()
    await client.connect()
    await client.declare_topology()
    lane = _current_lane()
    queue = _lane_queue(VECTORIZE.queue, lane)
    await client.consume(queue, handle_vectorize)
    logger.info("Vectorize consumer started (queue=%s)", queue)


# ==================== 定时任务：捞取 pending 消息 ====================

# 捞取配置
PENDING_SCAN_BATCH_SIZE = 100  # 每批捞取数量
PENDING_SCAN_MAX_TOTAL = 1000  # 每次最多捞取总数
PENDING_SCAN_INTERVAL_SEC = 1  # 批次间隔（秒）
PENDING_SCAN_DAYS = 7  # 只捞取 N 天内的消息


async def scan_pending_messages() -> int:
    """
    扫描数据库中 pending 状态的消息，推送到 RabbitMQ

    Returns:
        int: 推送的消息数量
    """
    from datetime import datetime, timedelta

    client = RabbitMQClient.get_instance()

    # 计算 7 天前的时间戳（毫秒）
    cutoff_time = datetime.now() - timedelta(days=PENDING_SCAN_DAYS)
    cutoff_ts = int(cutoff_time.timestamp() * 1000)

    total_pushed = 0
    offset = 0

    while total_pushed < PENDING_SCAN_MAX_TOTAL:
        message_ids = await _crud_scan_pending(cutoff_ts, offset, PENDING_SCAN_BATCH_SIZE)

        if not message_ids:
            break

        # 推送到 RabbitMQ
        for message_id in message_ids:
            await client.publish(VECTORIZE, {"message_id": message_id})
            total_pushed += 1

        logger.info(f"已推送 {len(message_ids)} 条 pending 消息到队列")

        offset += PENDING_SCAN_BATCH_SIZE

        # 批次间隔，控制 QPS
        if total_pushed < PENDING_SCAN_MAX_TOTAL:
            await asyncio.sleep(PENDING_SCAN_INTERVAL_SEC)

    return total_pushed


@cron_error_handler()
async def cron_scan_pending_messages(ctx) -> None:
    """
    定时任务：扫描 pending 状态的消息并推送到向量化队列

    - 每 10 分钟执行一次
    - 每次最多捞取 1000 条
    - 只处理 7 天内的消息
    - 使用分布式锁避免重复执行
    """
    redis = AsyncRedisClient.get_instance()
    lock_key = "vectorize:pending_scan:lock"

    # 获取分布式锁（5 分钟过期）
    got = await redis.set(lock_key, "1", ex=300, nx=True)
    if not got:
        logger.info("pending 消息扫描任务正在执行中，跳过")
        return

    try:
        logger.info("开始扫描 pending 状态的消息...")
        count = await scan_pending_messages()
        logger.info(f"pending 消息扫描完成，共推送 {count} 条消息")
    except Exception as e:
        logger.error(f"pending 消息扫描失败: {e}")
    finally:
        await redis.delete(lock_key)


async def main():
    """主入口"""
    # 注册信号处理
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    # 配置日志（JSON 格式 + 文件输出，供 ELK 采集）
    setup_logging(log_dir="/logs/agent-service", log_file="vectorize-worker.log")

    await start_vectorize_consumer()

    # 保持进程运行
    while _running:
        await asyncio.sleep(1)

    logger.info("Worker 已停止")


if __name__ == "__main__":
    asyncio.run(main())
