"""连续上下文 —— 她跨 moment 记得住的那一段，存在哪、什么时候断、写不进去算谁的。

一个 moment 跑完，这一轮喂进去的那条 USER 消息、她说的每一句、每一次工具调用和工具
返回，原样存下来；下一个 moment 把它们接在这一轮的输入前面。她因此不是每十分钟从头
开始，而是接着上次往下说。

这份契约是 T3（唤醒与暂停）、T4（分层裁剪）、T5（手机三层）共同的地基，六条：

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

**写失败不让这一轮失败：记一行 ERROR 加一个计数，下一个 moment 重铺一次状态。**
:func:`commit_moment_transcript` 自己不吞任何异常（CAS 没落地也当失败抛
:class:`TranscriptConflict`），接住它的是
:func:`app.living.moment._remember_this_round`。

**两种代价不对称。** 写上下文这一步发生在她已经开过口之后：出站的消息、生成上传的
图、换过的 ``switch_to``、挂上去的事，一样都回滚不掉。让这一轮失败只会让下一拍重放同
一件事，而发送去重键带着 moment_id 和正文——换个措辞或者跨一个时间格就对不上，她于是
把同一句话对真人再说一遍。那是用户直接看得见的错误。

**但"下一轮什么都不做"不成立。** 没写成的那一版之后，下一个 moment 读到的是更早的那
一版：跨清理点才重铺状态，没跨的话它照旧原样接着往下说 —— 她眼前最后一条是两轮之前
的，而刺激写着"离上一次过了十分钟"，那十分钟指的是她根本看不到的那一轮。所以**这个
缺口必须被下一轮发现**，发现了就立一根界桩（:func:`_gap_marker`）把她此刻的状态重铺
进去。

**发现它靠的是 moment 记录上的 ``context_ver``，不是写失败时做点什么**
（:func:`app.living.moment.lost_last_round`）。那一列是"这一轮的上下文该写成第几版"，
跟 moment 记录同一次提交、在上下文写入之前落地，所以下一轮读到的版本比它小就是没落
地。**这一条不能挂在补偿动作上**：进程崩在两次提交之间时那行 ERROR 根本来不及记，而
它留下的缺口跟写失败一模一样。

**历史一条不丢，只补一根界桩。** 整条作废（下一轮冷启动）也能让她不接在过时历史上，
但那是为一轮没写成丢掉一整天的连续上下文，而这整条线存在的理由就是那份连续。界桩自
己带着时刻，排在那段过时历史后面，先后关系因此是明确的。

**丢掉的到底是什么。** 状态快照里"你刚做过、说过"那段读的是库里的 ``Happening``，跟
moment 记录同一批已经提交，所以重铺之后她照样知道自己那一轮说过什么；丢的是那一轮的
工具返回和中间过程。**其中有一样补不回来：那一轮她在手机上读到的别人的正文。** 手机
已读跟 moment 记录同一次提交，已经推进了，而重铺只重铺她自己那一侧 —— 那些消息她主动
翻会话还找得到，但不会再被自动摆到眼前。

**观测点有两个，因为它们盖不住同一段。** ``living_context_write_failed_total``
（:data:`app.living.moment.CONTEXT_WRITE_FAILED`）只有走到写入那一步才记得上；
``living_context_gap_total``（:data:`app.living.moment.CONTEXT_GAP`）是下一轮发现缺口
时记的，崩在两次提交之间那种情形只有它看得见。

三 · 提交顺序
-------------

**moment 记录和手机已读是同一次提交；上下文在它提交之后单独写。**
``insert_idempotent(moment)`` 和 :func:`app.living.phone.commit_glances` 在同一个事务里
跑（见 :func:`app.living.moment.run_moment` 的收尾）：游标住在 moment 记录的
``next_seq`` 列上，所以"她读到哪了"和"她看过哪些手机"由同一个 commit 决定。
:func:`commit_moment_transcript` 随后在自己的事务里写，写不进去不牵动前面那一次。

这个顺序直接回答两个故障：

  * **上下文写成了、但世界认为这一轮没发生**：不存在。上下文是最后一步，前面那次提交
    没成功就根本走不到它。
  * **同一输入重复唤醒**：moment 记录先落地，所以重放读到的是"这个 moment 跑过了"。
    常规 moment 的重放被时间格挡成同一个 moment；被人叫来的那种身份就是把她叫来的那条
    消息，同一条消息只把她叫来一次（:func:`app.living.moment.moment_ran`）。

剩下的那个缺口是**世界往前走了而她的上下文停在上一轮**（上下文写失败，或者进程崩在
这两次提交之间）。它不会被静默吞掉：``context_ver`` 让下一个 moment 认得出来，代价和
处理见上面第二条。

**模型调用不在任何一个事务里。** 事务都在模型跑完之后才开，只包几条 INSERT。一次几十
秒的模型调用占着一条业务连接会把连接池拖垮，:mod:`app.living.serial` 写了这条。

四 · 裁剪只有一处
-----------------

**存储层不做任何截断**（:mod:`app.agent.session`）。它原来有两条上限（200 条消息 /
256 KiB），行为是丢最老的 + 记一行警告、不影响返回值。本设计的硬顶是 200k token，
比那两条大一个数量级；两套同时生效的话她的话会被另一套规则先砍掉，而调用方拿到的
返回值一切正常，排查时看不出来。所以那两条连同实现一起删了，裁剪只留这一处
（:func:`next_transcript`，规则见下面第六条）。

**硬顶不是优化，是这条线能连续跑的前提。** 没有它的时候上下文只增不减：到了模型的
context 上限，``Agent.run`` 抛错、收尾不提交、下一拍读到同样的历史再抛一次。日界一
到（04:00）上下文清空才自己恢复，所以症状是"这一天剩下的每一拍都在同一个地方炸"，
不是卡死在某一个 moment 上。撞顶要多久是条件估算：可用容量 ``C``、每轮固定输入
``P``、每轮新增 ``g``，一小时六轮，约 ``(C - P) / (6g)`` 小时。

五 · 多副本
-----------

**这条线要求 agent-service 单副本，本次不放开。** 三重依赖，缺一不可：

  1. 同一个人的 moment 串行靠进程内的 ``asyncio.Lock``（:func:`app.living.serial.hold`）；
  2. framework 的 interval 时间源在多副本下每个副本各跑一份，同一拍会推进两遍——那是
     发生在锁之外的重复，换成跨进程的锁也拦不住；
  3. 现在再加一条：上下文是"读一版、算一轮、写下一版"，两个副本交错就会互相覆盖。

第三条有一道真正的门：:func:`commit_moment_transcript` 用读到的版本做 CAS，别人在
中间写过就抛 :class:`TranscriptConflict`，而不是默默盖掉。它把"看不见的互相覆盖"变
成"一行看得见的 ERROR"，但它**不是**多副本的许可证——前两条仍然没有解。真要上多副本，
先给时间源做 leader election。

六 · 裁剪规则
-------------

**两档时长，固定时刻清理。** 外部素材（她读到的东西）留 ``material_minutes``，她自
己的话和动作留 ``own_minutes``，清理只发生在 ``cleanup_minutes`` 的整点上
（:func:`_cleanup_instant`，按生活日 04:00 起算）。滑动窗口每轮都改上下文开头、前缀
缓存每轮失效；固定时刻清理让两次清理之间的前缀一个字节都不动。

**"保留 1 小时"在整点清理下实际是 1–2 小时，这是设计不是 bug。** 刚过清理点写下的
东西要等到下一个清理点才可能被裁：13:05 读到的网页在 15:00 那次清理才走（1 小时
55 分），13:59 读到的同样在 15:00 走（1 小时 1 分）。验收按明确的截止线判断——
"到 14:59 还在、过了 15:00 就没了"，不说"大概一小时左右"。

**以一次完整的工具调用为单位裁，不是以单条消息。** 一个没有结果的工具调用会被
provider 拒掉整个请求，而且同一轮里多个调用和多个结果必须逐个对上，不是"开头没有孤
儿"就行。所以：调用还在保留期内时只把过期的**载荷**换成一句写死的短语
（:data:`MATERIAL_TRIMMED`），消息结构一条不动；整组过期时调用和它的全部结果一起删。

**哪些返回是素材、哪些要留着，逐只手列在** :data:`MATERIAL_TOOLS` / :data:`KEPT_TOOLS`
**上**，两份合起来必须正好覆盖 ``MOMENT_TOOLS``（有用例钉住）。分不清的那一档是留着：
留错了只是多占 token，裁错了是她拿着一个失效的句柄去发图。

**``look_at_phone`` 在"留着"那一档，它是唯一一只返回别人内容却留 4 小时的手。** 判据
不是"这是不是她读到的东西"，而是"这次返回里有没有别处找不到的凭据"：那一页上每条她
自己发的消息带着 ``take_back_id``，头上那串 ``before=`` 是往前翻唯一的入口。前者在状
态快照"你刚做过、说过"那段有副本，但那段只有最近 12 条；后者根本没有第二份。只留句
柄、换掉正文更精确，代价是裁剪这一层要解析手机那边的渲染格式 —— 那边改一个字，句柄
就静默地留不住，而症状是几小时后她拿着一个不存在的编号去撤消息。别人的正文因此多留
3 小时；一页十几条文本跟那个风险不是一个量级，而且图片块不跟着多留（它走下面那条独立
的线）。

**"素材"只指工具返回的载荷。** 每轮喂进去的那条 USER（状态快照 + 手机信封）和她自
己说的每一句都算她这一侧，走 ``own_minutes``：它们是她那段经历读得懂的骨架，先于她
的话消失的话，剩下的对白就没有了由头。

**图片块比文本先走。** 图片的地址是 TOS 预签名 URL，:data:`PICTURE_URL_MINUTES` 分钟
就死；gemini adapter 回放历史时会把 http(s) 地址重新下载成 inline bytes，下载失败直接
抛，而且抛在模型请求之前——历史里留着一张过期的图，她连"再调一次工具取一张"的机会都
没有。所以图片块不跟素材同一档：**只要它不在最新那一代里就换成**
:data:`PICTURE_TRIMMED`，同一条返回里的 ``pic=`` 句柄照留。这样一张图最长活
``cleanup_minutes`` 加一个 moment 间隔，:data:`MAX_CLEANUP_MINUTES` 把这个和
:data:`PICTURE_URL_MINUTES` 之间的余量守住。

**每次清理重铺一次状态**（:func:`_checkpoint`，内容是
:meth:`app.living.snapshot.MomentSnapshot.render_state`）：4 小时前的话被裁掉之后那段
经历只剩库里还有，所以清理时把她当前的状态（在哪、在做什么、上一次写下的那天、挂着什
么事、刚做过说过什么）作为新起点插进去。**全量状态只在这里给**——每轮送到她眼前的只
有新发生的事，她此刻的样子读一百遍字字一样，每轮重发就是把同一段话抄一遍。一天的第一
轮上下文是空的，那一下同样立一根，所以冷启动她照样知道自己站在哪。
它同时是**分代的界桩**——每条消息的"年龄下界"就是它右边第一个界桩的时刻，不需要给
每条消息单独存一个时刻。界桩之前那一代（一天里第一个界桩立起来之前写下的东西）没有
上界，一律留着，等下一个界桩立起来再算。

**裁在模型调用之前**（:func:`trim_for_round`），收尾那一步只做硬顶兜底
（:func:`next_transcript`）。只在收尾裁的话，一段带着过期图片地址的历史永远轮不到被
裁——每一轮都在 adapter 下载那一步抛错，收尾走不到；而且一次清理会连着换两次前缀，
白丢一次缓存命中。

**硬顶是兜底，不是主路。** 每次写入前估一次 token（:func:`estimate_tokens`），超过
``hard_cap_tokens`` 就从最老的组开始整组丢到 ``trim_target_tokens`` 以下，并记一行
日志；**这一轮的消息一条都不丢**，它们自己就超了的话记 ERROR 后原样写下去。估算只算
这份上下文，不含 SYSTEM 和工具定义（它们不在这里，是每次请求的固定开销，设硬顶时要
留出余量）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from inner_shared.dynamic_config import dynamic_config

from app.agent.neutral import ContentBlock, Message, Role
from app.agent.session import load_session, replace_session
from app.agent.trace import make_session_id
from app.living.day_page import living_day_bounds, living_day_of

logger = logging.getLogger(__name__)


class TranscriptConflict(RuntimeError):
    """她的上下文在这一轮跑的时候被别人改过了。

    进程内的排他占用保证同一个人不会有两个 moment 同时跑，所以正常永远撞不上。撞上
    就说明那个前提破了（多副本、或者有人绕开了占用）。这时候必须抛：默默覆盖等于把
    另一个进程刚写下的一整段丢掉，而且没有任何痕迹。接住它的是
    :func:`app.living.moment._remember_this_round` —— 这一轮仍然算数，只是她的上下文
    停在别人写下的那一版上，而这件事留了一行 ERROR。
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

    ``session`` 是必填的：这次写入跑在调用方的事务里，而那个事务只包这一件事 ——
    moment 记录和手机已读在它之前已经单独提交过了（模块 docstring 第三条）。

    写失败（CAS 没落地、或者 PG 抛错）一律往外抛，这里不吞。怎么处理由调用方定：
    :func:`app.living.moment._remember_this_round` 记一行 ERROR 并让这一轮照样算数。
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


