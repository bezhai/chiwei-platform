"""
向量化 Worker - 消费 Redis Stream 中的消息并进行向量化处理

使用 Redis Stream + Consumer Group 实现可靠的消息队列：
1. 消息持久化 - 消息写入后不会丢失
2. ACK 机制 - 只有消费者确认后消息才算处理完成
3. Pending 重试 - 消费者挂掉后，pending 消息可以被其他消费者接管

启动命令：
    uv run python -m app.workers.vectorize_worker
"""

import asyncio
import logging
import os
import signal
import time
import uuid

from inner_shared.logger import setup_logging
from redis.asyncio import Redis
from redis.exceptions import ResponseError
from sqlalchemy import update
from sqlalchemy.future import select

from app.agents import InstructionBuilder, create_client
from app.clients.image_client import image_client
from app.clients.redis import AsyncRedisClient
from app.orm.base import AsyncSessionLocal
from app.orm.models import ConversationMessage, LarkGroupChatInfo
from app.services.qdrant import qdrant_service
from app.utils.content_parser import parse_content

logger = logging.getLogger(__name__)

# Redis Stream 配置
STREAM_NAME = "vectorize_stream"
GROUP_NAME = "vectorize_workers"
CONSUMER_NAME = f"worker-{os.getpid()}"

# 重试配置
MAX_RETRIES = 3  # 最大重试次数
RETRY_DELAY_MS = 60000  # 重试间隔（毫秒）

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


# 下载权限缓存: chat_id -> (allows_download, expire_time)
_download_permission_cache: dict[str, tuple[bool, float]] = {}
_PERMISSION_CACHE_TTL = 600  # 10 分钟


async def check_group_allows_download(chat_id: str, chat_type: str) -> bool:
    """检查群聊是否允许下载资源（带缓存）

    - P2P 直接返回 True
    - group 类型查 DB，download_has_permission_setting != 'not_anyone' 时允许
    - DB 查询失败时 fail-open（返回 True）
    """
    if chat_type == "p2p":
        return True

    now = time.monotonic()
    cached = _download_permission_cache.get(chat_id)
    if cached and cached[1] > now:
        return cached[0]

    try:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(LarkGroupChatInfo.download_has_permission_setting).where(
                    LarkGroupChatInfo.chat_id == chat_id
                )
            )
            row = result.scalar_one_or_none()
            # 无记录或字段为空 → 默认允许；仅 'not_anyone' 时禁止
            allows = row != "not_anyone"
    except Exception:
        logger.warning(f"查询群 {chat_id} 下载权限失败，默认允许")
        allows = True

    _download_permission_cache[chat_id] = (allows, now + _PERMISSION_CACHE_TTL)
    return allows


def _handle_signal(signum, frame):
    """处理终止信号"""
    global _running
    logger.info(f"收到信号 {signum}，准备优雅退出...")
    _running = False


