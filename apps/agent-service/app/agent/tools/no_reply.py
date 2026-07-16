"""Main-chat tool for intentionally ending a turn without replying."""

from __future__ import annotations

from app.agent.tooling import tool


@tool
async def no_reply(reason: str) -> str:
    """不回复用户，直接结束本轮对话。

    只在对方这次说的话本身有问题时调用：持续骚扰、无意义刷屏（比如反复刷同一句话、
    无意义地@你）、钓鱼式逼回应、政治敏感话题，或者你真的不喜欢/不想接这个话题——
    沉默本身就是态度，不用为了显得机灵或礼貌硬找一句话接。单纯因为自己当下困、
    没精神、心情不好，不算理由——那只该让语气变冷淡、话变短，不该让你整轮沉默。
    调用前后都不要输出任何文字。

    调用时必须带上 reason，用自己的话简短说明这次触发的理由。

    Args:
        reason: 简短说明这次触发 no_reply 的理由。
    """
    return "本轮不回复。"
