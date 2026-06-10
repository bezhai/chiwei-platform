"""daily_fetch_node —— 钟与落脚处的编排契约（眼睛 Task 3）.

「fetch」概念已消解：认知层（看什么、怎么看、怎么叙述）整个在
``app/world/eyes.py``，node 只剩钟与落脚处的接线——早退检查 → 调眼睛 → 落
DailyMaterials → 记成本。这些是节点编排测试：mock 眼睛入口（不烧真模型），
验证编排正确性、不验证 LLM 想得对。最致命的几条：

  * **单飞锁**：整段「早退检查 → 眼睛 → 落库」在 single_flight 锁内（key 按
    lane+date）——锁被持有时不跑眼睛、不落库、静默 return（持有方正在干活，
    失败有下一钟点 cron 兜底）。insert_idempotent 只保最终一份数据、保不了只
    烧一次，所以幂等 claim 必须在 LLM 之前、早退检查必须在锁内；
  * **早退**：当天底料已存在直接 return——不调眼睛（不烧 agent token）、不再落库
    （白天每小时打点的 cron 下，按天幂等的闸门之一）；
  * **失败不落库**：眼睛抛错照实穿透、底料 / 成本都不落 → 下一钟点 cron 自动重试；
  * 眼睛带回的叙述落进 DailyMaterials.briefing，date / fetched_at 按 CST；
  * 成本 record_round_cost(actor="world_eyes")、带 collect_usage 累计；
  * 落成本失败 best-effort 吞掉、不把一轮真实的看搞成失败。
"""

from __future__ import annotations

import datetime as _dt
from contextlib import asynccontextmanager

import pytest

import app.fetch.node as fn
from app.fetch.materials import DailyMaterials
from app.runtime.single_flight import SingleFlightConflict


def _patch_single_flight(monkeypatch, *, conflict=False, events=None):
    """把 node 的 single_flight 换成 fake CM（照抄 tests/nodes/test_memory_pipelines.py 风格）。

    conflict=True → 进入即 raise SingleFlightConflict（模拟别的执行正持有锁）；
    events 不为 None 时记录 lock_enter / lock_exit，供锁覆盖范围的顺序断言。
    返回 captured：记录锁 key / ttl。``raising=False``：实现前桩也装得上，让
    red 阶段的失败落在行为断言上而不是 fixture 炸掉。
    """
    captured: dict = {}

    @asynccontextmanager
    async def _fake(key, *, ttl):
        captured["key"] = key
        captured["ttl"] = ttl
        if conflict:
            raise SingleFlightConflict(key)
        if events is not None:
            events.append("lock_enter")
        try:
            yield
        finally:
            if events is not None:
                events.append("lock_exit")

    monkeypatch.setattr(fn, "single_flight", _fake, raising=False)
    return captured


@pytest.fixture(autouse=True)
def _single_flight_passthrough(monkeypatch):
    """默认直通桩：编排测试不连真 Redis；要验锁行为的测试自行重新打桩。"""
    _patch_single_flight(monkeypatch)


def saved_rows(monkeypatch):
    """打桩 save_daily_materials，记录落库 kwargs（不碰真库，编排测试）。"""
    rows: list[dict] = []

    async def fake_save(**kwargs):
        rows.append(kwargs)

    monkeypatch.setattr(fn, "save_daily_materials", fake_save)
    return rows