async def get_message_by_id(message_id: str) -> ConversationMessage | None:
    """从数据库获取消息"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(ConversationMessage).where(
                ConversationMessage.message_id == message_id
            )
        )
        return result.scalar_one_or_none()


async def update_vector_status(message_id: str, status: str) -> None:
    """更新消息的向量化状态"""
    async with AsyncSessionLocal() as session:
        await session.execute(
            update(ConversationMessage)
            .where(ConversationMessage.message_id == message_id)
            .values(vector_status=status)
        )
        await session.commit()


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

    # 4. 批量下载图片转Base64
    image_base64_list: list[str] = []
    if image_keys:
        # bot_name 默认 bytedance（兼容历史数据）
        bot_name = message.bot_name or "bytedance"
        tasks = [
            image_client.download_image_as_base64(key, message.message_id, bot_name)
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


async def process_message(redis: Redis, stream_id: str, message_id: str) -> None:
    """处理单条消息（带并发控制）"""
    async with _get_semaphore():
        try:
            # 1. 从数据库获取完整消息
            message = await get_message_by_id(message_id)
            if not message:
                logger.warning(f"消息 {message_id} 不存在，跳过")
                await redis.xack(STREAM_NAME, GROUP_NAME, stream_id)
                return

            # 2. 检查状态，已处理过的直接跳过
            if message.vector_status in ("completed", "skipped"):
                logger.debug(
                    f"消息 {message_id} 已处理（{message.vector_status}），跳过"
                )
                await redis.xack(STREAM_NAME, GROUP_NAME, stream_id)
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

            # 5. ACK 消息
            await redis.xack(STREAM_NAME, GROUP_NAME, stream_id)

        except Exception as e:
            logger.error(f"消息 {message_id} 向量化失败: {e}")
            # 更新状态为失败，但不 ACK，消息会保留在 pending 中
            await update_vector_status(message_id, "failed")


async def consume_stream() -> None:
    """消费 Redis Stream"""
    redis = AsyncRedisClient.get_instance()

    # 创建消费者组（如果不存在）
    try:
        await redis.xgroup_create(STREAM_NAME, GROUP_NAME, id="0", mkstream=True)
        logger.info(f"创建消费者组 {GROUP_NAME}")
    except ResponseError as e:
        if "BUSYGROUP" not in str(e):
            raise
        # 组已存在，忽略

    logger.info(f"启动向量化 Worker: {CONSUMER_NAME}")

    while _running:
        try:
            # 1. 先处理 pending 消息（之前消费失败的）
            pending_info = await redis.xpending(STREAM_NAME, GROUP_NAME)
            if pending_info and pending_info["pending"] > 0:
                # 获取 pending 消息
                pending_messages = await redis.xpending_range(
                    STREAM_NAME, GROUP_NAME, min="-", max="+", count=10
                )
                for pending in pending_messages:
                    stream_id = pending["message_id"]
                    times_delivered = pending["times_delivered"]

                    # 检查是否超过最大重试次数
                    if times_delivered > MAX_RETRIES:
                        logger.warning(
                            f"消息 {stream_id} 已重试 {times_delivered} 次，放弃重试"
                        )
                        # 超过重试次数，ACK 消息避免无限堆积
                        await redis.xack(STREAM_NAME, GROUP_NAME, stream_id)
                        continue

                    # 如果消息已经 pending 超过重试间隔，尝试重新处理
                    if pending["time_since_delivered"] > RETRY_DELAY_MS:
                        # 认领消息
                        claimed = await redis.xclaim(
                            STREAM_NAME,
                            GROUP_NAME,
                            CONSUMER_NAME,
                            min_idle_time=RETRY_DELAY_MS,
                            message_ids=[stream_id],
                        )
                        for claimed_id, data in claimed:
                            if data and "message_id" in data:
                                logger.info(
                                    f"重试消息 {data['message_id']}，"
                                    f"第 {times_delivered} 次尝试"
                                )
                                await process_message(
                                    redis, claimed_id, data["message_id"]
                                )

            # 2. 读取新消息
            messages = await redis.xreadgroup(
                GROUP_NAME,
                CONSUMER_NAME,
                streams={STREAM_NAME: ">"},
                count=10,
                block=5000,  # 阻塞 5 秒
            )

            if messages:
                # 收集所有待处理的任务
                tasks = []
                for _stream_name, entries in messages:
                    for stream_id, data in entries:
                        if data and "message_id" in data:
                            tasks.append(
                                process_message(redis, stream_id, data["message_id"])
                            )
                # 并发执行（并发数由 semaphore 控制）
                if tasks:
                    await asyncio.gather(*tasks)

        except Exception as e:
            logger.error(f"消费循环异常: {e}")
            await asyncio.sleep(5)  # 出错后等待重试

    logger.info("Worker 已停止")


# ==================== 定时任务：捞取 pending 消息 ====================

# 捞取配置
PENDING_SCAN_BATCH_SIZE = 100  # 每批捞取数量
PENDING_SCAN_MAX_TOTAL = 1000  # 每次最多捞取总数
PENDING_SCAN_INTERVAL_SEC = 1  # 批次间隔（秒）
PENDING_SCAN_DAYS = 7  # 只捞取 N 天内的消息


async def scan_pending_messages() -> int:
    """
    扫描数据库中 pending 状态的消息，推送到 Redis Stream

    Returns:
        int: 推送的消息数量
    """
    from datetime import datetime, timedelta

    redis = AsyncRedisClient.get_instance()

    # 计算 7 天前的时间戳（毫秒）
    cutoff_time = datetime.now() - timedelta(days=PENDING_SCAN_DAYS)
    cutoff_ts = int(cutoff_time.timestamp() * 1000)

    total_pushed = 0
    offset = 0

    while total_pushed < PENDING_SCAN_MAX_TOTAL:
        # 查询 pending 状态的消息
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                select(ConversationMessage.message_id)
                .where(ConversationMessage.vector_status == "pending")
                .where(ConversationMessage.create_time >= cutoff_ts)
                .order_by(ConversationMessage.create_time.desc())
                .offset(offset)
                .limit(PENDING_SCAN_BATCH_SIZE)
            )
            message_ids = [row[0] for row in result.fetchall()]

        if not message_ids:
            break

        # 推送到 Redis Stream
        for message_id in message_ids:
            await redis.xadd(STREAM_NAME, {"message_id": message_id})
            total_pushed += 1

        logger.info(f"已推送 {len(message_ids)} 条 pending 消息到队列")

        offset += PENDING_SCAN_BATCH_SIZE

        # 批次间隔，控制 QPS
        if total_pushed < PENDING_SCAN_MAX_TOTAL:
            await asyncio.sleep(PENDING_SCAN_INTERVAL_SEC)

    return total_pushed


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
    setup_logging(log_dir="/logs/ai-service", log_file="vectorize-worker.log")

    await consume_stream()


if __name__ == "__main__":
    asyncio.run(main())
