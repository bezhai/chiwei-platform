"""她惦记着没了结的事 —— 状态快照里唯一由她自己维护的那一层。

快照的另外三层都是**从事实读出来的当前状态**（她在哪、她刚做过什么、这段时间发生
了什么），读多少遍都一样，而且天然有界。只有这一层是她自己写的，因为它回答的是一
个只有她能回答的问题：**那些已经滚出窗口的事情里，哪几件我还惦记着。**

**开、关、保持是同一个动作。** 她调 :func:`app.living.moment.keep_in_mind` 重写一遍
整份清单（:func:`rewrite_loose_ends`）：写进去的就挂着，这次没写的就是关掉了。关不是
代码判断"这条过期了"，是**她的省略**在生效——判准是"它是在让她的决定生效，还是替她做
决定"，省略即关掉属于前者。所以这里没有任何过期阈值、没有条数上限、没有优先级排序。

**哪一缝都能调，不绑在"换事情"上。** 「是否换事」不等于「是否记住」：有人跟她说了
句要紧的话，她手上的书没放下（那一缝答「继续」），但她记住了。绑在换事情上的话，这
条感知在游标推进之后就永久消失——她自己最近那十二条里只有她**自己**说做的，别人说的
话不在里面。

**已知限制（不修，coe 上观察）：线头的身份是那句话的原文。** 所以

  * 她换个说法重述同一件事 → 算成一条**新**线头，旧的那条因为没被列出来而关掉；
  * 她某一缝漏抄了一条 → 那条就此关掉，跟她真的了结了它分不出来；
  * 关掉之后原句一字不差地再出现 → 复活，出处停在第一次那一缝，中间那段断档在数据
    上看不出来（读起来像"从头到尾一直挂着"）。

给线头做精确身份（编号、模糊匹配、相似度合并）是工程脑，而且真人的记忆本来就是这样
模糊、会漏、会自己接上的。这里选择照实记录她说了什么，把失真留在数据里让人看得见，
而不是用一套匹配规则把它藏起来。

**``thread_id`` 从内容派生**，所以同一句话重写多少遍都是同一条线头，
``opened_moment_id`` 永远停在第一次写下的那一缝。这是"跨多缝没被遗忘、而且指得出
是从哪一缝带过来的"这条验收的全部依据：清单上每一条都自带出处。

**关掉的又被列出来 = 复活，出处不变。** 她重新惦记起同一件事，说明它从最早那一缝
起就一直在她心上；给它换一个新出处等于把这段延续抹掉。

有版本链（``ver``），因为线头有一个真实的状态变化：写下 → 了结（→ 也可能重新挂
起）。用 framework 的 ``Version`` + ``insert_append`` CAS，不另起一张影子表——
"这条了结了没有"是这条线头的状态，不是另一件事。

**她的日程也挂在这里，不进** :class:`~app.living.records.Upcoming`。那张表是 world
的**客观时刻表**（快递到门口、天黑），到期交付一次就被消费掉。她的安排是另一种东西：
"我该去开的那个会"在她真的去之前，不会因为时间过了就不算数；而且把她的安排写进世界
的账本，等于 life 单方面替世界宣布将要发生什么——2026-08 那次幻觉污染就是这个形状。

所以时刻（``due_at``）是线头自己的一个可空属性，而**"到点了"不落库**，在渲染时当场
跟 ``now`` 比出来（:mod:`app.living.snapshot`）。不是因为没有定时器（``CalendarTick``
每 60 秒在跑），而是因为落库意味着有个东西替她把状态从"挂着"改成"到点了"，而那是她
的判断。到点之后那条**继续显示**，直到她自己不再列它。

**时刻写在条目里，不是一个平行参数**：``keep_in_mind`` 收的是整份清单，一个单独的
时刻参数对应不上多条事项。条目的形状是 ``[YYYY-MM-DD HH:MM] 那件事``（:func:`parse_entry`
拆开、:func:`format_entry` 拼回），而快照渲染回给她的就是拼回来的那个形状——她下一缝
照抄，解析出来必须是同一件事，所以这两个函数是一对，改一个必须改另一个。

``thread_id`` 仍然**只从** ``what`` 派生，不含时刻：改期还是同一件事。掺进去的话每次
改期都变成"旧的关掉、新开一条"，``opened_moment_id`` 那段延续就断了。"整份重写"这个
语义顺带把改期（这次列的时刻不一样）和撤销时刻（这次不写方括号）都解决了。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Annotated

from pydantic import field_validator
from sqlalchemy import text

from app.data.session import get_session
from app.infra.cst_time import CST
from app.living.records import _require_aware
from app.runtime.data import Data, Key, Version
from app.runtime.migrator import _table_name
from app.runtime.persist import insert_append, select_latest

# 派生 thread_id 的命名空间。换掉它 == 历史线头全部对不上、每条的出处一起重置。
_THREAD_ID_NS = uuid.UUID("2b7f9c14-6d8a-4f31-9e05-7c3a1d4b8e62")

# 她给一条线头挂时刻时写的形状，也是快照渲染回给她的形状。**只认这一种**：
# 年月日 + 时分，北京时间。她那一缝的第一行就是 ``现在 2026-07-25 周六 14:35 CST``
# （:func:`app.infra.cst_time.to_cst_full`），所以日期分量她算得出来；只收时分的话，
# "今天还是明天"就得由代码替她猜。
_DUE_FORMAT = "%Y-%m-%d %H:%M"

# 教她照抄的那个例子。docstring 不能是 f-string，所以工具文案里只能再写一份字面量；
# ``tests/living/test_moment.py`` 钉住两边一致——教的形状和收的形状对不上时，表现是
# 她照着说明写、每次都被顶回来，而说明看上去完全正常。
DUE_EXAMPLE = "[2026-07-25 15:00]"


class LooseEnd(Data):
    """她心上挂着的一件事：什么事、从哪一缝起挂着、了结了没有。

    自然键 ``(lane, persona_id, thread_id)``；``thread_id`` 从 ``what`` 派生
    （:func:`derive_thread_id`），所以重写清单不会堆出重复行。

    ``opened_moment_id`` 是**第一次**把它写进心上的那一缝，之后再怎么重写都不变。
    ``closed_moment_id`` 是关掉它的那一缝。这两列不是日志：验收要对每一条"跨缝
    带过来的事"指出具体是哪两条记录接上的，只给一个比例不算数。

    ``what`` 存的是归一化（去首尾空白、剥掉时刻）之后的原话，不做任何改写——她怎么
    说的就怎么存，下一缝原样还给她看。

    ``due_at`` 是**她自己说的**"这件事该在几点"，可空（绝大多数线头没有时刻）。这一列
    只记时刻，不记"到了没有"：到点与否在渲染时当场比，见模块 docstring。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    thread_id: Annotated[str, Key]
    ver: Annotated[int, Version]  # framework 维护：v1 = 写下，之后 = 关掉 / 复活
    what: str
    due_at: datetime | None = None  # 她说这件事该在几点；None = 没有时刻
    opened_at: datetime
    opened_moment_id: str
    closed_at: datetime | None = None
    closed_moment_id: str | None = None

    # 不声明 Meta.indexes：读取形状是"这个人每条线头的最新一版"，先
    # DISTINCT ON (lane, persona_id, thread_id) ORDER BY ver DESC 再筛 closed_at
    # —— 走的是 migrator 给 Version 类自动建的 ix_key_ver。``due_at`` 上也不加索引：
    # 到期判断在渲染时对这个人已经读出来的那几条当场比，没有"扫全表找到期项"这种查询。

    @field_validator("opened_at", "closed_at", "due_at")
    @classmethod
    def _aware_instant(cls, v: datetime | None) -> datetime | None:
        return _require_aware("opened_at / closed_at / due_at", v)


