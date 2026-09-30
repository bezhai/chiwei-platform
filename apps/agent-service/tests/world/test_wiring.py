"""world App 的接线：它开设的收件箱怎么声明，以及这个 App 的进程加载哪个接线模块。"""
from __future__ import annotations

import importlib
from datetime import timedelta

from app.agent.continuity import MAX_CLEANUP_MINUTES


def _load_world_wiring():
    import app.world.wiring as wiring
    from app.messaging.receiving import clear_inboxes
    from app.runtime.placement import clear_bindings
    from app.runtime.wire import clear_wiring

    clear_wiring()
    clear_bindings()
    clear_inboxes()
    importlib.reload(wiring)


def test_the_world_app_loads_the_world_wiring():
    from app.deployment import APP_WIRING

    assert APP_WIRING["world"] == ("app.world.wiring",)


def test_world_opens_one_inbox_that_takes_one_long_round_at_a_time():
    from app.messaging.receiving import INBOX_REGISTRY, PROCESSING_RETRY, _lease_ms
    from app.world.main_agent import ROUND_TIMEOUT, on_world_message
    from app.world.wake import wake_on_start

    _load_world_wiring()

    spec = INBOX_REGISTRY["world"]
    assert spec.on_message is on_world_message
    assert spec.on_question is None, "第一期 world 不回答问题（应答 agent 是 T4）"
    assert spec.one_at_a_time
    assert spec.on_open is wake_on_start
    assert spec.processing_timeout == ROUND_TIMEOUT
    # 一轮可能比通信机制默认的 15 分钟租约还久：租约要放长到一轮的上限之上。
    assert ROUND_TIMEOUT > timedelta(milliseconds=PROCESSING_RETRY.lease_ms)
    assert _lease_ms(spec) > ROUND_TIMEOUT.total_seconds() * 1000


def test_the_trim_policy_keeps_the_five_constraints():
    from app.world.main_agent import TRIM_POLICY as p

    assert p.material_minutes > 0 and p.own_minutes > 0
    assert p.own_minutes >= p.material_minutes
    assert 1 <= p.cleanup_minutes <= MAX_CLEANUP_MINUTES
    assert p.hard_cap_tokens > 0 and p.trim_target_tokens > 0
    assert p.trim_target_tokens < p.hard_cap_tokens
