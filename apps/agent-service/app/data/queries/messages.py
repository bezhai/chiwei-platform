"""``common_message`` queries — 一个 bot 身份在会话里看得到什么。

调用方给的是 ``persona`` 名下那些 bot（``own_bots`` / ``bot_user_ids``，由
:mod:`app.data.queries.persona` 取），这一层据此回答四个问题：哪些行还没被看过、
谁点了谁的名、打开一条会话看到哪一段、她那次开口在公共层落成了什么。

**这里的判据认 ``bot_name``，不认 ``role``。** 几个 bot 挂在同一个群里时它们的出站
在这张表里全是 ``role='assistant'``，长得一模一样；分得开的只有 ``bot_name``
（``bot_config`` 里 bot → persona 那条映射）。按 ``role`` 判会同时错两次：别的 bot
说的话被整段排除，同时又被无条件当成"她自己说的"。

判据只在这个模块里定义一次（:data:`_SAID_BY_HER` / :data:`_STILL_IN_THE_CONVERSATION`
/ :data:`_STILL_UNREAD`），每个查询拼同一份 —— 各写一遍的话，抄漏一个条件就是
"信封说三条、翻开却数出两条"，而库里没有任何东西对不上。
"""
from __future__ import annotations

import uuid

from sqlalchemy import func
from sqlalchemy.future import select
from sqlalchemy.sql import text

from app.data.models import CommonMessage
from app.runtime.db import auto_tx, current_session

__all__ = [
    "find_unread_summary",
    "find_unread_senders",
    "find_newest_unread_summons",
    "find_conversation_window",
    "find_messages_known_through",
    "search_conversations_by_name",
    "find_file_items_in_conversations",
    "find_messages_by_outbound_ids",
    "find_recall_state_by_outbound_ids",
]


# ---------------------------------------------------------------------------
# 判据：这话是谁说的 / 这行还在不在 / 她看过没有
# ---------------------------------------------------------------------------

# **``role`` 说不出"是谁说的"。** ``role='assistant'`` 只意味着"某个 bot 发的"，而
# 几个 bot 挂在同一个群里时它们的出站在这张表里长得一模一样。唯一分得开的是
# ``bot_name`` —— ``bot_config`` 里 bot → persona 那条映射（出站的写入方两边都填了
# 这一列：``apps/lark-service/src/lark/outbound/deliver.ts`` 和 channel-server 的 QQ
# 投影）。
#
# ``bot_name`` 为空的历史行按"她自己说的"算。认不出是谁的 bot 时宁可少一条未读，也
# 不能把她自己的话摆成别人在说 —— 那正是前几次栽过的病（她把自己的回声当成别人在
# 说话）。反过来漏一条只是少看见一句，下一条来了照样看得见。
_SAID_BY_HER = (
    "(cm.role = 'assistant'"
    " AND (cm.bot_name IS NULL"
    "      OR cm.bot_name = ANY(CAST(:own_bots AS text[]))))"
)


# 「这一行还在会话里吗」。**除了"打开会话"那一处，每一处读 ``common_message`` 的地方
# 都带着它**（那一处的判据是 :data:`_VISIBLE_WHEN_SHE_OPENS_IT`）。
#
# 撤回不删那一行（公共层是消息记录，删行会打断历史），撤成功只在 ``recalled_at`` 上
# 留个时刻，由投递侧写（撤失败不填）。所以读的一侧不管的话，她自己刚撤掉的话还会原样
# 出现在她眼前 —— 然后她接着那句往下说，而对面早就看不到了。
#
# **判据写在这一列的含义上**（这一行在渠道上已经不在了），不写在谁撤的它上面：这几处
# 回答的是"这条还算不算数"——算未读、叫不叫她、按名字搜得到搜不到，她自己撤的和同群
# 别人撤的是同一件事，没有理由分开对待。
#
# NULL = 没撤过（或者还没撤掉），这是绝大多数行的样子，所以这个条件不能写成
# ``= false`` 之类会被 NULL 吃掉的形状。
_STILL_IN_THE_CONVERSATION = "cm.recalled_at IS NULL"

