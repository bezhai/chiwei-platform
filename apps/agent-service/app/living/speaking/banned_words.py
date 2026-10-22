"""禁用词：Redis 里那份 ``banned_words`` 集合，嘴交出去之前那道检查读它
（:mod:`app.living.speaking.output_safety`）。只有她开口这一条链用，所以放在 speaking 插件里，不在
能力层。

    matched = await contains(text)
    if matched:
        ...  # 挡下来；``matched`` 是撞上的那个词

先去掉空白、转小写，再按子串比（跟原来的 ``nodes/safety._check_banned_word`` 一样）。

读 Redis 走 ``RedisCapability``，键原样传：每条泳道都读不带前缀的 ``banned_words``（能力层在
2026-05-13 的 hotfix 里不再加 ``{lane}:`` 前缀，见 ``app.capabilities.redis``）。泳道之间的禁用词
只在连的是不同 Redis 时才不同：``coe-*`` 有自己的一个，``ppe-*`` 读的是 prod 的。
"""
from __future__ import annotations

from app.capabilities.redis import get_redis_capability

_KEY = "banned_words"


async def contains(text: str) -> str | None:
    """撞上的那个禁用词；干净就是 ``None``。

    ``None`` 是能力层约定允许的（``docs/guides/plugin-host.md`` 第 6 节）："没撞上"是业务结果，不是能力层的失败。读 Redis 出错照能力层的
    类型往外抛（``CapabilityCallFailed`` / ``CapabilityTimeout``）。
    """
    cap = await get_redis_capability()
    words = await cap.smembers(_KEY)
    if not words:
        return None
    normalized = text.replace(" ", "").lower()
    for word in words:
        if isinstance(word, bytes):
            word = word.decode("utf-8")
        if word in normalized:
            return word
    return None
