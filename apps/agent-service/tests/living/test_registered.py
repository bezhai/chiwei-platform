"""living 几张表的建表契约：进得了 registry，而且列的形状是钉死的。

两件事都是"错了就静默"：

  * 拉不到 registry 的后果是静默的：``migrate_schema()`` 只看
    ``DATA_REGISTRY``，没进 registry 就不建表，一路跑到真读写才炸。所以这条用子进程
    验——只按线上那样起 agent-service 的插件宿主（:func:`tests.hosting.in_a_fresh_process`），
    不许靠测试自己额外 import 兜底。
  * 列的类型和字段集合**落表之后改不了**：migrator 是 additive-only，加列随时可以，
    删列 / 改类型直接 ``MigrationError`` 崩启动。所以把它们钉在这里——把
    ``occurred_at`` 手滑写回 ``str``、或者顺手加一个"可能有用"的字段，在这条测试就
    该红，而不是等它上了线才发现拆不掉。
"""
from __future__ import annotations

import datetime as dt
import re

import pytest
from pydantic import ValidationError

from app.living.day_page import LivingDayPage
from app.living.nudge import NudgeBegun
from app.living.outgoing import OutgoingMessage, OutgoingResult, OutgoingUpTo
from app.living.persona import PersonaVersion
from app.living.pictures import Picture
from app.living.reading import FilePickedUp, FileRead
from app.living.received import ReceivedMessage, ReceivedRead
from app.living.records import (
    KIND_SPEECH,
    MEDIUM_IN_PERSON,
    Happening,
    Whereabouts,
)
from app.runtime.schema_types import pg_type
from tests.hosting import in_a_fresh_process