# 「这条消息点了她的名」的唯一判据。
#
# 读的是 ``common_message.mentioned_common_user_ids`` —— 投影层在落账时算好写下的
# 那一列，不是 ``content``。公共层的内容契约里没有 mention 这种片段（只有
# text/image/audio/file/sticker/unsupported 六种），@ 在投影时就被内联回了正文，
# 所以从 ``content`` 里根本认不出被点的是谁。
#
# **列是 NULL 时这个表达式是 NULL，不是 false**，而 NULL 在 WHERE 和 BOOL_OR 里都
# 不算真。这正是要的：NULL = 没人算过这条消息（加列前的存量行、QQ 的行、飞书新写入
# 方上线前的行），既然没算过，就不能当成"确认点了她"，也不能当成"确认没点她"。往
# 这上面套 COALESCE 会把这个区分抹平。
#
# 两边都转成 text[] 再比：绑定进来的是一串 uuid 字符串（``find_bot_user_ids_for_persona``
# 那边 ``str()`` 出来的），跟本仓库其他 ``CAST(:x AS text[])`` 用同一种绑定形状。
_NAMED_HER = (
    "cm.mentioned_common_user_ids::text[] && CAST(:bot_user_ids AS text[])"
)

# 「这一行她还没看过吗」——未读集合 U 的判据，**全模块只有这一处定义**。
#
# 四个地方用它，其中三个拿它当 ``WHERE``：信封的未读计数
# （:func:`find_unread_summary`）、信封上点谁的名（:func:`find_unread_senders`）、
# 谁在叫她（:func:`find_newest_unread_summons`，那边再 ``AND`` 上一条额外条件）。
# 第四处是打开会话那条查询，它拿这个集合去标窗口里哪几行是新的。
#
# **必须是同一份。** 四处各写一遍同样的三个条件，抄漏一个就是"信封说三条、翻开却数
# 出两条"——她只会以为自己漏看了，而库里没有任何东西对不上。
#
# 收 ``:after_ms`` / ``:after_id`` 两个绑定参数，所以每个用它的查询都得带上游标。
#
# **游标是复合的。** 只按 ``event_time > 水位`` 开窗的话，**整个那一毫秒**都被排除
# —— 一条跟她刚读那条同毫秒、但晚一步落库的消息就此永久消失。所以水位是
# ``(event_time, common_message_id)``，按字典序推进。
_STILL_UNREAD = f"""(
       NOT {_SAID_BY_HER}
   AND {_STILL_IN_THE_CONVERSATION}
   AND (cm.event_time, CAST(cm.common_message_id AS text))
       > (:after_ms, :after_id)
)"""

# 「她打开这条会话时这一行还看得见吗」。**只有这一处的判据跟
# :data:`_STILL_IN_THE_CONVERSATION` 不同**，差的就是她自己撤掉的那条。
#
# 她撤完之后不知道自己撤了什么（coe-living 2026-09-04 实测：撤完 8 分钟还在问主人
# 撤了啥）。三种做法只有一种成立：
#
#   * 原样显示 → 她会接着一句对面看不到的话往下说，这正是当初加过滤的原因；
#   * 留个白洞 → 等于没修，她仍然不知道那里曾经有什么；
#   * **留痕迹并带原话** → 符合真实的信息状态：她自己知道撤了什么（真人能点开重新
#     编辑），对面不知道内容但知道有这么回事。
#
# **别人撤掉的仍然不显示**（判据里那个 :data:`_SAID_BY_HER`）：真人那侧看到的是
# "XX 撤回了一条消息"，内容确实没了；给她留一条带原话的痕迹，就是让她看到的会话跟
# 对面看到的不是同一个。
_VISIBLE_WHEN_SHE_OPENS_IT = f"(cm.recalled_at IS NULL OR {_SAID_BY_HER})"