_TABLE = _table_name(LooseEnd)


def derive_thread_id(what: str) -> str:
    """一件挂心事的 id —— 从它那句话派生，所以重写清单落回同一条。

    只做去首尾空白这一步归一化：再多的归一化（去标点、压空格、大小写）会把两件
    她自己觉得不一样的事悄悄合并成一条。宁可让措辞变了的那句当成新的一条——那至少
    是她真的换了说法。
    """
    return "end:" + uuid.uuid5(_THREAD_ID_NS, what.strip()).hex


def parse_entry(raw: str) -> tuple[str, datetime | None]:
    """把她列的一行拆成「哪件事」和「该在几点」。

    以 ``[`` 开头 = 她在挂时刻，方括号里必须是 ``YYYY-MM-DD HH:MM``（北京时间），
    后面必须还有那件事本身；不以 ``[`` 开头就是一件没有时刻的事，原样返回。

    **写不成的一律当场炸，不静默降级。** 三种写法各有各的静默毒性：

      * 时刻解析不出来（「[明天下午三点] 家属谈话会」）—— 把没解析出来的方括号留在
        ``what`` 里，会让它和「[2026-07-25 15:00] 家属谈话会」变成两条不同的线头
        （身份从 ``what`` 派生），她一改写法旧的就被关掉；悄悄剥掉方括号则是把她刚
        写下的安排丢进垃圾桶而不告诉她。
      * 方括号没闭合 —— 同上，而且更像手滑。
      * **时刻写了、那件事忘了写**（「[2026-07-25 15:00]」）—— 这条最危险：它返回的
        是一个空 ``what``，而空 ``what`` 在 :func:`rewrite_loose_ends` 那边跟"她敲了
        个空行"合流、被跳过。整份重写之下，她这一缝要是只列了这么一条，**她心里挂着
        的会被全部关掉，而工具还回她一句成功**。她漏写的只是那件事本身，代价却是一
        整份清单。

    炸出去的代价只是这一缝她重写一遍——报错原样喂回给她（``@tool_error``），同一轮里
    就能改对。

    代价是她想以 ``[`` 开头写一件**不带时刻**的事（「[未读] 千凪那条」）会被顶回来。
    这里选择让这种情况报错而不是猜她的意思：猜错的那一半是静默的。

    纯空白的一行（她敲了个空行）**不走这条路**，原样返回空 ``what`` + 无时刻，由调用
    方忽略——那种"空"里没有她写下的任何意图，跟上面第三种是两回事。
    """
    line = (raw or "").strip()
    if not line.startswith("["):
        return line, None
    close = line.find("]")
    if close < 0:
        raise ValueError(
            f"这条的时刻没写完：{line!r} 少了一个 ']'。"
            f"该在几点就写成 {DUE_EXAMPLE} 那样放在最前面，没有时刻就直接写那件事。"
        )
    stamp = line[1:close].strip()
    try:
        due_at = datetime.strptime(stamp, _DUE_FORMAT).replace(tzinfo=CST)
    except ValueError:
        raise ValueError(
            f"{stamp!r} 不是一个时刻。该在几点就照着 {DUE_EXAMPLE} 写"
            f"（年月日 + 时分，北京时间，今天几号看你眼前第一行），"
            f"没有时刻就直接写那件事、别写方括号。"
        ) from None
    what = line[close + 1 :].strip()
    if not what:
        raise ValueError(
            f"{line!r} 只写了时刻，没写是什么事 —— 这条挂不上。"
            f"时刻后面把那件事本身写上，像「{DUE_EXAMPLE} 家属谈话会」这样。"
        )
    return what, due_at