# ---------------------------------------------------------------------------
# 分层裁剪
# ---------------------------------------------------------------------------

# 她读到的素材：读完就该沉淀成她自己的东西，过了保留期换成一句短语。
#
#   * ``look_around``      够得着的地方现在什么样 —— 快照每轮重发一份
#   * ``search_online`` / ``browse_online``  搜索结果和信息流，没有任何工具吃它们的 URL
#   * ``read_a_guide``     说明书全文，想再看就再读一遍
#   * ``run_a_script``     命令的输出。上限 4000 字（``app.capabilities.sandbox``），
#     而且已经带着"还有多少字没给你"那句实话，这里不做第二次截断，只整块换掉
MATERIAL_TOOLS = frozenset(
    {
        "look_around",
        "search_online",
        "browse_online",
        "read_a_guide",
        "run_a_script",
    }
)

# 留着的那一档，两类东西：
#
# **一 · 长期标识** —— 不是她读到的内容，是她后面还要原样抄回去的凭据：
#
#   * ``draw_a_picture`` / ``find_a_picture_online`` / ``look_at_a_picture`` /
#     ``look_through_your_pictures``  返回里的 ``pic=<32 位十六进制>``，被
#     ``send_message(pictures=[...])`` / ``look_at_a_picture(which=...)`` /
#     ``look_through_your_pictures(before=...)`` 吃。翻页那只手的最后一串还是往前翻
#     的游标
#   * ``look_for_something_to_read``  ``file=<attachment_id>``，被 ``read_a_bit(which=...)``
#     吃。``read_a_bit`` 自己指代不明时抛的那句话里也逐个印着候选的 ``file=``，
#     所以它也在这一档
#   * ``look_up_contact`` / ``look_through_your_phone``  ``channel_id=<id>``，被
#     ``look_at_phone`` / ``send_message`` 吃（翻页那只手的最后一串还是往下翻的游标）。
#     安静下来的会话不在手机通知上，这两只手是找回它的仅有的两条路
#   * ``look_at_phone``  **这一档里唯一一只返回是别人内容的手，破例在这里说清楚。**
#     它那一页上有两样凭据：每条她自己发的消息带的 ``take_back_id``，和头上那串
#     ``before=``。前者在快照的"你刚做过、说过"那段有副本，但那段只有最近 12 条，滚
#     出去的旧消息就没有第二份了；后者是往前翻**唯一**的入口，从头到尾只出现在这一
#     次返回里，换掉正文她就再也翻不回这条会话更早的地方。
#     只保留句柄、把正文换掉更精确，但那要在裁剪这一层解析手机那边渲染出来的标签 ——
#     渲染改一个字，句柄就静默地留不住了，而症状是几小时后她拿着一个不存在的编号去
#     撤消息。别人的正文因此多留 3 小时：一页十几条文本，跟这个风险不是一个量级
#     （图片块**不**跟着多留，它走 :data:`PICTURE_TRIMMED` 那条独立的线）
#
# **二 · 她自己动作的回执** —— ``switch_to`` / ``move_to`` / ``keep_in_mind`` /
# ``say`` / ``act`` / ``send_message`` / ``take_back_message`` / ``stop_for_now``。
# 这几条是"这件事到底做成了没有"的唯一记录：``send_message`` 明确区分发出去了、已经说
# 过了、交出去但没等到确认三种结局，裁掉她就会照着一个不知道有没有成功的动作再来一遍。
# ``stop_for_now`` 那句确认是她上一轮怎么收尾的唯一痕迹（它结束这一轮，后面没有她的话
# 跟着），而且整条就几个字，换成短语省不下任何东西。
#
# **新加一只手落进哪一档必须显式写下来**（用例 ``test_every_tool_she_has_is_classified``
# 会因为漏掉而失败）。分不清就放这一档：留错了只是多占 token。
KEPT_TOOLS = frozenset(
    {
        "switch_to",
        "move_to",
        "keep_in_mind",
        "say",
        "act",
        "stop_for_now",
        "send_message",
        "take_back_message",
        "look_at_phone",
        "look_up_contact",
        "look_through_your_phone",
        "look_for_something_to_read",
        "read_a_bit",
        "draw_a_picture",
        "find_a_picture_online",
        "look_through_your_pictures",
        "look_at_a_picture",
    }
)

