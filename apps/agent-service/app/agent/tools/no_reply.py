"""Main-chat tool for intentionally ending a turn without replying."""

from __future__ import annotations

from app.agent.tooling import tool


@tool
async def no_reply() -> str:
    """不回复用户，直接结束本轮对话。

    当你不想回复、不喜欢这个话题、想拒绝接话，或遇到骚扰、无意义刷屏、
    钓鱼式逼回应、政治敏感话题时，调用这个工具。调用前后都不要输出任何文字。
    """
    return "本轮不回复。"