# 下面两条查询各自要在"某一批会话"里找东西。那批会话**由调用方给定**，不在这里算。
#
# 一度是在语句里内联一份 :func:`app.data.queries.persona.find_conversations_with_persona_bot`
# 的手抄副本，理由是"一条语句一个快照"。代价是同一条口径有了两份实现，而**可达性有
# 几个来源，白名单就要落几处闸** —— 收窄了主路而这两条照旧，她仍然能按名字搜出集合
# 外的地址、读到集合外的文件。所以副本删掉，集合从外面传进来。
#
# 换来的代价是快照从一个变成两个：先定集合、再查消息，中间 bot 被移出会话的话结果里
# 会多一条刚失效的。可以接受 —— bot 进出会话是人工操作、不是每秒发生的事，而且真要
# 把话发出去还得再过一次实时校验。
#
# **空集合就是空结果**，不是"不加限制"。这条 fail-closed 由 ``UNNEST`` 天然给出（空
# 数组展开成零行），但它是安全边界，所以两条查询各有一个用例钉着。
_GIVEN_CONVERSATIONS_CTE = """
  SELECT * FROM UNNEST(
      CAST(:channel_ids AS uuid[]),
      CAST(:scopes      AS text[]),
      CAST(:titles      AS text[])
  ) AS given(channel_id, scope, title)
"""


def _unzip_conversations(
    conversations: list[dict],
) -> dict[str, list[str]]:
    """把会话集合拆成三个平行数组，喂给 :data:`_GIVEN_CONVERSATIONS_CTE`。

    入参的形状就是 :func:`app.data.queries.persona.find_conversations_with_persona_bot`
    的出参（多余的键忽略）—— 两条查询的入口和那个查询的出口对得上，调用方不需要在
    中间翻译一道。
    """
    return {
        "channel_ids": [str(c["channel_id"]) for c in conversations],
        "scopes": [str(c["scope"]) for c in conversations],
        "titles": [str(c.get("title") or "") for c in conversations],
    }


# ---------------------------------------------------------------------------
# 未读：信封那一侧
# ---------------------------------------------------------------------------

# 未读 = 不是她自己说的、event_time 在她这条会话的水位之后。她自己发出去的那些不算
# 未读——那是她说的话。**别的 bot 说的算**：同一个群里的动静她本该感知到。
_UNREAD_SUMMARY_SQL = f"""
SELECT COUNT(*)                          AS unread,
       MIN(cm.event_time)                AS earliest,
       MAX(cm.event_time)                AS latest,
       BOOL_OR({_NAMED_HER})             AS named_you
  FROM common_message cm
 WHERE cm.common_conversation_id = CAST(:channel_id AS uuid)
   AND {_STILL_UNREAD}
"""

_UNREAD_SENDERS_SQL = f"""
SELECT COALESCE(cm.sender_display_name, '某人') AS who,
       MAX(cm.event_time)                       AS latest
  FROM common_message cm
 WHERE cm.common_conversation_id = CAST(:channel_id AS uuid)
   AND {_STILL_UNREAD}
 GROUP BY 1
 ORDER BY 2 DESC
 LIMIT :limit
"""


async def find_unread_summary(
    *,
    channel_id: str,
    after_ms: int,
    after_id: str,
    bot_user_ids: list[str],
    own_bots: list[str],
) -> dict | None:
    """这条会话上她还没看过的那些：几条、最早最晚是什么时候、有没有人点她的名。

    ``named_you`` 三态：``True``（确认点了她）/ ``False``（确认没点）/ ``None``
    （没人算过这批消息的 mention 列，见 :data:`_NAMED_HER`）—— 调用方不能把
    ``None`` 折成 ``False``。

    聚合无 ``GROUP BY``，所以一条未读都没有时也回一行（``unread=0``）。
    """
    async with auto_tx():
        row = (
            await current_session().execute(
                text(_UNREAD_SUMMARY_SQL),
                {
                    "channel_id": channel_id,
                    "after_ms": after_ms,
                    "after_id": after_id,
                    "bot_user_ids": bot_user_ids,
                    "own_bots": own_bots,
                },
            )
        ).mappings().first()
    return dict(row) if row is not None else None