_AWARE = dt.datetime(2026, 7, 25, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
_NAIVE = dt.datetime(2026, 7, 25, 10, 0)

# 每个类一份"全字段都合法"的最小载荷，用来把某一个时刻字段换成 naive 的做对照。
_VALID: dict[type, dict] = {
    Happening: {
        "lane": "coe-x",
        "happening_id": "h1",
        "seq": 1,
        "actor": "akao",
        "kind": KIND_SPEECH,
        "medium": MEDIUM_IN_PERSON,
        "content": "早",
        "occurred_at": _AWARE,
        "audience": [],
        "channel_id": None,
    },
    Whereabouts: {
        "lane": "coe-x",
        "persona_id": "akao",
        "moment_id": "m1",
        "seq": 1,
        "place": "家/客厅",
        "doing": "待着",
        "noted_at": _AWARE,
    },
    FileRead: {
        "lane": "coe-x",
        "persona_id": "akao",
        "attachment_id": "msg-1:key-a",
        "ver": 1,
        "title": "斜阳.txt",
        "impression": "读着有点上头。",
        "pages_read": 7,
        "finished": False,
        "read_at": _AWARE,
        "round_id": "r-1",
    },
    FilePickedUp: {
        "lane": "coe-x",
        "round_id": "r-1",
        "persona_id": "akao",
        "attachment_id": "msg-1:key-a",
        "title": "斜阳.txt",
        "tos_file": "files/key-a",
    },
    Picture: {
        "lane": "coe-x",
        "persona_id": "akao",
        "picture_id": "0123456789abcdef0123456789abcdef",
        "file_name": "temp/tos_ab_12.jpg",
        "what": "一只在窗台上晒太阳的猫",
        "made_at": _AWARE,
    },
    LivingDayPage: {
        "lane": "coe-x",
        "persona_id": "akao",
        "day": dt.date(2026, 7, 25),
        "text": "胶片摊了一茶几。",
        "written_at": _AWARE,
        "happenings": 3,
    },
    PersonaVersion: {
        "lane": "coe-x",
        "persona_id": "akao",
        "narrative": "她今年在准备去日本读书。",
        "source": "review",
        "written_at": "2026-07-25T10:00:00+08:00",
        "version": 1,
    },
    ReceivedMessage: {
        "lane": "coe-x",
        "persona_id": "ayana",
        "message_id": "0123456789abcdef0123456789abcdef",
        "sender": "world",
        "body": "窗外下起了雨。",
        "message_time": _AWARE,
        "wakes_recipient": True,
    },
    ReceivedRead: {
        "lane": "coe-x",
        "persona_id": "ayana",
        "message_id": "0123456789abcdef0123456789abcdef",
        "moment_id": "2026-07-25T10:00+08:00",
    },
    NudgeBegun: {
        "lane": "coe-x",
        "persona_id": "ayana",
        "nudged_by": "inbox:0123456789abcdef0123456789abcdef",
    },
    OutgoingMessage: {
        "lane": "coe-x",
        "message_id": "0123456789abcdef0123456789abcdef",
        "persona_id": "akao",
        "seq": 1,
        "sender": "赤尾",
        "recipient": "绫奈",
        "body": "当面对你说：「饭好了。」",
        "message_time": _AWARE,
    },
    OutgoingResult: {
        "lane": "coe-x",
        "message_id": "0123456789abcdef0123456789abcdef",
        "delivered": True,
        "reason": "",
    },
    OutgoingUpTo: {
        "lane": "coe-x",
        "persona_id": "akao",
        "happening_seq": 12,
        "whereabouts_seq": 3,
    },
}

# 列名 -> pg 类型。改这张表 == 改一张已经落地的表的形状，先想清楚怎么迁。
_PINNED: dict[type, dict[str, str]] = {
    Happening: {
        "lane": "TEXT",
        "happening_id": "TEXT",
        "seq": "BIGINT",
        "actor": "TEXT",
        "kind": "TEXT",
        "medium": "TEXT",
        "content": "TEXT",
        "occurred_at": "TIMESTAMPTZ",
        "audience": "JSONB",
        "channel_id": "TEXT",
    },
    Whereabouts: {
        "lane": "TEXT",
        "persona_id": "TEXT",
        "moment_id": "TEXT",
        "seq": "BIGINT",
        "place": "TEXT",
        "doing": "TEXT",
        "noted_at": "TIMESTAMPTZ",
    },
    FileRead: {
        "lane": "TEXT",
        "persona_id": "TEXT",
        "attachment_id": "TEXT",
        "ver": "BIGINT",
        "title": "TEXT",
        "impression": "TEXT",
        "pages_read": "BIGINT",
        "finished": "BOOLEAN",
        "read_at": "TIMESTAMPTZ",
        "round_id": "TEXT",
    },
    # durable 边落地的那条信号行（``(lane, round_id)`` 就是它的去重键）。它没有
    # 时刻列 —— 一程真正的时刻是**读完**那一版写下的 ``FileRead.read_at``，
    # 而不是她按下那一下；在信号上再放一个时刻只会多出一个没人该信的口径。
    FilePickedUp: {
        "lane": "TEXT",
        "round_id": "TEXT",
        "persona_id": "TEXT",
        "attachment_id": "TEXT",
        "title": "TEXT",
        "tos_file": "TEXT",
    },
    # 她做过的一张图。**没有 URL 列**：预签名地址 1.5 小时就死，存下来的后果是静默的
    # （那一行还在，点开是过期签名）。永久的是 ``file_name``，要看的时候现签。
    # 也**没有 channel_id**：她画图那一刻还没有"发给谁"这回事。
    Picture: {
        "lane": "TEXT",
        "persona_id": "TEXT",
        "picture_id": "TEXT",
        "file_name": "TEXT",
        "what": "TEXT",
        "made_at": "TIMESTAMPTZ",
    },
    # 她给一个生活日写下的那一页。``day`` 是 **DATE** 不是 TIMESTAMPTZ：它答的是
    # "哪个生活日"，而生活日的边界是钟点（04:00 到次日 04:00）——存成一个时刻等于
    # 让每个读取方自己再换算一次边界。**没有 written / done 标记列**：这一行存在
    # 本身就是"这天复盘过了"，两个事实中间崩一次就永久对不上。
    LivingDayPage: {
        "lane": "TEXT",
        "persona_id": "TEXT",
        "day": "DATE",
        "text": "TEXT",
        "written_at": "TIMESTAMPTZ",
        "happenings": "BIGINT",
    },
    # 「她是谁」那份正文的一版。**这张表比这里任何一张都更不能动**：prod 上已经有几十
    # 版真实数据，最新一版是她自己上周写的。``written_at`` 是 **TEXT** 不是
    # TIMESTAMPTZ —— 当初这么选是为了避开框架保留列 ``created_at``（那是落库时刻，
    # 语义不同），看着别扭也改不了：additive-only 的 migrator 遇到改类型直接
    # ``MigrationError``、整批迁移回滚、pod crash loop。``version`` 是框架的
    # ``Version`` 列（BIGINT），"最新一版"按它排，不按 ``written_at``。
    PersonaVersion: {
        "lane": "TEXT",
        "persona_id": "TEXT",
        "narrative": "TEXT",
        "source": "TEXT",
        "written_at": "TEXT",
        "version": "BIGINT",
    },
    # 她收到的一条消息，原样：通信机制外层只有 id、发送方、时间，其余都在正文里。**没有
    # 到达时刻列**：那是框架的 ``created_at``。**没有"读过没有"的列**：看过哪几条是另一件
    # 事、另一张表，跟这一轮一起落地。
    ReceivedMessage: {
        "lane": "TEXT",
        "persona_id": "TEXT",
        "message_id": "TEXT",
        "sender": "TEXT",
        "body": "TEXT",
        "message_time": "TIMESTAMPTZ",
        # 发件方说的要不要叫醒她。后加的列：加之前的行是 NULL，读出来当成叫醒。
        "wakes_recipient": "BOOLEAN",
    },
    # 她在哪一轮看过某一条收到的消息。一条一行，不是一个水位：后到的消息可能更早发生，
    # 按任何一种先后开水位都会漏。``moment_id`` 不进键（同一条只算看过一次），也**没有时刻
    # 列**——那一轮的『现在』在 ``LifeMoment.began_at`` 上，按 ``moment_id`` 查得到。
    ReceivedRead: {
        "lane": "TEXT",
        "persona_id": "TEXT",
        "message_id": "TEXT",
        "moment_id": "TEXT",
    },
    # 被什么叫醒的那一轮开始了。**没有"落地了没有"的列**：落没落地看那一轮的
    # ``LifeMoment`` 在不在，落地那次提交就是它的了结。**没有时刻列**：开始的先后是框架的
    # ``created_at``。
    NudgeBegun: {
        "lane": "TEXT",
        "persona_id": "TEXT",
        "nudged_by": "TEXT",
    },
    # 她要发出去的一条消息：id、发给谁、正文、消息上的时间生成那一刻就定死，补发时原样再发。
    # ``seq`` 是她要发的消息里的先后（同一位收件人按它的先后发）。``message_time`` 是消息说的事
    # 发生的那一刻，随消息发出去，对方按它排；补发时给发出那一刻，早话就排到后话后面。**没有
    # "发没发出去"的列**：结果是另一件事、另一张表。生成的先后是框架的 ``created_at``。
    OutgoingMessage: {
        "lane": "TEXT",
        "message_id": "TEXT",
        "persona_id": "TEXT",
        "seq": "BIGINT",
        "sender": "TEXT",
        "recipient": "TEXT",
        "body": "TEXT",
        "message_time": "TIMESTAMPTZ",
    },
    # 一条消息发出去的结果：送到了，或者对方没开收件箱。有这一行就不再发；没确认的那次什么都
    # 不记，下一次再发。
    OutgoingResult: {
        "lane": "TEXT",
        "message_id": "TEXT",
        "delivered": "BOOLEAN",
        "reason": "TEXT",
    },
    # 她的经历讲到哪了：两张表各自的 seq（经历是全泳道一条轴，位置是她自己一条轴）。
    OutgoingUpTo: {
        "lane": "TEXT",
        "persona_id": "TEXT",
        "happening_seq": "BIGINT",
        "whereabouts_seq": "BIGINT",
    },
}


def test_living_data_reaches_the_registry_when_agent_service_starts():
    registered = in_a_fresh_process(
        "agent-service",
        "from app.runtime.data import DATA_REGISTRY;"
        "print(sorted(c.__name__ for c in DATA_REGISTRY))",
        timeout=120,
    )
    for name in (
        "Happening",
        "Whereabouts",
        "FileRead",
        "FilePickedUp",
        "Picture",
        "LivingDayPage",
        "PersonaVersion",
        "ReceivedMessage",
        "ReceivedRead",
        "NudgeBegun",
        "OutgoingMessage",
        "OutgoingResult",
        "OutgoingUpTo",
    ):
        assert f"'{name}'" in registered, (
            f"{name} 没进 DATA_REGISTRY —— migrate_schema 不会建它的表。"
            f"registry: {registered}"
        )


def test_the_package_doc_lists_every_module_in_it():
    """``app/living/__init__.py`` 那份模块清单就是这个包里的全部模块。

    这份清单是读这个包的人第一眼看到的地图。漏一个的后果跟漏一张表一样是**静默**
    的：没有任何东西会因为它不在清单上而报错，于是清单慢慢变成一份只覆盖一半的名
    单，而它看起来仍然像完整的。实际发生过——``anchor`` / ``reading`` / ``web`` /
    ``takeback`` / ``landing`` 五个模块建起来之后，22 个里有 5 个从来没进过清单。

    同 ``tests/unit/data/test_queries_split.py`` 里那条按磁盘文件核对 queries 的检查。
    """
    from pathlib import Path

    import app.living as living_pkg

    on_disk = {
        p.stem
        for p in Path(living_pkg.__file__).parent.glob("*.py")
        if p.stem != "__init__"
    }
    listed = set(re.findall(r"app\.living\.([a-z_]+)", living_pkg.__doc__ or ""))
    assert on_disk - listed == set(), (
        f"这几个模块在包里但不在 __init__.py 的清单上：{sorted(on_disk - listed)}"
    )
    assert listed - on_disk == set(), (
        f"清单上这几个模块已经不存在了：{sorted(listed - on_disk)}"
    )


def test_column_shapes_are_pinned_because_they_can_never_change():
    """字段集合和列类型都钉死——additive-only 意味着现在错了以后改不回来。"""
    for cls, expected in _PINNED.items():
        actual = {
            name: pg_type(fi) for name, fi in cls.model_fields.items()
        }
        assert actual == expected, (
            f"{cls.__name__} 的列形状变了。加列是可以的（记得同步这张表）；"
            f"改类型 / 删列会让已经建好表的 lane 在启动时 MigrationError。"
        )


def test_every_timestamptz_field_rejects_a_naive_datetime():
    """不带 tzinfo 的时刻在写入前就被拒 —— 四个字段一视同仁，不许挡一半。

    落进 TIMESTAMPTZ 的 naive datetime 会被按服务器时区解释，静默偏 8 小时：
    按时刻开窗的读取会整段错位，而且一句报错都没有。跟
    ``medium`` 写成 ``"in-person"`` 是同一类静默毒化，所以挡在同一个位置。

    这条按 ``_PINNED`` 遍历，新加一个 TIMESTAMPTZ 字段却忘了校验就会红。
    """
    checked: list[tuple[str, str]] = []
    for cls, cols in _PINNED.items():
        # 载荷本身必须是合法的，否则下面的 raises 可能是别的原因引起的
        cls(**_VALID[cls])
        for name, typ in cols.items():
            if typ != "TIMESTAMPTZ":
                continue
            checked.append((cls.__name__, name))
            with pytest.raises(ValidationError, match="时区"):
                cls(**{**_VALID[cls], name: _NAIVE})

    assert sorted(checked) == [
        ("FileRead", "read_at"),
        ("Happening", "occurred_at"),
        ("LivingDayPage", "written_at"),
        ("OutgoingMessage", "message_time"),
        ("Picture", "made_at"),
        ("ReceivedMessage", "message_time"),
        ("Whereabouts", "noted_at"),
    ]


# ---------------------------------------------------------------------------
# 加列之后，**已经存在的行**还读不读得出来
# ---------------------------------------------------------------------------
#
# migrator 加列生成的是 ``ALTER TABLE ... ADD COLUMN <t>``——**可空、不带 DB 默认值**。
# pydantic 那个 ``= False`` 只是构造模型时的默认，跟列默认值没有半点关系。所以任何
# 已经有数据的泳道，加完列之后旧行的新列全是 NULL，于是两件事一起发生：
#
#   * 读出来构造模型 → ``bool`` 收到 ``None`` → **ValidationError**，整条链路推不动；
#   * ``WHERE nudged = false`` → NULL 既不等于 false 也不等于 true，**旧行全被过滤掉**。
#
# 这不是这一列的特例，是**每加一个非 Optional 字段都会重演**的部署顺序陷阱。这次
# 是一次性部署、首跑撞不上，但钉在这里，下次加列谁都躲不过去。


@pytest.mark.integration
async def test_a_row_written_before_the_column_existed_still_reads_back(living_db):
    """构造一条 ``nudged`` 为 NULL 的历史行 —— 两条读取路径都要能读出来。"""
    import datetime as dt

    from sqlalchemy import text as _text

    from app.data import session as session_mod
    from app.living.loose_ends import LooseEnd
    from app.living.moment import (
        LifeMoment,
        latest_moment,
        latest_regular_moment,
    )
    from tests.runtime.conftest import migrate

    for cls in (LooseEnd, LifeMoment):
        await migrate(cls, living_db)

    async with session_mod.get_session() as s:
        # 模拟"这一行是加列之前写下的"：显式把新列写成 NULL。
        await s.execute(
            _text(
                "INSERT INTO data_life_moment "
                "(lane, persona_id, moment_id, began_at,"
                " switched, pulled_by, recorded, doing, open_ends,"
                " said, nudged, dedup_hash) "
                "VALUES ('coe-living', 'akao', '2026-07-25T14:00+08:00',"
                " :at, false, '', 0, '看书', 0, '继续', NULL, 'legacy-1')"
            ),
            {"at": dt.datetime(2026, 7, 25, 14, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))},
        )

    got = await latest_moment(lane="coe-living", persona_id="akao")
    assert got is not None, "旧行读不出来 —— 加列之后这个泳道的 life 循环直接推不动了"
    assert got.nudged is False, "NULL 必须当成「不是被提前叫醒」，不能是 None"
    assert got.seq == 0, "NULL 必须当成 0 号，不能是 None"

    regular = await latest_regular_moment(lane="coe-living", persona_id="akao")
    assert regular is not None, (
        "旧行被 `nudged = false` 过滤掉了 —— NULL 既不等于 false 也不等于 true，"
        "于是她的常规节奏判断永远看不到历史，每一拍都当成「从没跑过」"
    )
    assert regular.moment_id == got.moment_id