# 过期载荷换成的那句话。**代码写死，不是概括**：概括会留下一个可能已经错了的版本，
# 而原文没了，错了没人知道（宪法原则 6：宁可不记，不可记错）。
MATERIAL_TRIMMED = "（这一段你当时读过，现在不在眼前了。还要就再去看一次。）"
PICTURE_TRIMMED = "（这张图不在你眼前了。还要看就再拿出来一次。）"

# 图片地址的寿命：``tos_client.get_file_url`` 的签名 90 分钟就过期
# （:mod:`app.living.pictures` 的 docstring 记着同一个数）。
PICTURE_URL_MINUTES = 90

# 清理周期的上限。一张图最长活一个清理周期加一个 moment 间隔（10 分钟），
# 60 + 10 = 70 < 90，留 20 分钟余量。要把周期配得更长，得先解决"回放时地址已经死了"
# 这件事本身，不能只调这个数。
#
# **另一头也得看着**：moment 间隔自己也走动态配置
# （``living_life_moment_minutes``）。把它调到 30 分钟以上，这条余量就没了 ——
# 那时候要一起把清理周期调下来。
MAX_CLEANUP_MINUTES = 60

# Dynamic Config key：五个阈值运行时都能改，不用重新部署。
MATERIAL_MINUTES_KEY = "living_context_material_minutes"
OWN_MINUTES_KEY = "living_context_own_minutes"
CLEANUP_MINUTES_KEY = "living_context_cleanup_minutes"
HARD_CAP_TOKENS_KEY = "living_context_hard_cap_tokens"
TRIM_TARGET_TOKENS_KEY = "living_context_trim_target_tokens"