async def find_unread_senders(
    *,
    channel_id: str,
    after_ms: int,
    after_id: str,
    own_bots: list[str],
    limit: int,
) -> list[dict]:
    """这条会话上还没被她看过的消息是谁发的，按最近说话的先后取前 ``limit`` 个。

    不是"最重要的几个"，就是按最近说话排。发件人没名字的行落 ``某人``（不暴露
    raw user_id，跟渲染层其它地方同一口径）。
    """
    async with auto_tx():
        rows = (
            await current_session().execute(
                text(_UNREAD_SENDERS_SQL),
                {
                    "channel_id": channel_id,
                    "after_ms": after_ms,
                    "after_id": after_id,
                    "own_bots": own_bots,
                    "limit": limit,
                },
            )
        ).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 谁在叫她
# ---------------------------------------------------------------------------

# 在叫她 = 私聊来的任意一条，或者群里点了她名字的那条。除此之外没有分级。
#
# **别的 bot 在群里说话不在这里面。** 它的话进未读、进信封（同一个群里的动静她本该
# 感知到），但群里不点名就是背景音 —— 不点名却算召唤的话，两个 agent 在一个群里会
# 互相把对方叫醒，永远停不下来。点名了就跟真人点名一样算，同一条判据，不多不少。
_SUMMONS_SQL = f"""
SELECT cm.common_message_id AS message_id,
       cm.event_time        AS at_ms
  FROM common_message cm
 WHERE cm.common_conversation_id = CAST(:channel_id AS uuid)
   AND {_STILL_UNREAD}
   AND (:is_direct OR {_NAMED_HER})
 ORDER BY cm.event_time DESC, cm.common_message_id DESC
 LIMIT 1
"""


async def find_newest_unread_summons(
    *,
    channel_id: str,
    after_ms: int,
    after_id: str,
    is_direct: bool,
    bot_user_ids: list[str],
    own_bots: list[str],
) -> dict | None:
    """这条会话上还没被她看过、而且**在叫她**的那批里最新的一条；没有返回 ``None``。"""
    async with auto_tx():
        row = (
            await current_session().execute(
                text(_SUMMONS_SQL),
                {
                    "channel_id": channel_id,
                    "after_ms": after_ms,
                    "after_id": after_id,
                    "is_direct": is_direct,
                    "bot_user_ids": bot_user_ids,
                    "own_bots": own_bots,
                },
            )
        ).mappings().first()
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# 打开一条会话
# ---------------------------------------------------------------------------

