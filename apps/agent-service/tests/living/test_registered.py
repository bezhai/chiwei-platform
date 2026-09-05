"""living 几张表的建表契约：进得了 registry，而且列的形状是钉死的。

两件事都是"错了就静默"：

  * 拉不到 registry 的后果是静默的：``Runtime.migrate_schema()`` 只看
    ``DATA_REGISTRY``，没进 registry 就不建表，一路跑到真读写才炸。所以这条用子进程
    验——只 import ``app.wiring``（跟线上启动同一条链），不许靠测试自己额外 import
    兜底。
  * 列的类型和字段集合**落表之后改不了**：migrator 是 additive-only，加列随时可以，
    删列 / 改类型直接 ``MigrationError`` 崩启动。所以把它们钉在这里——把
    ``occurred_at`` 手滑写回 ``str``、或者顺手加一个"可能有用"的字段，在这条测试就
    该红，而不是等它上了线才发现拆不掉。
"""
from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys

import pytest
from pydantic import ValidationError

from app.living.pictures import Picture
from app.living.reading import FilePickedUp, FileRead
from app.living.records import (
    KIND_SPEECH,
    MEDIUM_IN_PERSON,
    Happening,
    Upcoming,
    Whereabouts,
)
from app.runtime.schema_types import pg_type

_AWARE = dt.datetime(2026, 7, 25, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
_NAIVE = dt.datetime(2026, 7, 25, 10, 0)

# 每个类一份"全字段都合法"的最小载荷，用来把某一个时刻字段换成 naive 的做对照。
_VALID: dict[type, dict] = {
    Happening: {
        "lane": "coe-x",
        "happening_id": "h1",
        "seq": 1,
        "actor": "akao",
        "place": "家/客厅",
        "kind": KIND_SPEECH,
        "medium": MEDIUM_IN_PERSON,
        "content": "早",
        "occurred_at": _AWARE,
        "audience": [],
        "who_was_where": {},
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
    Upcoming: {
        "lane": "coe-x",
        "item_id": "i1",
        "ver": 1,
        "what": "天亮",
        "due_at": _AWARE,
        "place": None,
        "consumed_at": _AWARE,
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
}

# 列名 -> pg 类型。改这张表 == 改一张已经落地的表的形状，先想清楚怎么迁。
_PINNED: dict[type, dict[str, str]] = {
    Happening: {
        "lane": "TEXT",
        "happening_id": "TEXT",
        "seq": "BIGINT",
        "actor": "TEXT",
        "place": "TEXT",
        "kind": "TEXT",
        "medium": "TEXT",
        "content": "TEXT",
        "occurred_at": "TIMESTAMPTZ",
        "audience": "JSONB",
        "who_was_where": "JSONB",
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
    Upcoming: {
        "lane": "TEXT",
        "item_id": "TEXT",
        "ver": "BIGINT",
        "what": "TEXT",
        "due_at": "TIMESTAMPTZ",
        "place": "TEXT",
        "consumed_at": "TIMESTAMPTZ",
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
}


def test_living_data_reaches_the_registry_via_app_wiring():
    code = (
        "import app.wiring;"
        "from app.runtime.data import DATA_REGISTRY;"
        "print(sorted(c.__name__ for c in DATA_REGISTRY))"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    registered = proc.stdout
    for name in (
        "Happening",
        "Whereabouts",
        "Upcoming",
        "FileRead",
        "FilePickedUp",
        "Picture",
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
    ``due_at`` 是整个日历的基准，偏了就是日历全错、而且一句报错都没有。跟
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
        ("Picture", "made_at"),
        ("Upcoming", "consumed_at"),
        ("Upcoming", "due_at"),
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
                "(lane, persona_id, moment_id, began_at, after_seq, next_seq,"
                " perceived, switched, pulled_by, recorded, doing, open_ends,"
                " said, nudged, dedup_hash) "
                "VALUES ('coe-living', 'akao', '2026-07-25T14:00+08:00',"
                " :at, 0, 3, 1, false, '', 0, '看书', 0, '继续', NULL, 'legacy-1')"
            ),
            {"at": dt.datetime(2026, 7, 25, 14, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))},
        )

    got = await latest_moment(lane="coe-living", persona_id="akao")
    assert got is not None, "旧行读不出来 —— 加列之后这个泳道的 life 循环直接推不动了"
    assert got.nudged is False, "NULL 必须当成「不是提前缝」，不能是 None"
    assert got.seq == 0, "NULL 必须当成 0 号，不能是 None"

    regular = await latest_regular_moment(lane="coe-living", persona_id="akao")
    assert regular is not None, (
        "旧行被 `nudged = false` 过滤掉了 —— NULL 既不等于 false 也不等于 true，"
        "于是她的常规节奏判断永远看不到历史，每一拍都当成「从没跑过」"
    )
    assert regular.moment_id == got.moment_id


@pytest.mark.integration
async def test_a_new_moment_outranks_every_row_written_before_seq_existed(living_db):
    """加 ``seq`` 列之后，"读到哪了"不许被旧行钉死。

    这是同一个陷阱的另一面，而且比 ``ValidationError`` 更阴：DESC 排序下 pg 把 NULL
    放**最前**，所以 ``ORDER BY seq DESC`` 会让加列之前的每一行永远压在新缝前面 ——
    游标从此取回那条旧行、再也推不动，她每一缝把同一批动静重新感知一遍，一句报错
    都没有。

    钟点故意造反：新缝的 ``began_at`` 比两条旧行都早。落地顺序赢的必须是新缝。
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
                    "(lane, persona_id, moment_id, seq, began_at, after_seq,"
                    " next_seq, perceived, switched, pulled_by, recorded, doing,"
                    " open_ends, said, nudged, dedup_hash) "
                    "VALUES ('coe-living', 'akao', :mid, NULL, :at, 0, 3, 1,"
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
        after_seq=3,
        next_seq=9,
        perceived=2,
        switched=False,
        pulled_by="",
        recorded=0,
        doing="看书",
        open_ends=0,
        said="继续",
        nudged=True,
    )
    assert await insert_idempotent(fresh) == 1

    got = await latest_moment(lane="coe-living", persona_id="akao")
    assert got.moment_id == "nudge:m-1", (
        "加列之前的旧行（seq 是 NULL）压在了新缝前面 —— 游标从此钉死，"
        "她每一缝把同一批动静重新感知一遍"
    )
    assert got.next_seq == 9


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
    from app.living.happening import read_perceived_by

    async with session_mod.get_session() as s:
        await s.execute(
            _text(
                "INSERT INTO data_happening "
                "(lane, happening_id, seq, actor, place, kind, medium, content,"
                " occurred_at, audience, who_was_where, channel_id, dedup_hash) "
                "VALUES ('coe-living', 'legacy-h', 1, 'ayana', '家/客厅',"
                " 'speech', 'in_person', '早', NOW(), '[\"akao\"]'::jsonb,"
                " '{}'::jsonb, NULL, 'legacy-h1')"
            )
        )

    window = await read_perceived_by(lane="coe-living", persona_id="akao")
    assert [p.content for p in window.items] == ["早"]
