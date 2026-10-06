"""world App 的接线：它开设的收件箱怎么声明，以及这个 App 的进程加载哪个接线模块。"""
from __future__ import annotations

from datetime import timedelta

from app.agent.continuity import MAX_CLEANUP_MINUTES

from .conftest import load_world_wiring


def test_the_world_app_loads_the_world_wiring():
    from app.deployment import APP_WIRING

    assert APP_WIRING["world"] == ("app.world.wiring",)


def test_world_opens_one_inbox_whose_deliveries_wait_for_the_rounds_that_take_them():
    import app.world.wiring as wiring
    from app.messaging.receiving import INBOX_REGISTRY, PROCESSING_RETRY, _lease_ms
    from app.world.answer import answer_question
    from app.world.main_agent import ROUND_TIMEOUT
    from app.world.volume import writer_lock
    from app.world.wake import retry_latest_wake_without_limit, wake_on_start

    load_world_wiring()

    spec = INBOX_REGISTRY["world"]
    assert spec.on_message == wiring.ROUNDS.receive
    assert spec.on_question is answer_question
    assert spec.on_open is wake_on_start
    # 状态里的最新唤醒失败时不限次数重试，永不进死信；别的消息照常有限次重试。
    assert spec.retry_without_limit is retry_latest_wake_without_limit
    # 只在拿着卷的写锁时消费；启动补醒（on_open）也在拿到锁之后才跑。
    assert spec.consume_while is writer_lock
    # 一次投递可能要等前面正在跑的一轮、再等带着它的这一轮：处理时限和租约都要放长到这之上，
    # 远远超过通信机制默认的 15 分钟租约。
    assert spec.processing_timeout == wiring.ROUNDS.delivery_timeout
    assert spec.processing_timeout >= 2 * ROUND_TIMEOUT
    assert ROUND_TIMEOUT > timedelta(milliseconds=PROCESSING_RETRY.lease_ms)
    assert _lease_ms(spec) > spec.processing_timeout.total_seconds() * 1000


def test_the_trim_policy_keeps_the_five_constraints():
    from app.world.main_agent import TRIM_POLICY as p

    assert p.material_minutes > 0 and p.own_minutes > 0
    assert p.own_minutes >= p.material_minutes
    assert 1 <= p.cleanup_minutes <= MAX_CLEANUP_MINUTES
    assert p.hard_cap_tokens > 0 and p.trim_target_tokens > 0
    assert p.trim_target_tokens < p.hard_cap_tokens
