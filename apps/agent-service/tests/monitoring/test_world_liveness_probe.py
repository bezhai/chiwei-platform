"""世界存活探针 — 只告警、绝不叫人、独立于世界心跳（spec Task 3 + 决策 7）.

兜住唯一没被覆盖的单点：world 自己的进程 / 心跳源挂了、长时间没按自己的节奏
醒来，全员无声睡死还查不出来。探针周期跑一次、读 world_state 最新一条声明的
下次醒时刻判存活、异常就发一条飞书告警。

这些测试钉死四块硬约束：

  * **判据**（codex 必改、最关键）：不能用「world_state 多久没更新 + 固定阈值」
    —— world 的保底心跳没到它自己排的 next_wake_at 时会 gate out、那一轮不写
    world_state，而合法长睡可达 1h，固定阈值会把合法长睡误报成死。正确判据是
    读 world 自己最新一条 world_state 声明的 ``next_wake_at`` 做基准：过了它承诺
    的下次醒时刻 + 容错却还没更新，才判异常；``next_wake_at`` 为 None（从没排过）
    时 fallback 到「最长合法 sleep(1h) + 容错」，相对 world 上次写状态时刻量。
  * **只告警**：探针绝不触发任何 notify / deliver_event / 角色唤醒。
  * **独立于世界心跳**：窄入口不 import app.wiring、不启动 runtime source loop。
  * **fail-open**：读库失败 / webhook 失败 / 拼文本失败，任何一步炸都不向上抛。

webhook 用 ``httpx.MockTransport`` 桩掉（项目既有姿势，零新依赖）；判据是纯函数、
直接喂时刻断言；DB 读和「绝不叫醒」用 mock / 静态扫描隔离。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest

import app.monitoring.world_liveness_probe as probe

_CST = timezone(timedelta(hours=8))
_LANE = "coe-t3"
_URL = "https://open.feishu.cn/open-apis/bot/v2/hook/vitality-token"
_ENV = "WORLD_VITALITY_WEBHOOK_URL"

# 探针的两个基准常量：保底心跳周期之上的容错、最长合法 sleep（1h）。
_TOL = timedelta(minutes=10)
_MAX_SLEEP = timedelta(hours=1)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ---------------------------------------------------------------------------
# 判据（核心、codex 必改的那条）：用 world 自己声明的 next_wake_at 做基准
# ---------------------------------------------------------------------------


def test_past_next_wake_at_plus_tolerance_alerts():
    """过了 world 承诺的下次醒时刻 + 容错、还没更新 → 判异常（要告警）。"""
    now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=_CST)
    next_wake_at = _iso(now - _TOL - timedelta(minutes=5))  # 早就该醒、超了容错

    verdict = probe.judge_world_liveness(
        next_wake_at=next_wake_at,
        last_wrote_at=now - timedelta(hours=2),
        now=now,
        tolerance=_TOL,
        max_legal_sleep=_MAX_SLEEP,
    )

    assert verdict.should_alert is True


def test_legal_long_sleep_within_next_wake_at_does_not_alert():
    """world 正常长睡（合法可达 1h）：next_wake_at 还没到 → 不告警。

    这正是固定阈值会误报的场景 —— world 长睡期间 world_state 长时间不更新，
    但它声明的 next_wake_at 还在未来，没按自己的节奏醒来这件事并未发生。
    """
    now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=_CST)
    next_wake_at = _iso(now + timedelta(minutes=40))  # 还要睡 40 分钟、合法

    verdict = probe.judge_world_liveness(
        next_wake_at=next_wake_at,
        last_wrote_at=now - timedelta(minutes=20),
        now=now,
        tolerance=_TOL,
        max_legal_sleep=_MAX_SLEEP,
    )

    assert verdict.should_alert is False


def test_just_past_next_wake_at_within_tolerance_does_not_alert():
    """刚过 next_wake_at 但还在容错窗内 → 不告警（接受几分钟漂移）。"""
    now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=_CST)
    next_wake_at = _iso(now - timedelta(minutes=3))  # 过点 3 分钟 < 容错 10 分钟

    verdict = probe.judge_world_liveness(
        next_wake_at=next_wake_at,
        last_wrote_at=now - timedelta(minutes=33),
        now=now,
        tolerance=_TOL,
        max_legal_sleep=_MAX_SLEEP,
    )

    assert verdict.should_alert is False


def test_none_next_wake_at_falls_back_to_max_sleep_and_alerts():
    """next_wake_at 为 None（从没排过下次醒）→ fallback：上次写状态时刻 +
    最长合法 sleep(1h) + 容错仍没更新 → 告警。"""
    now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=_CST)
    # 上次写状态在 1h20m 前 > 1h + 10m 容错 → 异常
    last_wrote_at = now - _MAX_SLEEP - _TOL - timedelta(minutes=10)

    verdict = probe.judge_world_liveness(
        next_wake_at=None,
        last_wrote_at=last_wrote_at,
        now=now,
        tolerance=_TOL,
        max_legal_sleep=_MAX_SLEEP,
    )

    assert verdict.should_alert is True


def test_none_next_wake_at_within_max_sleep_does_not_alert():
    """next_wake_at 为 None 但上次写状态在 1h+容错 之内 → 不告警（仍可能合法长睡）。"""
    now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=_CST)
    last_wrote_at = now - timedelta(minutes=30)  # 半小时前写过、远没到 1h+容错

    verdict = probe.judge_world_liveness(
        next_wake_at=None,
        last_wrote_at=last_wrote_at,
        now=now,
        tolerance=_TOL,
        max_legal_sleep=_MAX_SLEEP,
    )

    assert verdict.should_alert is False


def test_unparseable_next_wake_at_falls_back_to_max_sleep():
    """next_wake_at 脏 / 无法解析（不该发生）→ 退回 None 的 fallback 判据，
    不因脏 state 把判据卡住。"""
    now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=_CST)
    last_wrote_at = now - _MAX_SLEEP - _TOL - timedelta(minutes=10)

    verdict = probe.judge_world_liveness(
        next_wake_at="not-a-timestamp",
        last_wrote_at=last_wrote_at,
        now=now,
        tolerance=_TOL,
        max_legal_sleep=_MAX_SLEEP,
    )

    assert verdict.should_alert is True


# ---------------------------------------------------------------------------
# 告警通道：独立 env、飞书 webhook 协议、fail-open
# ---------------------------------------------------------------------------


def _ok_handler(_req: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "msg": "success"})


@contextmanager
def _stub_webhook(handler: Callable[[httpx.Request], httpx.Response] = _ok_handler):
    """Patch httpx.AsyncClient（项目既有桩法），记录出站 request 和构造 kwargs。"""
    seen: dict[str, list] = {"requests": [], "client_kwargs": []}
    real_client = httpx.AsyncClient

    def _recording(req: httpx.Request) -> httpx.Response:
        seen["requests"].append(req)
        return handler(req)

    def _factory(*args, **kwargs):
        seen["client_kwargs"].append(dict(kwargs))
        return real_client(transport=httpx.MockTransport(_recording), **kwargs)

    with patch(
        "app.monitoring.world_liveness_probe.httpx.AsyncClient", side_effect=_factory
    ):
        yield seen


def _raising_handler(exc: Exception) -> Callable[[httpx.Request], httpx.Response]:
    def handler(_req: httpx.Request) -> httpx.Response:
        raise exc

    return handler


@pytest.mark.asyncio
async def test_alert_uses_independent_env_and_feishu_protocol(monkeypatch):
    """告警走独立 env ``WORLD_VITALITY_WEBHOOK_URL``，飞书 text 协议、10s 超时。"""
    monkeypatch.setenv(_ENV, _URL)

    with _stub_webhook() as seen:
        await probe.send_world_vitality_alert(lane=_LANE, text="世界可能睡死了")

    (req,) = seen["requests"]
    assert req.method == "POST"
    assert str(req.url) == _URL
    body = json.loads(req.content)
    assert body["msg_type"] == "text"
    assert "世界可能睡死了" in body["content"]["text"]
    assert set(body) == {"msg_type", "content"}
    assert any(kw.get("timeout") == 10.0 for kw in seen["client_kwargs"])


@pytest.mark.asyncio
async def test_alert_missing_env_skips_post(monkeypatch, caplog):
    """env 缺省 / 空白 → 不 POST、info 留痕、不抛。"""
    monkeypatch.delenv(_ENV, raising=False)

    with _stub_webhook() as seen, caplog.at_level(logging.INFO):
        await probe.send_world_vitality_alert(lane=_LANE, text="x")

    assert seen["requests"] == []
    assert any(_ENV in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_alert_webhook_failure_is_fail_open(monkeypatch, caplog):
    """webhook 连接炸 → 不向上抛、error 留痕（fail-open）。"""
    monkeypatch.setenv(_ENV, _URL)

    with (
        _stub_webhook(_raising_handler(httpx.ConnectError("dns down"))),
        caplog.at_level(logging.ERROR),
    ):
        await probe.send_world_vitality_alert(lane=_LANE, text="x")  # 不抛

    assert any(r.levelno == logging.ERROR for r in caplog.records)


# ---------------------------------------------------------------------------
# 编排 run_probe：读 → 判 → （异常才）告警；fail-open；绝不叫醒
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_probe_alerts_when_world_overdue(monkeypatch):
    """异常（过了 next_wake_at + 容错没更新）→ 走告警。"""
    now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=_CST)
    basis = probe.WorldLivenessBasis(
        next_wake_at=_iso(now - _TOL - timedelta(minutes=30)),
        last_wrote_at=now - timedelta(hours=3),
    )

    async def fake_read(*, lane):
        assert lane == _LANE
        return basis

    sent: list[str] = []

    async def fake_alert(*, lane, text):
        sent.append(text)

    monkeypatch.setattr(probe, "_read_latest_world_basis", fake_read)
    monkeypatch.setattr(probe, "send_world_vitality_alert", fake_alert)

    await probe.run_probe(lane=_LANE, now=now, tolerance=_TOL)

    assert len(sent) == 1
    assert _LANE in sent[0]


@pytest.mark.asyncio
async def test_run_probe_silent_when_world_healthy(monkeypatch):
    """正常（next_wake_at 还没到）→ 不告警。"""
    now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=_CST)
    basis = probe.WorldLivenessBasis(
        next_wake_at=_iso(now + timedelta(minutes=40)),
        last_wrote_at=now - timedelta(minutes=20),
    )

    async def fake_read(*, lane):
        return basis

    sent: list[str] = []

    async def fake_alert(*, lane, text):
        sent.append(text)

    monkeypatch.setattr(probe, "_read_latest_world_basis", fake_read)
    monkeypatch.setattr(probe, "send_world_vitality_alert", fake_alert)

    await probe.run_probe(lane=_LANE, now=now, tolerance=_TOL)

    assert sent == []


@pytest.mark.asyncio
async def test_run_probe_no_world_state_does_not_alert(monkeypatch):
    """该 lane 从没有任何 world_state（从没启动过）→ 不告警（不是睡死、是没起过）。"""
    now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=_CST)

    async def fake_read(*, lane):
        return None

    sent: list[str] = []

    async def fake_alert(*, lane, text):
        sent.append(text)

    monkeypatch.setattr(probe, "_read_latest_world_basis", fake_read)
    monkeypatch.setattr(probe, "send_world_vitality_alert", fake_alert)

    await probe.run_probe(lane=_LANE, now=now, tolerance=_TOL)

    assert sent == []


@pytest.mark.asyncio
async def test_run_probe_db_read_failure_is_fail_open(monkeypatch, caplog):
    """读库炸 → 探针自身不向上抛、error 留痕（fail-open）。"""
    now = datetime(2026, 6, 15, 12, 0, 0, tzinfo=_CST)

    async def fake_read(*, lane):
        raise RuntimeError("pg down")

    monkeypatch.setattr(probe, "_read_latest_world_basis", fake_read)

    with caplog.at_level(logging.ERROR):
        await probe.run_probe(lane=_LANE, now=now, tolerance=_TOL)  # 不抛

    assert any(r.levelno == logging.ERROR for r in caplog.records)


# ---------------------------------------------------------------------------
# 硬约束：探针绝不触发任何角色唤醒，且窄入口独立于世界心跳
# ---------------------------------------------------------------------------


def _executable_source(mod) -> str:
    """模块源码去掉注释 + 字符串字面量（含 docstring），只留可执行代码 token。

    探针的 docstring 为了讲清「绝不做什么」会提到 deliver_event / notify 等词，
    那是说明、不是调用。静态约束要钉的是这些**作为实际代码**不出现，所以扫描前
    先用 tokenize 把注释和字符串字面量剔掉，避免误伤解释性文字。
    """
    import inspect
    import io
    import tokenize

    src = inspect.getsource(mod)
    out: list[str] = []
    tokens = tokenize.generate_tokens(io.StringIO(src).readline)
    for tok in tokens:
        if tok.type in (tokenize.COMMENT, tokenize.STRING):
            continue
        out.append(tok.string)
    return " ".join(out)


def test_probe_source_never_wakes_any_character():
    """静态扫描：探针可执行代码里不出现任何唤醒角色 / 启动 runtime 的调用。

    探针只告警，绝不替世界叫人（决策 7：跟被否的 fan-out 心跳本质不同）；且必须
    独立于世界心跳——窄入口不 import app.wiring、不启动 source loop。这条用源码
    扫描钉死，防回归引入唤醒 / wiring 副作用。扫描只看可执行代码（剔掉注释 /
    字符串），docstring 里为讲清「绝不做什么」而提到的词不算违规。
    """
    src = _executable_source(probe)
    # 可执行代码 token 流（注释 / 字符串已剔除），按 token 边界扫描——这些都是单个
    # 标识符，docstring 里的解释性提及不会落进来。
    tokens = set(src.split())
    # 唤醒角色 / 跑世界推演的调用：绝不出现。
    waking = {
        "deliver_event",  # world → 角色信箱投递（会敲醒角色）
        "notify",  # world 唤醒角色工具
        "npc_visit",  # 另一个能戳醒角色的入口
        "emit",  # 任何往图里 emit（含 emit_delayed 等也会带 emit）
        "emit_delayed",  # 排未来 tick
        "world_tick",  # 跑世界推演轮
    }
    # 启动 runtime / source loop 的副作用：绝不出现（独立于世界心跳）。
    runtime_boot = {
        "wiring",  # app.wiring 全套（会启动 source loop）
        "prepare_for_run",  # bootstrap：load graph + start source loops
        "Runtime",  # runtime 引擎（起 source loop / consumer）
    }
    for token in waking | runtime_boot:
        assert token not in tokens, (
            f"探针可执行代码不得出现 {token!r} —— 它只告警、绝不叫人、独立于世界心跳"
        )