# token 估算。没有能离线跑的 tokenizer（gemini 的 count_tokens 是一次网络调用，不能
# 放在每轮写库的路上），所以按字节估，而且**一律往高了估**——估低了才会真的撞上模型
# 的上限，那时是整轮抛错。
#
#   * 每 3 个 UTF-8 字节算 1 个 token。中日文一个字 3 字节 ≈ 1 token，而 SentencePiece
#     常把常用词并成一个，所以这是高估；ASCII 实测约 4 字符 1 token，按 3 字节算同样高估
#   * 一张图按 2600 算：gemini 每 768×768 一块 258 token，2048×2048 是 9 块 ≈ 2322
#   * 每条消息再加 8，算角色、id 这些框架开销
_BYTES_PER_TOKEN = 3
_PICTURE_TOKENS = 2600
_FRAME_TOKENS = 8

# 界桩那条消息的开头，两种。她读得懂，而且认得出来：只有这里写 USER 消息，她自己写
# 不出这个开头。
#
#   * :data:`CHECKPOINT_HEAD`  固定时刻的清理，往前那一段真的不在了；
#   * :data:`GAP_HEAD`         上一轮的上下文没落地，往前那一段还在、中间少了一轮。
#
# 两种在结构上是同一回事 —— 都带一个时刻、都重铺一遍状态 —— 所以都算这一代的界桩
# （:func:`_checkpoint_at` 两个都认）。**文案必须分开**：缺口那次她眼前的历史一条没
# 少，套用"再往前的那一段不在你眼前了"就是往她眼前塞一句假话。
CHECKPOINT_HEAD = "【上下文清理 "
GAP_HEAD = "【上一轮没存下来 "
_CHECKPOINT_TAIL = "】"
_CHECKPOINT_HEADS = (CHECKPOINT_HEAD, GAP_HEAD)


