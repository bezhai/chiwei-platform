"""世界的钟：两条时间源真的挂上了，而且挂法不会在启动时炸掉 Pod。

三件全是「错了就静默 / 错了就起不来」的：

  * **wire 不存在 = 世界不走。** 日历不到期、world 一轮不跑，业务代码全绿。所以
    用子进程只 import ``app.wiring``（跟线上启动同一条链）去看注册表。
  * **挂时间源的 Data 必须能只用一个 ``ts`` 构造。** 框架的源循环固定按
    ``data_type(ts=<iso>)`` 造 payload；多一个必填字段就是每一拍 ValidationError
    **直接杀 Pod**，而不是少跑一轮。
  * **``WorldRound`` 的列形状落表之后改不了。** migrator additive-only，删列 /
    改类型会 ``MigrationError`` 崩启动。
"""
from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys

import pytest
from pydantic import ValidationError

from app.living.clock import CalendarTick, WorldRoundTick, living_lane
from app.living.world import WorldRound
from app.runtime.schema_types import pg_type

_AWARE = dt.datetime(2026, 7, 25, 10, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))


def _in_a_fresh_process(expr: str, *, lane: str | None = "coe-living") -> str:
    """在一个干净进程里只 import ``app.wiring``（跟线上启动同一条链）跑一句表达式。

    ``lane`` 决定这个进程"部署在哪"——living 的三条钟只在 ``coe-*`` 上注册
    （见 :mod:`app.wiring.living`），所以泳道是这几条用例的输入，不能不给。
    """
    env = dict(os.environ)
    if lane is None:
        env.pop("LANE", None)
    else:
        env["LANE"] = lane
    proc = subprocess.run(
        [sys.executable, "-c", f"import app.wiring;{expr}"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


_TICK_WIRES = (
    "from app.runtime.wire import WIRING_REGISTRY;"
    "print([(s.data_type.__name__,"
    " sorted(c.__name__ for c in s.consumers),"
    " [(x.kind, x.params) for x in s.sources])"
    " for s in WIRING_REGISTRY"
    " if s.data_type.__name__ in"
    " ('CalendarTick', 'WorldRoundTick', 'LifeMomentTick')])"
)


def test_the_world_round_ledger_reaches_the_registry_via_app_wiring():
    out = _in_a_fresh_process(
        "from app.runtime.data import DATA_REGISTRY;"
        "print(sorted(c.__name__ for c in DATA_REGISTRY))"
    )
    assert "'WorldRound'" in out, (
        f"WorldRound 没进 DATA_REGISTRY —— migrate_schema 不会建它的表。registry: {out}"
    )


def test_both_clocks_are_wired_to_an_interval_source_on_a_coe_lane():
    out = _in_a_fresh_process(_TICK_WIRES, lane="coe-living")
    assert "CalendarTick" in out and "calendar_tick" in out, out
    assert "WorldRoundTick" in out and "world_round_tick" in out, out
    assert out.count("'interval'") == 3, (
        f"coe 泳道上三条钟该全挂上时间源 —— 世界不会自己走。拿到：{out}"
    )


@pytest.mark.parametrize(
    "lane", [None, "prod", "ppe-something", "blue", "coe-somethingelse"]
)
def test_the_living_clocks_do_not_exist_outside_the_experiment_lane(lane):
    """**这条是实验的边界，不是行为开关。**

    runtime 在 prod 上默认**启用**时间源（``lane_policy.time_sources_enabled_by_
    default``），而 ``living_lane()`` 又回落到 ``prod``。所以这三条 wire 一旦无条件
    注册，这个分支合进 main、prod 一发版，world 加三个 life 就在 prod 库上直接开跑：
    建表、写数据、烧模型钱 —— 正面违反 spec 的 Non-goal「不替换 prod」。

    表还是会被 ``migrate_schema()`` 在所有 lane 建出来，那个拦不住也无害（空表）；
    拦得住的是**有没有人往里写**。
    """
    out = _in_a_fresh_process(_TICK_WIRES, lane=lane)
    assert out.strip() == "[]", (
        f"泳道 {lane!r} 上挂着 living 的时间源 —— prod 一发版她们就自己跑起来了。"
        f"拿到：{out}"
    )


def test_the_living_data_still_reaches_the_registry_outside_the_experiment_lane():
    """只拦钟，不拦建表：Data 注册跟泳道无关，别把两件事绑一起。"""
    out = _in_a_fresh_process(
        "from app.runtime.data import DATA_REGISTRY;"
        "print(sorted(c.__name__ for c in DATA_REGISTRY))",
        lane="prod",
    )
    for name in ("Happening", "Whereabouts", "Upcoming", "WorldRound"):
        assert f"'{name}'" in out, out


@pytest.mark.parametrize("cls", [CalendarTick, WorldRoundTick])
def test_a_time_source_tick_is_constructible_from_ts_alone(cls):
    """框架源循环只喂一个 ``ts``；多一个必填字段 = 每一拍 ValidationError 杀 Pod。"""
    assert cls(ts="2026-07-25T10:00:00+08:00").ts == "2026-07-25T10:00:00+08:00"


@pytest.mark.parametrize("cls", [CalendarTick, WorldRoundTick])
def test_a_tick_is_transient(cls):
    """钟只是「该看一眼了」，世界的内容在 Upcoming / Happening 里，不建表。"""
    assert cls.Meta.transient is True


def test_the_world_round_column_shapes_are_pinned():
    actual = {name: pg_type(fi) for name, fi in WorldRound.model_fields.items()}
    assert actual == {
        "lane": "TEXT",
        "round_id": "TEXT",
        "ran_at": "TIMESTAMPTZ",
        "produced": "BIGINT",
        "said": "TEXT",
    }, (
        "WorldRound 的列形状变了。加列是可以的；改类型 / 删列会让已经建好表的 lane "
        "在启动时 MigrationError。"
    )


def test_a_round_refuses_a_naive_ran_at():
    """naive 落进 TIMESTAMPTZ 会被按服务器时区解释、静默偏几小时 —— 间隔判断全错。"""
    WorldRound(lane="coe-x", round_id="r1", ran_at=_AWARE, produced=0, said="没有")
    with pytest.raises(ValidationError, match="时区"):
        WorldRound(
            lane="coe-x",
            round_id="r1",
            ran_at=dt.datetime(2026, 7, 25, 10, 0),
            produced=0,
            said="没有",
        )


def test_prod_is_the_lane_when_nothing_is_deployed(monkeypatch):
    """lane 进 Key 是硬约束：拿不到泳道时必须是 ``prod``，不能是空串。

    空串会开一条谁也读不到的影子轴：写进去的行没人查得到，而她那边一片安静。
    """
    monkeypatch.delenv("LANE", raising=False)
    assert living_lane() == "prod"


def test_the_deployment_lane_is_used_as_is(monkeypatch):
    monkeypatch.setenv("LANE", "coe-living")
    assert living_lane() == "coe-living"
