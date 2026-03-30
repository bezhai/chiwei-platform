# tests/unit/test_schedule_pipeline.py
"""Schedule multi-agent pipeline tests"""
import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

# 公共常量
FAKE_DATE = date(2026, 4, 1)
FAKE_PERSONA = "赤尾人设"
FAKE_WEEKLY = "本周计划内容"
FAKE_JOURNAL = "昨天的日志"
FAKE_RECENT = [
    MagicMock(content="3月31日手帐", period_start="2026-03-31"),
    MagicMock(content="3月30日手帐", period_start="2026-03-30"),
    MagicMock(content="3月29日手帐", period_start="2026-03-29"),
]
FAKE_IDEATION = "素材：4月新番列表、樱花季展览"
FAKE_SCHEDULE = "今日手帐内容"


async def _mock_get_plan(plan_type, start, end):
    """按 plan_type 区分：daily 返回 None（不存在），weekly 返回周计划"""
    if plan_type == "weekly":
        return MagicMock(content=FAKE_WEEKLY)
    return None  # daily 不存在，允许生成


@pytest.fixture
def common_patches():
    """公共 mock 上下文"""
    with (
        patch("app.workers.schedule_worker.get_plan_for_period",
              new_callable=AsyncMock, side_effect=_mock_get_plan),
        patch("app.workers.schedule_worker.get_journal",
              new_callable=AsyncMock, return_value=MagicMock(content=FAKE_JOURNAL)),
        patch("app.workers.schedule_worker._get_persona_core",
              return_value=FAKE_PERSONA),
        patch("app.workers.schedule_worker._get_recent_daily_schedules",
              new_callable=AsyncMock, return_value=FAKE_RECENT),
        patch("app.workers.schedule_worker.upsert_schedule",
              new_callable=AsyncMock),
    ):
        yield


@pytest.mark.asyncio
async def test_pipeline_calls_ideation_writer_critic(common_patches):
    """管线按 Ideation → Writer → Critic 顺序执行"""
    call_order = []

    async def fake_ideation(**kw):
        call_order.append("ideation")
        return FAKE_IDEATION

    async def fake_writer(**kw):
        call_order.append("writer")
        return FAKE_SCHEDULE

    async def fake_critic(**kw):
        call_order.append("critic")
        return "PASS"

    with (
        patch("app.workers.schedule_worker._run_ideation",
              new_callable=AsyncMock, side_effect=fake_ideation),
        patch("app.workers.schedule_worker._run_writer",
              new_callable=AsyncMock, side_effect=fake_writer),
        patch("app.workers.schedule_worker._run_critic",
              new_callable=AsyncMock, side_effect=fake_critic),
    ):
        from app.workers.schedule_worker import generate_daily_plan
        result = await generate_daily_plan(FAKE_DATE)

    assert call_order == ["ideation", "writer", "critic"]
    assert result == FAKE_SCHEDULE


@pytest.mark.asyncio
async def test_critic_reject_triggers_rewrite(common_patches):
    """Critic 不通过时 Writer 重写"""
    writer_call_count = 0

    async def fake_writer(**kw):
        nonlocal writer_call_count
        writer_call_count += 1
        return f"手帐v{writer_call_count}"

    critic_responses = iter(["建议：去掉雷同的胶片机意象", "PASS"])

    async def fake_critic(**kw):
        return next(critic_responses)

    with (
        patch("app.workers.schedule_worker._run_ideation",
              new_callable=AsyncMock, return_value=FAKE_IDEATION),
        patch("app.workers.schedule_worker._run_writer",
              new_callable=AsyncMock, side_effect=fake_writer),
        patch("app.workers.schedule_worker._run_critic",
              new_callable=AsyncMock, side_effect=fake_critic),
    ):
        from app.workers.schedule_worker import generate_daily_plan
        result = await generate_daily_plan(FAKE_DATE)

    assert writer_call_count == 2
    assert result == "手帐v2"


@pytest.mark.asyncio
async def test_max_rewrite_attempts(common_patches):
    """3 轮都没 PASS → 用最后一版"""
    writer_call_count = 0

    async def fake_writer(**kw):
        nonlocal writer_call_count
        writer_call_count += 1
        return f"手帐v{writer_call_count}"

    async def fake_critic(**kw):
        return "建议：还是有问题"

    with (
        patch("app.workers.schedule_worker._run_ideation",
              new_callable=AsyncMock, return_value=FAKE_IDEATION),
        patch("app.workers.schedule_worker._run_writer",
              new_callable=AsyncMock, side_effect=fake_writer),
        patch("app.workers.schedule_worker._run_critic",
              new_callable=AsyncMock, side_effect=fake_critic),
    ):
        from app.workers.schedule_worker import generate_daily_plan
        result = await generate_daily_plan(FAKE_DATE)

    assert writer_call_count == 3
    assert result == "手帐v3"


@pytest.mark.asyncio
async def test_ideation_failure_degrades_gracefully(common_patches):
    """Ideation 失败 → Writer 无素材降级"""
    async def failing_ideation(**kw):
        raise Exception("model timeout")

    writer_received_ideation = None

    async def capture_writer(**kw):
        nonlocal writer_received_ideation
        writer_received_ideation = kw.get("ideation_output", "NOT_FOUND")
        return FAKE_SCHEDULE

    async def fake_critic(**kw):
        return "PASS"

    with (
        patch("app.workers.schedule_worker._run_ideation",
              new_callable=AsyncMock, side_effect=failing_ideation),
        patch("app.workers.schedule_worker._run_writer",
              new_callable=AsyncMock, side_effect=capture_writer),
        patch("app.workers.schedule_worker._run_critic",
              new_callable=AsyncMock, side_effect=fake_critic),
    ):
        from app.workers.schedule_worker import generate_daily_plan
        result = await generate_daily_plan(FAKE_DATE)

    assert result == FAKE_SCHEDULE
    assert writer_received_ideation == ""  # 降级为空字符串
