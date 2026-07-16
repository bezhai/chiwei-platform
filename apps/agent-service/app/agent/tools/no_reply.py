"""Main-chat tool for intentionally ending a turn without replying."""

from __future__ import annotations

from app.agent.tooling import tool


@tool
async def no_reply(reason: str) -> str:
    """不回复用户，直接结束本轮对话。

    只在对方这次说的话本身有问题时调用：持续骚扰、无意义刷屏、钓鱼式逼回应、
    政治敏感话题，或者你真的不喜欢/不想接这个话题——沉默本身就是态度，不用为了
    显得机灵或礼貌硬找一句话接。判断靠的是你这一刻真实的感受，不是对方发了几条、
    说了几遍：主人或家人因为找不到你多喊你几声，是惦记你、不是骚扰，不适用这条；
    只有你真的从对方这次的语气或意图里感觉到不耐烦、纠缠、找茬或没话找话时，才
    用得上。单纯因为自己当下困、没精神、心情不好，不算理由——那只该让语气变冷淡、
    话变短，不该让你整轮沉默；但如果这会儿是真的睡着了（不是困，是已经睡下），
    那不算数，这时候不回不是态度问题。调用前后都不要输出任何文字。

    调用时必须带上 reason，用自己的话简短说明这次触发的理由。

    Args:
        reason: 简短说明这次触发 no_reply 的理由。
    """
    return "本轮不回复。"
