"""判一段要发出去的话安不安全。

两条链共用这一份：真人问她、她答的那条（事后审计，判不合格就去撤回），和她自己开口
的那条（交出去之前判，不合格就不发）。判的东西是同一件事 —— 一段将要被真人看见的
文本 —— 所以它属于能力层，不属于其中任何一条链。

**这一关坏掉时放行，但欠的账要记下来。** 坏掉时挡下来，挡的不是一条消息、是整条
线：她和三个姐妹一起哑掉，挂多久哑多久，而这道检查的实测拦截率本来就很低。用一个大
且显眼的故障去换一个小且罕见的风险不划算。代价是它**静默** —— 没有人会因为这一关挂
了而察觉，所以 :class:`OutputVerdict` 必须把"判成了"和"没判成"分开交出去。

原来的实现把异常吞在自己肚子里、两种情况返回同一个"通过"，于是"那段时间漏了多少条
没检查就发出去了"这个问题在日志里和库里都答不出来。这个模块存在的全部理由就是把这
个区分交给调用方。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from pydantic import BaseModel, Field

from app.agent.core import Agent, AgentConfig
from app.capabilities import banned_words

logger = logging.getLogger(__name__)

# 判词那一步的 agent。prompt 在 langfuse 上，两条链共用同一份。
_GUARD_OUTPUT = AgentConfig(
    "guard_output_safety", "guard-model", "post-safety-check"
)

# 模型说"不安全"、但它自己有多确定才算数。
#
# **这不是这次拍的数**：0.7 是事后审计那条链在 prod 上跑了很久的既定口径，原样沿用。
# 换一个数就是在没有任何新证据的情况下改动一道现役护栏的松紧。
_UNSAFE_ENOUGH = 0.7

_REASON_BANNED_WORD = "output_banned_word"
_REASON_UNSAFE = "output_unsafe"


class _OutputSafetyResult(BaseModel):
    is_unsafe: bool = Field(description="Response contains unsafe content")
    confidence: float = Field(ge=0, le=1)


@dataclass(frozen=True)
class OutputVerdict:
    """这段话过不过得了这一关。

    ``ok`` 和 ``checked`` 是两件事，不能合成一个：

      * ``ok=True,  checked=True``  —— 判过了，放行。
      * ``ok=False, checked=True``  —— 判过了，拦下。``reason`` / ``detail`` 说明拦
        的是什么。
      * ``ok=True,  checked=False`` —— **没判成**（超时、模型挂了、词表读不到），
        按 fail-open 放行。调用方照发可以，但这一条要记得下来 —— 它就是"那段时间
        漏了多少"里的一条。

    ``ok=False, checked=False`` 不存在：没判成就没有拦的依据。
    """

    ok: bool
    reason: str | None = None
    detail: str | None = None
    checked: bool = True


def build_guard() -> Agent:
    """判词那一步的 agent。模块级函数，测试替身从这里换，不碰真模型。"""
    return Agent(
        _GUARD_OUTPUT,
        model_kwargs={"reasoning_effort": "low"},
        update_trace=False,
    )


async def _ask_guard(text: str) -> _OutputSafetyResult:
    return await build_guard().extract(
        _OutputSafetyResult, messages=[], prompt_vars={"response": text}
    )


async def audit_output(
    text: str, *, timeout_s: float | None = None
) -> OutputVerdict:
    """判这段话能不能发出去。

    ``timeout_s`` 不给就是**不设期限**。事后审计那条链本来就没有期限（它是发出去之
    后的后台审计，慢一点只是慢一点），不在这次顺手给它加一个；她自己开口那条链是在
    一缝里同步等，必须给，否则挂住就是把她卡在网络上。

    **期限包住整段检查，不是只包住问模型那一步。** 禁用词走的是 Redis，而那条连接
    自己没有 socket / connect 超时 —— 只套住模型调用的话，词表那一步挂住时她照样出
    不来。

    期限是护栏不是决定：它管的是"这一步最多占用多久"，不是"要不要说这句话"。
    """
    if not text or not text.strip():
        # 没有内容可判。这不是欠账 —— 别让它把"漏了多少"这个数冲淡。
        return OutputVerdict(ok=True)

    if timeout_s is None:
        return await _judge(text)
    try:
        return await asyncio.wait_for(_judge(text), timeout_s)
    except TimeoutError:
        logger.warning(
            "output safety: 整段检查超过 %ss 没回来，这条按没判过算", timeout_s
        )
        return OutputVerdict(ok=True, checked=False)


async def _judge(text: str) -> OutputVerdict:
    """两步，顺序不能换：先查禁用词（便宜、确定、命中就到此为止，省掉一次模型调
    用），再问模型。

    两步各自 fail-open，但**任何一步没跑成，整条结论就是"没判成"**。
    """
    checked = True

    try:
        banned = await banned_words.contains(text)
    except Exception as e:
        logger.error("output safety: 禁用词表读不到，这条按没判过算：%s", e)
        checked = False
    else:
        if banned:
            logger.warning("output safety: 命中禁用词 %s", banned)
            return OutputVerdict(
                ok=False, reason=_REASON_BANNED_WORD, detail=banned
            )

    try:
        result = await _ask_guard(text)
    except Exception as e:
        logger.error("output safety: 判词没跑成，这条按没判过算：%s", e)
        return OutputVerdict(ok=True, checked=False)

    if result.is_unsafe and result.confidence >= _UNSAFE_ENOUGH:
        logger.warning("output safety: 判为不安全 confidence=%.2f", result.confidence)
        return OutputVerdict(
            ok=False,
            reason=_REASON_UNSAFE,
            detail=f"confidence={result.confidence}",
        )
    return OutputVerdict(ok=True, checked=checked)