# 打开会话那一眼。展示窗口 W、未读总数 ``|U|``、``max(U)`` 由**同一条语句**一次给出。
#
# **为什么必须是一条。** ``app/data/session.py`` 没配更强的隔离级别，PostgreSQL 默认
# ``READ COMMITTED`` 下同一个事务里连续两条 ``SELECT`` 各取各的快照。分成两条时，一条
# 在两次查询之间提交的新消息不在窗口里、却可能成为未读里最新那条 —— 游标推到它身上，
# 这条她从没见过的消息就被永久跳过了，一句报错都没有。并发撤回同样会让「其中 N 条是
# 新的」跟未读总数互相对不上。单条语句只取一个快照，三个答案必然出自同一份事实。
#
# ``unread`` 是未读集合 U，判据是 :data:`_STILL_UNREAD`（全模块唯一那份）。窗口那侧
# 不重写一遍判据，而是 ``LEFT JOIN`` 回这个集合：``is_unread`` 于是**字面上就是**
# "这一行在 U 里"，「其中 N 条是新的」＝ ``|U ∩ W|`` 由此成为结构上的事实，不再靠两处
# 判据长得一样来维持。``common_message_id`` 是主键，join 不会把窗口里的行放大。
#
# 窗口 W 收游标参数但**不按游标过滤**：一行都不会因为"读过了"而被挡在窗口外。窗口
# 回答"这条会话最近说了些什么"，未读回答"其中哪些是新的"。
#
# 多带的几列各有各的用处，缺一个她就少知道一件事：``said_by_you`` 决定这一行署"你"
# 还是署那个人的名字（认 ``bot_name``，不认 ``role``）；``recalled_at`` 决定要不要写
# 明这条已经撤回；``agent_outbound_id`` 是她能拿去撤回的那个编号，只有她主动发起的
# 行才有。
#
# ``unread_total`` / ``newest_unread_*`` 三列在每一行上都一样（标量子查询）。窗口一行
# 都没有时整条语句返回零行，这三个答案也就无从读起 —— **而那恰好是对的**：U 里每一行
# 都满足 ``recalled_at IS NULL``，也就必然满足 ``recalled_at IS NULL OR 是她说的``，
# 所以 U 是窗口候选集的子集；候选集非空时 ``LIMIT``（≥1）取出的窗口也非空。反过来推：
# 窗口为空 ⟹ 候选集为空 ⟹ U 为空。**"窗口为空但未读非空"在同一个快照里不可能发生。**
# 万一这个推理哪天被破坏（比如 limit 变成 0），零行的后果是"什么都没看到、游标不动"
# —— 宁可重看、不可漏看那一侧，不会静默跳过任何一条。
_OPEN_CONVERSATION_SQL = f"""
WITH unread AS (
  SELECT cm.common_message_id AS message_id,
         cm.event_time        AS at_ms
    FROM common_message cm
   WHERE cm.common_conversation_id = CAST(:channel_id AS uuid)
     AND {_STILL_UNREAD}
),
newest_unread AS (
  SELECT message_id, at_ms
    FROM unread
   ORDER BY at_ms DESC, message_id DESC
   LIMIT 1
),
recent AS (
  SELECT cm.common_message_id AS message_id,
         COALESCE(cm.sender_display_name, '某人') AS who,
         {_SAID_BY_HER}       AS said_by_you,
         cm.content           AS content,
         cm.content_text      AS content_text,
         cm.event_time        AS at_ms,
         cm.recalled_at       AS recalled_at,
         cm.agent_outbound_id AS outbound_id,
         (u.message_id IS NOT NULL) AS is_unread
    FROM common_message cm
    LEFT JOIN unread u ON u.message_id = cm.common_message_id
   WHERE cm.common_conversation_id = CAST(:channel_id AS uuid)
     AND {_VISIBLE_WHEN_SHE_OPENS_IT}
   ORDER BY cm.event_time DESC, cm.common_message_id DESC
   LIMIT :limit
)
SELECT r.*,
       (SELECT COUNT(*) FROM unread)          AS unread_total,
       (SELECT message_id FROM newest_unread) AS newest_unread_id,
       (SELECT at_ms FROM newest_unread)      AS newest_unread_ms
  FROM recent r
 ORDER BY r.at_ms DESC, r.message_id DESC
"""


async def find_conversation_window(
    *,
    channel_id: str,
    after_ms: int,
    after_id: str,
    own_bots: list[str],
    limit: int,
) -> list[dict]:
    """打开一条会话那一眼：最近 ``limit`` 条往来 + 未读总数 + 未读里最新那条。

    一条语句给出三个答案，理由写在 :data:`_OPEN_CONVERSATION_SQL` 上：两条语句就是
    两个快照，中间提交的那条消息会被永久跳过。``unread_total`` /
    ``newest_unread_id`` / ``newest_unread_ms`` 在每一行上都一样，取第一行即可。

    窗口按 ``at_ms`` 降序（最近的在前），**含她自己撤掉的那条**（带 ``recalled_at``
    留痕迹）、不含别人撤掉的。``content`` 是 jsonb 原样，没有解析。
    """
    async with auto_tx():
        rows = (
            await current_session().execute(
                text(_OPEN_CONVERSATION_SQL),
                {
                    "channel_id": channel_id,
                    "after_ms": after_ms,
                    "after_id": after_id,
                    "own_bots": own_bots,
                    "limit": limit,
                },
            )
        ).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 她**已经知道**的那一段
# ---------------------------------------------------------------------------