def format_entry(what: str, due_at: datetime | None) -> str:
    """拼成她下一缝该照抄的那一行 —— :func:`parse_entry` 的反面。

    快照和 ``keep_in_mind`` 的确认都用它，所以她眼前见到的形状永远就是她该写回来的
    形状。这比在工具描述里讲一遍格式管用得多：她每一缝都在读这个形状。
    """
    if due_at is None:
        return what
    return f"[{due_at.astimezone(CST).strftime(_DUE_FORMAT)}] {what}"


async def list_open_loose_ends(*, lane: str, persona_id: str) -> list[LooseEnd]:
    """她此刻还挂着没了结的事，按挂上的先后升序。

    每条只看版本链上最新的一版：``closed_at`` 一旦被填上，这条就不再出现（除非
    后来又被她列出来、append 了一版把它清空）。
    """
    sql = (
        f"SELECT * FROM ("
        f"  SELECT DISTINCT ON (lane, persona_id, thread_id) * FROM {_TABLE} "
        f"  WHERE lane = :lane AND persona_id = :persona_id "
        f"  ORDER BY lane, persona_id, thread_id, ver DESC"
        f") latest "
        f"WHERE closed_at IS NULL "
        f"ORDER BY opened_at ASC, thread_id ASC"
    )
    async with get_session() as s:
        result = await s.execute(
            text(sql), {"lane": lane, "persona_id": persona_id}
        )
        rows = result.mappings().all()
    return [LooseEnd(**{k: row[k] for k in LooseEnd.model_fields}) for row in rows]