def cost_calls(monkeypatch):
    """打桩 record_round_cost，记录成本落库调用 kwargs（含 usage）。"""
    calls: list[dict] = []

    async def fake_record(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(fn, "record_round_cost", fake_record)
    return calls


def existing_materials(monkeypatch, materials: DailyMaterials | None):
    """打桩 find_daily_materials：早退检查读到的「当天底料」。"""

    async def fake_find(**kwargs):
        return materials

    monkeypatch.setattr(fn, "find_daily_materials", fake_find)


def _mock_eyes(monkeypatch, *, briefing="今天的当日叙述。", usage=None, raises=None):
    """把眼睛入口 run_world_eyes 换成记录调用 + 返回叙述（或抛错）的桩。

    usage 不为 None 时在桩里调 _accumulate_usage——验证 node 用 collect_usage
    包住整个认知层调用（token 在眼睛里产生、在 node 收口落 PG）。
    """
    calls: list[dict] = []

    async def fake_eyes(**kwargs):
        calls.append(kwargs)
        if usage is not None:
            from app.agent.trace import _accumulate_usage

            _accumulate_usage(usage)
        if raises is not None:
            raise raises
        return briefing

    monkeypatch.setattr(fn, "run_world_eyes", fake_eyes)
    return calls


def _fixed_now(monkeypatch, *, y=2026, mo=6, d=10, h=6, mi=0):
    """钉死 now（CST），让 date / fetched_at 可断言。"""

    class _FixedDateTime(_dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(y, mo, d, h, mi, tzinfo=tz)

    monkeypatch.setattr(fn.cst_time, "datetime", _FixedDateTime)


def _materials(**kwargs) -> DailyMaterials:
    base = {
        "lane": "coe-t3",
        "date": "2026-06-10",
        "briefing": "今天已经看过一遍了。",
        "fetched_at": "2026-06-10T04:05:00+08:00",
    }
    base.update(kwargs)
    return DailyMaterials(**base)


# ---------------------------------------------------------------------------
# 早退：当天底料已存在就不烧 agent（白天每小时打点的同日重试闸门）
# ---------------------------------------------------------------------------


async def test_early_exit_when_todays_materials_exist(monkeypatch):
    """当天底料已存在：不调眼睛、不再落库、不记成本——直接 return。"""
    _fixed_now(monkeypatch)
    existing_materials(monkeypatch, _materials())
    saved = saved_rows(monkeypatch)
    costs = cost_calls(monkeypatch)
    eyes = _mock_eyes(monkeypatch)

    await fn.daily_fetch_node(fn.DailyMaterialsFetch(lane="coe-t3"))

    assert eyes == [], "当天已有底料：不许再调眼睛烧 token"
    assert saved == []
    assert costs == []


# ---------------------------------------------------------------------------
# 正常路径：调眼睛 → 落叙述 → 记成本
# ---------------------------------------------------------------------------


async def test_calls_eyes_with_lane_and_date(monkeypatch):
    """node 把 lane + CST「今天」交给眼睛入口（认知层只认这两样）。"""
    _fixed_now(monkeypatch)
    existing_materials(monkeypatch, None)
    saved_rows(monkeypatch)
    cost_calls(monkeypatch)
    eyes = _mock_eyes(monkeypatch)

    await fn.daily_fetch_node(fn.DailyMaterialsFetch(lane="coe-t3"))

    assert eyes == [{"lane": "coe-t3", "date": "2026-06-10"}]


async def test_persists_eyes_briefing(monkeypatch):
    """眼睛带回的当日叙述落进 DailyMaterials.briefing。"""
    existing_materials(monkeypatch, None)
    saved = saved_rows(monkeypatch)
    cost_calls(monkeypatch)
    _mock_eyes(monkeypatch, briefing="带世界关切的当日叙述")

    await fn.daily_fetch_node(fn.DailyMaterialsFetch(lane="coe-t3"))

    assert len(saved) == 1
    row = saved[0]
    assert row["lane"] == "coe-t3"
    assert row["briefing"] == "带世界关切的当日叙述"


async def test_date_and_fetched_at_are_cst(monkeypatch):
    """date 按 CST「今天」算、fetched_at 是 CST aware ISO。"""
    _fixed_now(monkeypatch, h=6)
    existing_materials(monkeypatch, None)
    saved = saved_rows(monkeypatch)
    cost_calls(monkeypatch)
    _mock_eyes(monkeypatch)

    await fn.daily_fetch_node(fn.DailyMaterialsFetch(lane="coe-t3"))

    row = saved[0]
    assert row["date"] == "2026-06-10"
    assert "+08:00" in row["fetched_at"], "fetched_at 必须是 CST aware ISO"


# ---------------------------------------------------------------------------
# 单飞锁：幂等 claim 必须在 LLM 之前——insert_idempotent 只保最终一份数据、
# 保不了只烧一次眼睛（MQ 重复投递 / 双进程同挂 dataflow 的并发钟点）
# ---------------------------------------------------------------------------


async def test_lock_held_skips_eyes_silently(monkeypatch):
    """锁被持有：不调眼睛、不落库、不记成本、不抛——静默 return。

    持有方正在干活；若持有方失败，下一钟点 cron 自然重试。SingleFlightConflict
    绝不能穿透 node（穿透会让 dataflow 把冗余唤醒当失败处理）。
    """
    _fixed_now(monkeypatch)
    existing_materials(monkeypatch, None)
    saved = saved_rows(monkeypatch)
    costs = cost_calls(monkeypatch)
    eyes = _mock_eyes(monkeypatch)
    _patch_single_flight(monkeypatch, conflict=True)

    # 不该抛——锁冲突 = 别的执行正在烧这一天，静默让位。
    await fn.daily_fetch_node(fn.DailyMaterialsFetch(lane="coe-t3"))

    assert eyes == [], "锁被持有：绝不再烧一遍眼睛 agent"
    assert saved == []
    assert costs == []


async def test_normal_path_runs_inside_lock(monkeypatch):
    """正常路径：锁 key 按 lane+date、ttl 用常量；早退检查 / 眼睛 / 落库全在锁内。"""
    _fixed_now(monkeypatch)
    events: list[str] = []
    captured = _patch_single_flight(monkeypatch, events=events)

    async def fake_find(**kwargs):
        events.append("find")
        return None

    async def fake_eyes(**kwargs):
        events.append("eyes")
        return "锁内带回的当日叙述"

    async def fake_save(**kwargs):
        events.append("save")

    monkeypatch.setattr(fn, "find_daily_materials", fake_find)
    monkeypatch.setattr(fn, "run_world_eyes", fake_eyes)
    monkeypatch.setattr(fn, "save_daily_materials", fake_save)
    cost_calls(monkeypatch)

    await fn.daily_fetch_node(fn.DailyMaterialsFetch(lane="coe-t3"))

    assert captured["key"] == "world_eyes:coe-t3:2026-06-10"
    assert captured["ttl"] == fn.WORLD_EYES_LOCK_TTL_SECONDS
    assert events == ["lock_enter", "find", "eyes", "save", "lock_exit"], (
        "早退检查必须在锁内（锁外检查仍有并发窗口），眼睛 / 落库也全程在锁内"
    )


async def test_early_exit_inside_lock_skips_eyes(monkeypatch):
    """早退仍然生效且发生在锁内：锁内读到当天底料 → 不烧眼睛、正常释放锁。"""
    _fixed_now(monkeypatch)
    events: list[str] = []
    _patch_single_flight(monkeypatch, events=events)

    async def fake_find(**kwargs):
        events.append("find")
        return _materials()

    monkeypatch.setattr(fn, "find_daily_materials", fake_find)
    saved = saved_rows(monkeypatch)
    costs = cost_calls(monkeypatch)
    eyes = _mock_eyes(monkeypatch)

    await fn.daily_fetch_node(fn.DailyMaterialsFetch(lane="coe-t3"))

    assert events == ["lock_enter", "find", "lock_exit"]
    assert eyes == []
    assert saved == []
    assert costs == []


# ---------------------------------------------------------------------------
# 失败语义：眼睛抛错照实穿透、本钟点啥也不落（下一钟点 cron 自动重试）
# ---------------------------------------------------------------------------


async def test_eyes_failure_propagates_and_nothing_persisted(monkeypatch):
    """眼睛失败：异常穿透 node、底料 / 成本都不落——同日重试交给下一钟点。"""
    _fixed_now(monkeypatch)
    existing_materials(monkeypatch, None)
    saved = saved_rows(monkeypatch)
    costs = cost_calls(monkeypatch)
    _mock_eyes(monkeypatch, raises=RuntimeError("model down"))

    with pytest.raises(RuntimeError, match="model down"):
        await fn.daily_fetch_node(fn.DailyMaterialsFetch(lane="coe-t3"))

    assert saved == [], "眼睛失败本钟点绝不落库（落了会让早退吞掉后续重试）"
    assert costs == []


# ---------------------------------------------------------------------------
# 成本观测：record_round_cost(actor="world_eyes") + collect_usage 累计
# ---------------------------------------------------------------------------


async def test_records_token_cost_with_world_eyes_actor(monkeypatch):
    """一轮收口把眼睛产生的累计 token 落 PG，actor = "world_eyes"、round_id = 当天日期。"""
    _fixed_now(monkeypatch)
    existing_materials(monkeypatch, None)
    saved_rows(monkeypatch)
    cost_recorded = cost_calls(monkeypatch)
    _mock_eyes(
        monkeypatch,
        usage={"input": 300, "output": 60, "total": 360, "cache_read_input_tokens": 40},
    )

    await fn.daily_fetch_node(fn.DailyMaterialsFetch(lane="coe-t3"))

    assert len(cost_recorded) == 1
    rec = cost_recorded[0]
    assert rec["lane"] == "coe-t3"
    assert rec["actor"] == "world_eyes", "眼睛的成本 actor 必须是 'world_eyes'"
    assert rec["usage"]["input"] == 300
    assert rec["usage"]["total"] == 360
    assert rec["usage"]["calls"] == 1
    assert rec["round_id"] == "2026-06-10", "round_id 用当天日期（按天唯一）"
    assert rec["observed_at"]


async def test_cost_record_failure_does_not_fail_round(monkeypatch):
    """落成本失败 best-effort 吞掉，不把一轮真实的看搞成失败（底料照常落库）。

    打桩真实 record_thinking_tokens 抛错（走 record_round_cost 里真正的 swallow
    路径），而非打桩 node 的 record_round_cost —— 这样测的是真实吞错语义。
    """
    import app.domain.thinking_cost as tc

    existing_materials(monkeypatch, None)
    saved = saved_rows(monkeypatch)
    _mock_eyes(monkeypatch, usage={"input": 1, "output": 1, "total": 2})

    async def boom_record(**kwargs):
        raise RuntimeError("PG down recording cost")

    monkeypatch.setattr(tc, "record_thinking_tokens", boom_record)

    # 不该抛——成本观测是旁路。
    await fn.daily_fetch_node(fn.DailyMaterialsFetch(lane="coe-t3"))

    # 底料照常落库（成本失败不影响真实的看收口）。
    assert len(saved) == 1
    assert saved[0]["briefing"]