# 边界就是游标：她看过的（event_time <= 水位）+ **她自己**发出去的。**没看过的一个
# 字都不给** —— 给它未读内容，"内容要她去看"这条线当场就漏了：她会回应一句自己根本
# 没读过的话。
#
# 绕过游标那道门是给"她当然知道自己说过什么"留的，**只有她自己的话走得进来**。别的
# bot 的话从这道门溜进来会同时破两条线：白送未读内容，而且渲染时被署成"你"。
_KNOWN_SQL = f"""
SELECT COALESCE(cm.sender_display_name, '某人') AS who,
       {_SAID_BY_HER}       AS said_by_you,
       cm.content           AS content,
       cm.content_text      AS content_text,
       cm.event_time        AS at_ms
  FROM common_message cm
 WHERE cm.common_conversation_id = CAST(:channel_id AS uuid)
   AND {_STILL_IN_THE_CONVERSATION}
   AND (
        {_SAID_BY_HER}
        OR (cm.event_time, CAST(cm.common_message_id AS text))
           <= (:cursor_ms, :cursor_id)
   )
 ORDER BY cm.event_time DESC, cm.common_message_id DESC
 LIMIT :limit
"""


async def find_messages_known_through(
    *,
    channel_id: str,
    cursor_ms: int,
    cursor_id: str,
    own_bots: list[str],
    limit: int,
) -> list[dict]:
    """这条会话上她已经知道的那一段：游标之前的 + 她自己说过的，最近 ``limit`` 条。

    按 ``at_ms`` **降序**返回（最近的在前），撤掉的行一条都不在里面。
    """
    async with auto_tx():
        rows = (
            await current_session().execute(
                text(_KNOWN_SQL),
                {
                    "channel_id": channel_id,
                    "cursor_ms": cursor_ms,
                    "cursor_id": cursor_id,
                    "own_bots": own_bots,
                    "limit": limit,
                },
            )
        ).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 按名字找回一条会话
# ---------------------------------------------------------------------------

# 匹配两边：群按会话标题（群基本都有名），私聊按**在里面说过话的人**
# （``sender_display_name``，也正是信封上给她看的那个"谁"——她搜的名字和她见过的
# 名字是同一个）。私聊会话本身多半没有标题：prod 实测 205 条私聊里 158 条
# ``display_name`` 是空的，所以只查标题等于查不到人。
#
# 只在调用方给的那批会话里找（:data:`_GIVEN_CONVERSATIONS_CTE`）—— 查出来的地址必须
# 是她真能用的，否则这只手只是把 fail-loud 从"找不到"推迟到"发不出去"。
#
# **匹配的是 ``sender_display_name``，不是 ``common_user.display_name``。** 后者有索引、
# 表也小（prod 12973 行），但她从来没见过那个名字：信封和会话正文给她看的"谁"全都是
# ``sender_display_name``。两者在 prod 上 4652 组里有 2157 组不一致（46%），按 common_user
# 搜等于让她搜一个自己没见过的名字。
#
# 代价是没有索引可用，只能扫。**过滤必须下推进扫描**（``WHERE ... ILIKE`` 在 matched
# 里，不是先聚合再 FILTER）：prod 实测 akao 名下 323 条会话共 254 万条消息，下推之后
# 是一次并行 seq scan，EXPLAIN ANALYZE 386ms。这只手她一天调不了几次、不在每一缝的
# 路径上，386ms 换"她能主动找回一个人"是划算的——所以这里不加时间窗，加了她就再也
# 找不回久没联系的人。
_LOOK_UP_SQL = f"""
WITH mine AS (
{_GIVEN_CONVERSATIONS_CTE}
),
matched AS (
  SELECT cm.common_conversation_id AS channel_id,
         COALESCE(cm.sender_display_name, '某人') AS who,
         MAX(cm.event_time) AS latest
    FROM common_message cm
    JOIN mine m ON m.channel_id = cm.common_conversation_id
   WHERE cm.sender_display_name ILIKE :name_like
     AND NOT {_SAID_BY_HER}
     AND {_STILL_IN_THE_CONVERSATION}
   GROUP BY 1, 2
)
SELECT m.channel_id AS channel_id,
       m.scope      AS scope,
       m.title      AS title,
       ARRAY_AGG(DISTINCT x.who) FILTER (WHERE x.who IS NOT NULL) AS matched,
       MAX(x.latest) AS latest
  FROM mine m
  LEFT JOIN matched x ON x.channel_id = m.channel_id
 GROUP BY m.channel_id, m.scope, m.title
HAVING m.title ILIKE :name_like OR COUNT(x.who) > 0
 ORDER BY m.channel_id
"""