async def rewrite_loose_ends(
    *,
    lane: str,
    persona_id: str,
    moment_id: str,
    now: datetime,
    still_on_my_mind: Sequence[str],
) -> list[LooseEnd]:
    """把她这一缝报的整份清单落下来；返回落完之后还挂着的那些。

    ``still_on_my_mind`` 是**全量**，不是增量：里面有的挂着，里面没有的关掉。空
    清单就是"心里空了"，代码不许替她留着任何一条。漏抄跟真的了结在这里分不出来，
    这是已知限制（见模块 docstring），不加任何补偿逻辑。

    每一条都可以带一个"该在几点"（:func:`parse_entry`）。因为是整份重写，改期就是
    这次列的时刻不一样、撤销时刻就是这次不写方括号，不需要第二只手。

    同一句话在一份清单里出现两次算一件（派生 id 一样，时刻取先出现的那个）。

    **"她敲了个空行"和"她写了一半"是两种空，别合流。** 纯空白的一行直接忽略——里面
    没有她写下的任何意图。而「[2026-07-25 15:00]」这种写了时刻、漏了那件事的，由
    :func:`parse_entry` 当场顶回去，**不会**走到下面那行 ``if not what: continue``。
    合流的代价是一整份清单：整份重写之下，她这一缝要是只列了这么一条，跳过它就等于
    交上来一份空清单，她原有的线头全部走进关闭流程，而她收到的是一句成功。

    **整份先解析完再动库**：一条写坏了，这一份整个不落，她上一份清单原样还在。
    边解析边写的话，坏条目之前的那几条已经改期生效、之后的那几条会被当成"这次没列"
    关掉，而她只看到一句报错。

    返回值让调用方（一缝）直接报得出"缝末还挂着几件"，不用再查一次库。
    """
    wanted: dict[str, tuple[str, datetime | None]] = {}
    for raw in still_on_my_mind:
        what, due_at = parse_entry(raw)
        if not what:
            continue
        wanted.setdefault(derive_thread_id(what), (what, due_at))

    for thread_id, (what, due_at) in wanted.items():
        await _keep_open(
            lane=lane,
            persona_id=persona_id,
            thread_id=thread_id,
            what=what,
            due_at=due_at,
            moment_id=moment_id,
            now=now,
        )

    for end in await list_open_loose_ends(lane=lane, persona_id=persona_id):
        if end.thread_id in wanted:
            continue
        await insert_append(
            LooseEnd(
                **{
                    **end.model_dump(),
                    "closed_at": now,
                    "closed_moment_id": moment_id,
                }
            ),
            expected_current_ver=end.ver,
        )

    return await list_open_loose_ends(lane=lane, persona_id=persona_id)


async def _keep_open(
    *,
    lane: str,
    persona_id: str,
    thread_id: str,
    what: str,
    due_at: datetime | None,
    moment_id: str,
    now: datetime,
) -> None:
    """让这条线头处于"挂着、该在她说的那个时刻"的状态。

    没有就写下；关着的就复活；开着而且时刻没变就什么都不做；开着但这次列的时刻不一样
    就 append 一版改期（撤销时刻同理，那是改成 ``None``）。

    **时刻没变时必须是 no-op**：她每十分钟重列一遍整份清单，一动就 append 的话，一条
    挂一整天的线头能堆出上百版，而中间没有任何真实的状态变化。

    复活 / 改期时 ``opened_at`` / ``opened_moment_id`` **原样保留**：她重新惦记起同一
    件事，说明它从最早那一缝起就一直在她心上；改个时间更不是新开一件事。换一个新出处
    等于把这段延续抹掉。而 ``due_at`` 取**这次列的**，不是把关掉之前那个翻出来——她重新
    列的时候写了什么，什么就是现在的安排。

    走构造函数而不是 ``model_copy(update=...)``：后者在 pydantic v2 上完全跳过
    校验，naive 的时刻会从这条缝里溜进 TIMESTAMPTZ 列。
    """
    keys = {"lane": lane, "persona_id": persona_id, "thread_id": thread_id}
    latest = await select_latest(LooseEnd, keys)
    if latest is None:
        await insert_append(
            LooseEnd(
                **keys,
                ver=0,  # 由 insert_append 按 expected_current_ver 赋成 1
                what=what,
                due_at=due_at,
                opened_at=now,
                opened_moment_id=moment_id,
            ),
            expected_current_ver=0,
        )
        return
    assert isinstance(latest, LooseEnd)
    if latest.closed_at is None and latest.due_at == due_at:
        return
    await insert_append(
        LooseEnd(
            **{
                **latest.model_dump(),
                "due_at": due_at,
                "closed_at": None,
                "closed_moment_id": None,
            }
        ),
        expected_current_ver=latest.ver,
    )
