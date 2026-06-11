"""life 感知白名单：哪些会话的聊天回灌进 life engine。

成本止血（spec Task 5）：现在每条群消息都在唤醒 life 轮。收窄为只有白名单内
的群的消息进 life 成为她的经历；其他群只走 chat 被动回复路径（回复不受影响，
只是这段对话不再回灌进她的信箱）。

配置形态：Dynamic Config key ``life_feed_chat_whitelist``，值=逗号分隔的
common_conversation_id 列表，由运维侧配置——群 id 不硬编码进代码。

口径（bezhai 拍板）：
- p2p 私聊不过滤（用户口径只针对"群"），p2p 短路时不消费配置
- fail-closed：配置缺失/为空 → 所有群聊回灌全部跳过。配置系统挂了宁可她
  暂时听不见群聊，也不能成本失控。
"""

from __future__ import annotations

import asyncio
import logging

from inner_shared.dynamic_config import dynamic_config

logger = logging.getLogger(__name__)

LIFE_FEED_CHAT_WHITELIST_KEY = "life_feed_chat_whitelist"


def parse_whitelist(raw: str) -> frozenset[str]:
    """逗号分隔的配置串 -> 白名单集合（去空格、剔除空项）。"""
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


async def should_feed_chat_to_life(*, chat_id: str | None, is_p2p: bool) -> bool:
    """这次对话是否回灌进 life（成为她的经历 / 唤醒 life 轮）。

    Dynamic Config 的拉取是同步 httpx（10s 缓存），走 ``asyncio.to_thread``
    避免缓存刷新那一次阻塞事件循环。

    白名单**为空**（配置缺失 / 空串）时的挡下要打 warning（codex T3 小改）：
    fail-closed 把"配置丢失"和"正常名单外"挡成同一个 False，配置系统挂了 /
    key 被误删若不可感知，止血会无声变成"她永远听不见群聊"。名单非空、单纯
    不在名单内的挡下是预期行为，由调用方（chat_node 挡点）记 info。
    """
    if is_p2p:
        return True
    raw = await asyncio.to_thread(
        dynamic_config.get, LIFE_FEED_CHAT_WHITELIST_KEY, default="",
    )
    whitelist = parse_whitelist(raw)
    if not whitelist:
        logger.warning(
            "dynamic config %s is empty/missing; fail-closed: blocking ALL "
            "group-chat life feeds (chat %s skipped) — check the config if "
            "this is not intended",
            LIFE_FEED_CHAT_WHITELIST_KEY,
            chat_id,
        )
        return False
    return bool(chat_id) and chat_id in whitelist