async def search_conversations_by_name(
    *,
    conversations: list[dict],
    name_like: str,
    own_bots: list[str],
) -> list[dict]:
    """``conversations`` 里名字对得上 ``name_like`` 的那些。

    集合由调用方给定（形状同
    :func:`app.data.queries.persona.find_conversations_with_persona_bot` 的出参），
    这条查询**不自己算她有哪些会话** —— 理由见 :data:`_GIVEN_CONVERSATIONS_CTE`。
    集合为空就返回空。

    ``name_like`` 是完整的 ``ILIKE`` 模式（调用方自己加 ``%``）。群按标题匹配，
    私聊按在里面说过话的人匹配 —— 私聊多半没有标题，只查标题等于查不到人。
    ``matched`` 是对得上的那些人名（一条都没有时是 ``None``）。
    """
    async with auto_tx():
        rows = (
            await current_session().execute(
                text(_LOOK_UP_SQL),
                {
                    **_unzip_conversations(conversations),
                    "name_like": name_like,
                    "own_bots": own_bots,
                },
            )
        ).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 有人发到她手机上的文件
# ---------------------------------------------------------------------------

# 文件项的字段名是 ``kind`` / ``key``（``meta.file_name`` 放原始文件名），跟渠道投影
# 写进 ``common_message.content`` 的形状一致 —— 见 ``tests/living/test_reading.py`` 里
# 那份照抄真实记录的常量。**曾经写成 ``type`` / ``value``**：SQL 和测试数据用了同一份
# 臆造形状、彼此自洽所以全绿，而线上一个文件都查不出来。改这几个字段名之前先去库里看
# 一条真实记录。
#
# **不加时间窗。** 加了她就再也读不到上个月别人发来的那本书 —— 那正是"阈值替她
# 遗忘"。代价是全表扫，用 ``content @> '[{"kind":"file"}]'`` 先把绝大多数消息挡在
# 展开之前（jsonb 包含判断，比逐条展开数组便宜得多）。展开时仍要挡一次
# ``jsonb_typeof = 'array'``：content 不是数组的历史行会让 jsonb_array_elements 直接
# 报错。
#
# ``DISTINCT`` 是必需的：一个 persona 名下可能挂着好几个 bot（正式那个和 dev 那个），
# 同一条会话会被 presence 匹配出好几行，不去重同一个文件就会列出来好几遍。
#
# ``recalled_at`` 在这里**不做过滤，只当一列事实读出来**（``still_gettable``）。撤回
# 不删公共层那一行（那是消息记录，删行会打断历史），只在这一列上留个时刻，而这一列说的
# 是"渠道上还有没有它"——也就是还取不取得到字节。
#
# 一度是 ``WHERE cm.recalled_at IS NULL``，那样查询就同时替调用方做了两个决定：拿不到
# 的不给读（对），以及她读过它这件事也一并消失（错）。她读过的那份印象是真发生过的
# 事，撤回改变不了它。所以判定留给调用方。
#
# **不复用 :data:`_STILL_IN_THE_CONVERSATION` / :data:`_VISIBLE_WHEN_SHE_OPENS_IT`。**
# 那两个判据回答的是"这行还算不算数"，跟这里的"还取不取得到字节"不是同一件事；共享
# 一个常量会让下一个改判据的人以为改一处两边都对。文件这边只有一种情况：撤掉了就拿不
# 到了，谁撤的都一样。
_SENT_FILES_SQL = f"""
WITH hers AS (
{_GIVEN_CONVERSATIONS_CTE}
)
SELECT DISTINCT
       CAST(cm.common_message_id AS text)       AS message_id,
       it->>'key'                               AS file_key,
       COALESCE(it->'meta'->>'file_name', '')   AS file_name,
       COALESCE(cm.sender_display_name, '某人') AS who,
       cm.event_time                            AS at_ms,
       h.scope                                  AS scope,
       h.title                                  AS where_title,
       (cm.recalled_at IS NULL)                 AS still_gettable
  FROM common_message cm
  JOIN hers h ON h.channel_id = cm.common_conversation_id
 CROSS JOIN LATERAL jsonb_array_elements(
       CASE WHEN jsonb_typeof(cm.content) = 'array'
            THEN cm.content ELSE '[]'::jsonb END
 ) AS it
 WHERE cm.content @> '[{{"kind": "file"}}]'::jsonb
   AND it->>'kind' = 'file'
   AND COALESCE(it->>'key', '') <> ''
 ORDER BY at_ms DESC, message_id DESC
"""