@dataclass(frozen=True)
class TrimPolicy:
    """裁剪的五个阈值。运行时从 Dynamic Config 读（:func:`load_trim_policy`）。"""

    material_minutes: int
    own_minutes: int
    cleanup_minutes: int
    hard_cap_tokens: int
    trim_target_tokens: int


DEFAULT_TRIM_POLICY = TrimPolicy(
    material_minutes=60,
    own_minutes=240,
    cleanup_minutes=60,
    hard_cap_tokens=200_000,
    trim_target_tokens=100_000,
)


def _holds_together(policy: TrimPolicy) -> str | None:
    """这套阈值自相矛盾在哪；没矛盾返回 ``None``。"""
    if policy.material_minutes <= 0 or policy.own_minutes <= 0:
        return "两档时长都得是正数"
    if policy.own_minutes < policy.material_minutes:
        return "她自己的话不能比素材留得还短 —— 那会留下没有结果的调用"
    if not 0 < policy.cleanup_minutes <= MAX_CLEANUP_MINUTES:
        return (
            f"清理周期得在 1..{MAX_CLEANUP_MINUTES} 分钟之间 —— "
            f"再长图片就会比它的地址（{PICTURE_URL_MINUTES} 分钟）活得久"
        )
    if policy.hard_cap_tokens <= 0 or policy.trim_target_tokens <= 0:
        return "硬顶和裁剪目标都得是正数"
    if policy.trim_target_tokens >= policy.hard_cap_tokens:
        return "裁剪目标得小于硬顶，不然撞顶之后裁不下去"
    return None


