"""连续上下文 —— 她跨 moment 记得住的那一段，存在哪、什么时候断、写不进去算谁的。

一个 moment 跑完，这一轮喂进去的那条 USER 消息、她说的每一句、每一次工具调用和工具
返回，原样存下来；下一个 moment 把它们接在这一轮的输入前面。她因此不是每十分钟从头
开始，而是接着上次往下说。

这份契约是 T3（唤醒与暂停）、T4（分层裁剪）、T5（手机三层）共同的地基，五条：

一 · 粒度与日界
---------------

**一个 persona 一条，键是 ``lane:persona_id:生活日``**
（:func:`moment_transcript_id`，落在
:class:`app.domain.session_transcript.SessionTranscript` 的 ``session_id`` 列上）。
lane 在键里，两条泳道天然是两行，不需要额外的隔离字段。

**日界取生活日（CST 04:00，:func:`app.living.day_page.living_day_of`），不是自然
日。** 凌晨两点她还醒着、还在说同一件事，按自然日切就会在午夜把这一段从中间劈开，
而她自己感觉不到这个边界。04:00 是她这一天真正结束的时刻，日记那一页也按同一个边界
写（:mod:`app.living.day_page`）。

**日界一到上下文就是空的，这是设计的一部分。** 跨天由她自己写下的那一页、她心里挂
着的事和状态快照接住（:mod:`app.living.snapshot`），不靠把昨天的对话原样拖进今天。

**一个 moment 的键只按 ``began_at`` 算一次，读和写用同一个。** 一个 03:58 开始、
04:02 结束的 moment 整个算在旧的那一天里；分别算就会读一个键、写另一个键，这一轮凭
空消失。

二 · 写失败的语义
-----------------

**写失败让这一轮失败，不接受静默降级。** 上下文就是她的记忆，写不进去还照常往前走
等于静默失忆。:func:`commit_moment_transcript` 不吞任何异常，CAS 没落地也当失败抛
(:class:`TranscriptConflict`)。

**这跟旧实现的取舍反过来了，理由是失败面不一样。** 旧实现把写回当 best-effort，理由
是"抛出去会让调用方的 durable @node 把已完成的一轮当成失败去重投 / 进 DLQ"。这条论证
对 chat 那种 MQ 驱动的 @node 成立，对她这条不成立：她的两条唤醒路（
:func:`app.living.moment.life_moment_tick`、:func:`app.living.nudge.phone_nudge_tick`）
都用 ``asyncio.gather(..., return_exceptions=True)`` 逐人接住异常并只记一行日志，
tick 本身是 interval 时间源、fire-and-forget，没有重投也没有 DLQ。所以这里抛出去的
代价是"这一轮白跑了，下一拍重来"，不是"一轮被无限重投"。

**这一轮失败的代价，是已经发生的副作用可能被重放。** 这个代价本来就在：moment 记录
一直是副作用之后才落库的，:mod:`app.living.anchor` 写着为什么——moment 的身份落在时
间格上（提前来的那种落在把她叫来的那条消息上），所以重跑算出的是同一个 moment、所有
派生 id 原样对上，重放同样的动作写不出新行。残余缺口同样照旧：模型是不确定的，重放
未必做一样的事，那时两边都会留下。把上下文加进这次提交没有引入新的失败类型，只是多
了一个触发它的原因。

三 · 提交顺序
-------------

**上下文、moment 记录、感知游标、手机已读是同一次提交，不是四次。**
:func:`commit_moment_transcript` 收一个调用方的 ``AsyncSession``，跟
``insert_idempotent(moment)`` 和 :func:`app.living.phone.commit_glances` 在同一个事务
里跑（见 :func:`app.living.moment.run_moment` 的收尾）。游标住在 moment 记录的
``next_seq`` 列上，所以"她读到哪了"和"她记得什么"由同一个 commit 决定。

这直接回答两个故障：

  * **事件已消费但历史没保存**：不存在。消费（游标推进 + 手机已读）和历史是同一次
    提交，一起成功或者一起没有。旧形态里它会发生——上下文在模型跑完那一刻就写回了，
    而游标要等收尾，中间崩掉就是"她记得自己处理过，但世界认为她还没看过"。
  * **同一输入重复唤醒**：收尾没提交过的那一轮，上下文里也没有它，所以重放读到的历
    史跟上一次一模一样，它是一次真正的重放，不是"世界往前走了、她的上下文停在原地"。
    常规 moment 的重放被时间格挡成同一个 moment；被人叫来的那种身份就是把她叫来的那条
    消息，同一条消息只把她叫来一次（:func:`app.living.moment.moment_ran`）。

**模型调用不在这个事务里。** 事务在模型跑完之后才开，只包两三条 INSERT。一次几十秒
的模型调用占着一条业务连接会把连接池拖垮，:mod:`app.living.serial` 写了这条。

四 · 裁剪只有一处
-----------------

**存储层不做任何截断**（:mod:`app.agent.session`）。它原来有两条上限（200 条消息 /
256 KiB），行为是丢最老的 + 记一行警告、不影响返回值。本设计的硬顶是 200k token，
比那两条大一个数量级；两套同时生效的话她的话会被另一套规则先砍掉，而调用方拿到的
返回值一切正常，排查时看不出来。所以那两条连同实现一起删了，裁剪只留这一处。

**T4 之前这里没有任何上限。** 现在存多少就是多少，一天下来一个 persona 的上下文会
一直长。这条在 coe 泳道跑一天就会撞到模型的 context 上限——那时 ``Agent.run`` 抛错、
收尾不提交、下一拍原样重放，一直卡在同一个 moment 上。**所以 T4 的硬顶不是优化，是这
条线能跑起来的前提**，它落地之前不要让这条线连续跑一整天。

五 · 多副本
-----------

**这条线要求 agent-service 单副本，本次不放开。** 三重依赖，缺一不可：

  1. 同一个人的 moment 串行靠进程内的 ``asyncio.Lock``（:func:`app.living.serial.hold`）；
  2. framework 的 interval 时间源在多副本下每个副本各跑一份，同一拍会推进两遍——那是
     发生在锁之外的重复，换成跨进程的锁也拦不住；
  3. 现在再加一条：上下文是"读一版、算一轮、写下一版"，两个副本交错就会互相覆盖。

第三条有一道真正的门：:func:`commit_moment_transcript` 用读到的版本做 CAS，别人在
中间写过就抛 :class:`TranscriptConflict`，而不是默默盖掉。它把"看不见的互相覆盖"变
成"看得见的一轮失败"，但它**不是**多副本的许可证——前两条仍然没有解。真要上多副本，
先给时间源做 leader election。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.agent.neutral import Message
from app.agent.session import load_session, replace_session
from app.agent.trace import make_session_id
from app.living.day_page import living_day_of


class TranscriptConflict(RuntimeError):
    """她的上下文在这一轮跑的时候被别人改过了。

    进程内的排他占用保证同一个人不会有两个 moment 同时跑，所以正常永远撞不上。撞上
    就说明那个前提破了（多副本、或者有人绕开了占用），这一轮必须当失败处理：默默覆盖
    等于把另一个进程刚写下的一整段丢掉，而且没有任何痕迹。
    """


def moment_transcript_id(*, lane: str, persona_id: str, now: datetime) -> str:
    """这个人在 ``now`` 所属的那个生活日上的上下文键。

    格式 ``lane:persona_id:YYYY-MM-DD``，日期是**生活日**（CST 04:00 起算），不是
    ``now`` 的日历日。整个 moment 只算一次、读写共用（见模块 docstring 第一条）。
    """
    return make_session_id(lane, persona_id, living_day_of(now).isoformat())


async def load_moment_transcript(
    transcript_id: str,
) -> tuple[list[Message], int]:
    """她此刻的上下文，以及读到的版本号。

    没有记录（这个生活日的第一个 moment、或者刚清过库）就是 ``([], 0)``：空上下文是
    一天的正常开头，不是错误。版本号交给 :func:`commit_moment_transcript` 做 CAS，
    所以调用方必须把它带到收尾，不能中途丢掉。

    进程里不存任何副本 —— 读的就是 PG 里最新那一版。这就是"杀掉 pod 还能接着上次"
    的全部机制。
    """
    return await load_session(transcript_id)


async def commit_moment_transcript(
    transcript_id: str,
    messages: list[Message],
    *,
    expected_ver: int,
    session: Any,
) -> None:
    """把这一轮结束后的完整上下文写成下一版，落在调用方的事务里。

    ``messages`` 是**完整的新上下文**（读到的历史 + 这一轮的输入 + 这一轮模型产出的
    每一条），不是增量。``expected_ver`` 是 :func:`load_moment_transcript` 读到的那一
    版；库里已经不是它了就抛 :class:`TranscriptConflict`。

    ``session`` 是必填的：这次写入必须跟 moment 记录、感知游标、手机已读同一个事务
    （模块 docstring 第三条）。单独开一个事务就会重新造出"事件已消费但历史没保存"。

    写失败（CAS 没落地、或者 PG 抛错）一律往外抛，调用方这一轮失败。不吞。
    """
    landed = await replace_session(
        transcript_id, messages, expected_ver=expected_ver, session=session
    )
    if not landed:
        raise TranscriptConflict(
            f"上下文 {transcript_id} 在这一轮跑的时候被别人写过了"
            f"（读到的是 ver={expected_ver}）—— 同一个人有两个 moment 在并发跑，"
            f"进程内排他占用的前提破了"
        )