async def find_file_items_in_conversations(
    conversations: list[dict],
) -> list[dict]:
    """``conversations`` 里别人发过的每一个文件项，最近的在前。

    集合由调用方给定，这条查询不自己算她有哪些会话（同
    :func:`search_conversations_by_name`）。集合为空就返回空 —— 一条会话不在集合里，
    连它里面有什么文件、文件叫什么名字、发在哪都不该露出来。

    **撤回掉的也在里面**，带着 ``still_gettable=False`` —— 撤回改变的是"现在还能不能
    拿到"，不是"有没有发生过"。谁看得到它、谁读得起它由调用方定。
    """
    async with auto_tx():
        rows = (
            await current_session().execute(
                text(_SENT_FILES_SQL), _unzip_conversations(conversations)
            )
        ).mappings().all()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 她那次开口在公共层落成了什么
# ---------------------------------------------------------------------------


async def find_messages_by_outbound_ids(
    outbound_ids: list[uuid.UUID],
) -> list[dict]:
    """认领了这些 ``agent_outbound_id`` 的公共层行，按 ``event_time`` 升序。

    一次开口只该落一行；真出现多行（投递方重复写了）时靠这个排序让调用方取最早
    那条。**列侧不做任何 CAST** —— CAST 成 text 会绕开
    ``ix_common_message_agent_outbound_id`` 走全表扫。
    """
    stmt = (
        select(
            CommonMessage.agent_outbound_id,
            CommonMessage.common_message_id,
            CommonMessage.event_time,
        )
        .where(CommonMessage.agent_outbound_id.in_(outbound_ids))
        .order_by(
            CommonMessage.event_time.asc(),
            CommonMessage.common_message_id.asc(),
        )
    )
    async with auto_tx():
        rows = (await current_session().execute(stmt)).mappings().all()
    return [dict(r) for r in rows]


async def find_recall_state_by_outbound_ids(
    outbound_ids: list[uuid.UUID],
) -> list[dict]:
    """这些 ``agent_outbound_id`` 各自落了几行、其中几行渠道那边真撤掉了、最后一行
    是什么时候没的。

    ``recalled_at IS NOT NULL`` 是"**这一行**撤成功了"的全部判据 —— 投递侧撤失败不填
    这一列，所以空着就是还没撤掉（**不是撤失败**）。

    但"**这次开口**撤完了"要的是每一行都撤掉：一次开口被切成几段发出去时每段各一行，
    撤掉一段、另一段还挂在真人眼前，跟整条撤完不是同一件事。所以这里不按行取，按 id
    聚合出「几段 / 撤掉几段 / 最后一段什么时候没的」，**判据留给调用方**。

    ``count(recalled_at)`` 只数非空的那些，跟 ``count(*)`` 相等就是全撤掉了。
    """
    stmt = (
        select(
            CommonMessage.agent_outbound_id,
            func.count().label("parts"),
            func.count(CommonMessage.recalled_at).label("parts_recalled"),
            func.max(CommonMessage.recalled_at).label("last_recalled_at"),
        )
        .where(CommonMessage.agent_outbound_id.in_(outbound_ids))
        .group_by(CommonMessage.agent_outbound_id)
    )
    async with auto_tx():
        rows = (await current_session().execute(stmt)).mappings().all()
    return [dict(r) for r in rows]