async def load_trim_policy() -> TrimPolicy:
    """这一轮按哪套阈值裁；配脏了整套退回 :data:`DEFAULT_TRIM_POLICY` 并记一行。

    **退回是整套，不是逐项。** 几个阈值之间有约束（她自己的话不能比素材短、目标得小
    于硬顶），逐项修补会拼出一套谁也没设计过的策略，而它会静默地裁错东西。

    Dynamic Config 的拉取是同步 httpx（10s 缓存），走 ``asyncio.to_thread`` 避免缓存
    刷新那一次阻塞事件循环（与 :func:`app.living.moment.life_moment_minutes` 同口径）。
    """

    def read() -> TrimPolicy:
        return TrimPolicy(
            material_minutes=dynamic_config.get_int(
                MATERIAL_MINUTES_KEY, default=DEFAULT_TRIM_POLICY.material_minutes
            ),
            own_minutes=dynamic_config.get_int(
                OWN_MINUTES_KEY, default=DEFAULT_TRIM_POLICY.own_minutes
            ),
            cleanup_minutes=dynamic_config.get_int(
                CLEANUP_MINUTES_KEY, default=DEFAULT_TRIM_POLICY.cleanup_minutes
            ),
            hard_cap_tokens=dynamic_config.get_int(
                HARD_CAP_TOKENS_KEY, default=DEFAULT_TRIM_POLICY.hard_cap_tokens
            ),
            trim_target_tokens=dynamic_config.get_int(
                TRIM_TARGET_TOKENS_KEY,
                default=DEFAULT_TRIM_POLICY.trim_target_tokens,
            ),
        )

    policy = await asyncio.to_thread(read)
    broken = _holds_together(policy)
    if broken is not None:
        logger.warning(
            "上下文裁剪的动态配置不成立（%s）：%r；本次整套退回默认值 %r",
            broken,
            policy,
            DEFAULT_TRIM_POLICY,
        )
        return DEFAULT_TRIM_POLICY
    return policy


def estimate_tokens(messages: list[Message]) -> int:
    """这一份上下文大概多少 token —— 往高了估，理由见本模块的估算常量。

    只算上下文本身：SYSTEM 正文和工具定义不在这份列表里，它们是每次请求的固定开销
    （工具定义这一份 2026-09 实测约 26 KB，按同一口径约 8.7k token），设硬顶时要在
    模型的 context 上限之外给它们和这一轮的新增留出余量。
    """
    return sum(_message_tokens(m) for m in messages)