@pytest.mark.integration
async def test_a_new_moment_outranks_every_row_written_before_seq_existed(living_db):
    """加 ``seq`` 列之后，"最后落地的是哪一轮"不许被旧行钉死。

    这是同一个陷阱的另一面，而且比 ``ValidationError`` 更阴：DESC 排序下 pg 把 NULL
    放**最前**，所以 ``ORDER BY seq DESC`` 会让加列之前的每一行永远压在新一轮前面 ——
    从此每一轮取回的都是那条旧行：离上一次过了多久算错，上下文丢没丢也判不出来，一句
    报错都没有。

    钟点故意造反：新一轮的 ``began_at`` 比两条旧行都早。落地顺序赢的必须是新一轮。
    """
    import datetime as dt

    from sqlalchemy import text as _text

    from app.data import session as session_mod
    from app.living.loose_ends import LooseEnd
    from app.living.moment import LifeMoment, latest_moment
    from app.runtime.persist import insert_idempotent
    from tests.runtime.conftest import migrate

    cst = dt.timezone(dt.timedelta(hours=8))
    for cls in (LooseEnd, LifeMoment):
        await migrate(cls, living_db)

    async with session_mod.get_session() as s:
        for hour, tag in ((14, "old-a"), (15, "old-b")):
            await s.execute(
                _text(
                    "INSERT INTO data_life_moment "
                    "(lane, persona_id, moment_id, seq, began_at,"
                    " switched, pulled_by, recorded, doing,"
                    " open_ends, said, nudged, dedup_hash) "
                    "VALUES ('coe-living', 'akao', :mid, NULL, :at,"
                    " false, '', 0, '看书', 0, '继续', false, :tag)"
                ),
                {
                    "mid": f"2026-07-25T{hour}:00+08:00",
                    "at": dt.datetime(2026, 7, 25, hour, 0, tzinfo=cst),
                    "tag": tag,
                },
            )

    fresh = LifeMoment(
        lane="coe-living",
        persona_id="akao",
        moment_id="nudge:m-1",
        seq=1,
        began_at=dt.datetime(2026, 7, 25, 13, 0, tzinfo=cst),  # 比旧行都早
        switched=False,
        pulled_by="",
        recorded=0,
        doing="看书",
        open_ends=0,
        said="继续",
        context_ver=1,
        nudged=True,
    )
    assert await insert_idempotent(fresh) == 1

    got = await latest_moment(lane="coe-living", persona_id="akao")
    assert got.moment_id == "nudge:m-1", (
        "加列之前的旧行（seq 是 NULL）压在了新一轮前面 —— 从此每一轮取回的都是那条旧行"
    )


@pytest.mark.integration
async def test_a_happening_written_before_channel_id_existed_still_reads_back(
    living_db,
):
    """``channel_id`` 声明成 ``str | None``，所以旧行的 NULL 天然读得出来。

    这条不是重复上面那条：它验的是**同一个陷阱在另一列上没有发生**，因为那一列
    一开始就选了可空。选 ``str = ""`` 的话它会跟 ``nudged`` 一模一样地炸。
    """
    from sqlalchemy import text as _text

    from app.data import session as session_mod
    from app.living.snapshot import recent_own_happenings

    async with session_mod.get_session() as s:
        await s.execute(
            _text(
                "INSERT INTO data_happening "
                "(lane, happening_id, seq, actor, kind, medium, content,"
                " occurred_at, audience, channel_id, dedup_hash) "
                "VALUES ('coe-living', 'legacy-h', 1, 'akao',"
                " 'speech', 'in_person', '早', NOW(), '[\"ayana\"]'::jsonb,"
                " NULL, 'legacy-h1')"
            )
        )

    got = await recent_own_happenings(lane="coe-living", persona_id="akao")
    assert [(h.content, h.channel_id) for h in got] == [("早", None)]