def _text_tokens(text: str) -> int:
    return -(-len(text.encode("utf-8")) // _BYTES_PER_TOKEN)


def _message_tokens(message: Message) -> int:
    total = _FRAME_TOKENS
    content = message.content
    if isinstance(content, str):
        total += _text_tokens(content)
    else:
        for block in content:
            if block.type == "text":
                total += _text_tokens(block.text or "")
            else:
                total += _PICTURE_TOKENS
    if message.reasoning_content:
        total += _text_tokens(message.reasoning_content)
    for call in message.tool_calls:
        total += _text_tokens(call.name)
        total += _text_tokens(json.dumps(call.arguments, ensure_ascii=False))
    return total


def _cleanup_instant(now: datetime, minutes: int) -> datetime:
    """``now`` 之前最近的那个清理点，按生活日 04:00 起算。

    按生活日而不是按 Unix 纪元取整，是为了让周期跟她那一天对齐：04:00 是整点，所以
    60 分钟的周期落在每个整点上。取整让两次清理之间的截止线完全不动 —— 前缀因此逐字节
    稳定，前缀缓存才有得命中。
    """
    start, _end = living_day_bounds(living_day_of(now))
    step = timedelta(minutes=minutes)
    return start + (now - start) // step * step


def _marker(head: str, at: datetime, what_happened: str, state: str) -> Message:
    """一根界桩：发生了什么 + 那个时刻 + 她此刻的状态，作为往后那一段的新起点。"""
    return Message(
        role=Role.USER,
        content=(
            f"{head}{at.isoformat()}{_CHECKPOINT_TAIL}\n"
            f"{what_happened}你现在：\n\n{state}"
        ),
    )


def _checkpoint(at: datetime, state: str) -> Message:
    """固定时刻清理立的那根：再往前的东西这一下真的从她眼前走了。"""
    return _marker(
        CHECKPOINT_HEAD,
        at,
        "再往前的那一段不在你眼前了，只剩你自己记下来的。",
        state,
    )


def _gap_marker(at: datetime, state: str) -> Message:
    """上一轮的上下文没落地时立的那根。

    往前那一段一条没少，少的是**中间那一轮** —— 它做过说过的事照样发生了（那些跟
    moment 记录同一批已经提交），只是过程没存下来。所以这句话说的是"接不上"，不是
    "看不到"，而且它带着自己的时刻：没有它的话她眼前最后一条是两轮之前的，而刺激写
    着"离上一次过了十分钟"，那十分钟指的是她根本看不到的那一轮。
    """
    return _marker(
        GAP_HEAD,
        at,
        "上一轮你做过说过的没能存下来，往上那一段停在它**之前** —— 中间那一轮的"
        "经过接不回来了，它留下的东西在下面这份状态里。",
        state,
    )


def _checkpoint_at(message: Message) -> datetime | None:
    """这条是界桩吗（两种都算）；是就给出它的时刻。"""
    if message.role is not Role.USER or not isinstance(message.content, str):
        return None
    head = next(
        (h for h in _CHECKPOINT_HEADS if message.content.startswith(h)), None
    )
    if head is None:
        return None
    end = message.content.find(_CHECKPOINT_TAIL, len(head))
    if end < 0:
        return None
    try:
        return datetime.fromisoformat(message.content[len(head) : end])
    except ValueError:
        return None


def _groups(messages: list[Message]) -> list[list[int]]:
    """把消息切成"一次完整的工具调用"：带调用的那条 ASSISTANT + 紧跟的全部 TOOL。

    其余每条自成一组。切好之后整组留、整组删，就不会出现没有结果的调用。
    """
    groups: list[list[int]] = []
    i = 0
    while i < len(messages):
        group = [i]
        if messages[i].role is Role.ASSISTANT and messages[i].tool_calls:
            j = i + 1
            while j < len(messages) and messages[j].role is Role.TOOL:
                group.append(j)
                j += 1
            i = j
        else:
            i += 1
        groups.append(group)
    return groups


def _bounds(messages: list[Message]) -> list[datetime | None]:
    """每条消息的"最晚写于"：它右边第一个界桩的时刻，没有就是 ``None``。

    ``None`` = 它在最新那一代里，年龄无从判断，一律留着。
    """
    nearest: datetime | None = None
    out: list[datetime | None] = [None] * len(messages)
    for i in range(len(messages) - 1, -1, -1):
        out[i] = nearest
        at = _checkpoint_at(messages[i])
        if at is not None:
            nearest = at
    return out


def _call_names(messages: list[Message]) -> dict[str, str]:
    return {
        call.id: call.name for m in messages for call in m.tool_calls
    }


def _without_pictures(message: Message) -> Message:
    """图片块换成一句话，别的一个字不动。

    换成**文本块**而不是整个丢掉：``look_at_phone`` 的正文里逐张写着 ``[图片N]``，
    块数一少就跟那些编号对不上了。
    """
    content = message.content
    if not isinstance(content, list) or all(b.type == "text" for b in content):
        return message
    return Message(
        role=message.role,
        content=[
            b if b.type == "text" else ContentBlock.from_text(PICTURE_TRIMMED)
            for b in content
        ],
        reasoning_content=message.reasoning_content,
        tool_calls=message.tool_calls,
        tool_call_id=message.tool_call_id,
    )


def _faded(message: Message, *, name: str | None) -> Message:
    """过期的素材载荷换成一句写死的短语；消息结构一条不动。"""
    if message.role is not Role.TOOL or name not in MATERIAL_TOOLS:
        return _without_pictures(message)
    return Message(
        role=Role.TOOL,
        content=MATERIAL_TRIMMED,
        tool_call_id=message.tool_call_id,
    )


def _clean(
    messages: list[Message], *, at: datetime, policy: TrimPolicy
) -> list[Message]:
    """按两档时长裁一遍历史。整组过期整组删，没过期只换过期的载荷。"""
    bounds = _bounds(messages)
    names = _call_names(messages)
    own = timedelta(minutes=policy.own_minutes)
    material = timedelta(minutes=policy.material_minutes)

    kept: list[Message] = []
    for group in _groups(messages):
        bound = bounds[group[0]]
        if bound is None:
            kept.extend(messages[i] for i in group)
            continue
        age = at - bound
        if age >= own:
            continue
        if age >= material:
            kept.extend(
                _faded(messages[i], name=names.get(messages[i].tool_call_id or ""))
                for i in group
            )
        else:
            kept.extend(_without_pictures(messages[i]) for i in group)
    return kept


def _under_cap(
    messages: list[Message], *, floor: int, policy: TrimPolicy
) -> list[Message]:
    """撞上硬顶就从最老的组开始整组丢，丢到裁剪目标以下，并且一定留下一行日志。

    ``floor`` 是这一轮自己的消息条数，它们一条都不丢：丢掉刚发生的事等于这一轮白跑。
    """
    total = estimate_tokens(messages)
    if total <= policy.hard_cap_tokens:
        return messages

    tail_from = len(messages) - floor
    dropped: set[int] = set()
    running = total
    for group in _groups(messages):
        if running <= policy.trim_target_tokens or group[-1] >= tail_from:
            break
        dropped.update(group)
        running -= sum(_message_tokens(messages[i]) for i in group)

    kept = [m for i, m in enumerate(messages) if i not in dropped]
    line = (
        "上下文撞到硬顶：估 %d token > %d，裁到 %d token（%d 条 → %d 条）"
    )
    args = (total, policy.hard_cap_tokens, running, len(messages), len(kept))
    if running > policy.hard_cap_tokens:
        logger.error(
            line + "；这一轮自己就超了，只能原样写下去",
            *args,
        )
    else:
        logger.warning(line, *args)
    return kept


def trim_for_round(
    history: list[Message],
    *,
    now: datetime,
    state: str,
    policy: TrimPolicy,
    lost_last_round: bool = False,
) -> list[Message]:
    """这一轮该喂给模型的那份历史：跨过清理点就裁一遍并立一根界桩。

    没跨过、也没有缺口就把 ``history`` 原样还回来，一个字节都不动。

    **``lost_last_round`` 是"上一轮的上下文没落地"**（判据在
    :func:`app.living.moment.lost_last_round`）。这时候没跨清理点也要立一根
    （:func:`_gap_marker`，时刻取 ``now`` 而不是清理点 —— 它得排在那段过时的历史
    后面才说得清先后），把她此刻的状态重铺进去。**历史一条不丢**：少的是中间那一轮
    的经过，往前那些仍然是她真实说过的话，为一轮没写成丢掉一整天的连续上下文是更大
    的代价。

    **``history`` 是空的时候也立一根。** 空上下文就是一天的开头或者刚重启，那时她眼前
    只有这一轮的增量刺激（:func:`app.living.moment.run_moment` 只送新发生的事），没有
    这根界桩她就不知道自己在哪、在做什么、心里挂着什么。界桩说的那句"再往前的那一段不
    在你眼前了"在这两种情形下都是实话。

    **裁在模型调用之前，不是之后。** 两个理由，都是硬的：

      * *过期的图片地址会让这一轮抛错，而且抛在模型请求之前。* 只在收尾裁的话，一段
        带着死地址的历史永远轮不到被裁——每一轮都在 adapter 下载那一步炸掉，收尾根本
        走不到。停机超过签名寿命再起来就是这个形状。
      * *前缀缓存。* 这一轮喂进去的前缀和这一轮存下去的前缀因此是同一份，下一轮接着
        命中；裁在收尾的话一次清理会连着换两次前缀，白丢一次命中。
    """
    at = _cleanup_instant(now, policy.cleanup_minutes)
    # 界桩先立起来再裁：它同时是这一代的上界，立完再裁，这一代的图片当场就走。
    # 反过来（先裁后立）的话图片要等到下一轮才走，白多活一个 moment 间隔。
    staged = list(history)
    if _due(history, at):
        staged.append(_checkpoint(at, state))
    elif lost_last_round:
        # 跨清理点那根已经重铺过状态了，两根一起立没有意义。
        staged.append(_gap_marker(now, state))
    return _clean(staged, at=at, policy=policy)


def next_transcript(
    history: list[Message],
    produced: list[Message],
    *,
    policy: TrimPolicy,
) -> list[Message]:
    """这一轮结束后该存下来的完整上下文，直接交给 :func:`commit_moment_transcript`。

    ``history`` 是 :func:`trim_for_round` 裁过、这一轮真的喂给了模型的那一份，
    ``produced`` 是这一轮的输入和模型产出的每一条。两档时长在上一步已经裁完，这里
    只剩硬顶兜底。
    """
    if not produced:
        raise ValueError("这一轮一条消息都没有 —— 没有可写下去的上下文")
    return _under_cap([*history, *produced], floor=len(produced), policy=policy)


def _due(history: list[Message], at: datetime) -> bool:
    """这一轮跨过清理点了吗 —— 上一根界桩比这个清理点早就是跨过了。

    一根都没有（一天的头几轮）也算跨过：那一下把第一根界桩立起来，往后的年龄才有得算。
    """
    for message in reversed(history):
        last = _checkpoint_at(message)
        if last is not None:
            return last < at
    return True
