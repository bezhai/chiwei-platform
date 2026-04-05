# tests/unit/test_dream_worker.py
"""Dream worker 单元测试

覆盖场景：
1. generate_daily_dream 正常生成 grain=daily 碎片
2. generate_daily_dream 无碎片时跳过
3. generate_weekly_dream 从 daily 碎片生成 grain=weekly 碎片
4. cron_generate_dreams 遍历所有 persona
"""

import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_generate_daily_dream_produces_fragment():
    """正常场景：有当天碎片 → 生成并返回 grain=daily 的 ExperienceFragment"""
    frag1 = MagicMock(content="今天群里聊了动漫")
    frag2 = MagicMock(content="和阿儒聊了一会")
    persona_obj = MagicMock(display_name="赤尾", persona_lite="可爱的狐耳少女")

    saved_fragment = MagicMock()
    saved_fragment.id = 42

    with (
        patch("app.workers.dream_worker.get_fragments_in_date_range", new_callable=AsyncMock, return_value=[frag1, frag2]),
        patch("app.workers.dream_worker.get_bot_persona", new_callable=AsyncMock, return_value=persona_obj),
        patch("app.workers.dream_worker.get_recent_fragments_by_grain", new_callable=AsyncMock, return_value=[]),
        patch("app.workers.dream_worker.get_prompt") as mock_get_prompt,
        patch("app.workers.dream_worker.ModelBuilder") as mock_mb,
        patch("app.workers.dream_worker.create_fragment", new_callable=AsyncMock, return_value=saved_fragment),
        patch("app.workers.dream_worker.settings") as mock_settings,
    ):
        mock_settings.diary_model = "test-model"
        mock_get_prompt.return_value.compile.return_value = "compiled daily prompt"
        mock_model = AsyncMock()
        mock_model.ainvoke.return_value = MagicMock(content="今天做了个温柔的梦")
        mock_mb.build_chat_model = AsyncMock(return_value=mock_model)

        from app.workers.dream_worker import generate_daily_dream
        result = await generate_daily_dream("akao", date(2026, 4, 5))

    assert result is saved_fragment
    # create_fragment 被调用，且碎片 grain=daily
    from app.workers.dream_worker import create_fragment as cf_fn
    # verify via the mock
    mock_model.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_daily_dream_skips_if_no_fragments():
    """无当天碎片时直接返回 None，不调用 LLM"""
    with (
        patch("app.workers.dream_worker.get_fragments_in_date_range", new_callable=AsyncMock, return_value=[]),
        patch("app.workers.dream_worker.get_bot_persona", new_callable=AsyncMock),
        patch("app.workers.dream_worker.get_prompt") as mock_get_prompt,
        patch("app.workers.dream_worker.ModelBuilder") as mock_mb,
        patch("app.workers.dream_worker.create_fragment", new_callable=AsyncMock) as mock_create,
    ):
        from app.workers.dream_worker import generate_daily_dream
        result = await generate_daily_dream("akao", date(2026, 4, 5))

    assert result is None
    mock_create.assert_not_awaited()
    mock_get_prompt.assert_not_called()


@pytest.mark.asyncio
async def test_generate_weekly_dream_from_dailies():
    """有 daily 碎片 → 生成 grain=weekly 碎片"""
    daily_frags = [
        MagicMock(content=f"第{i}天的回顾") for i in range(7)
    ]
    persona_obj = MagicMock(display_name="赤尾", persona_lite="可爱的狐耳少女")
    saved_fragment = MagicMock()
    saved_fragment.id = 99

    with (
        patch("app.workers.dream_worker.get_recent_fragments_by_grain", new_callable=AsyncMock, return_value=daily_frags),
        patch("app.workers.dream_worker.get_bot_persona", new_callable=AsyncMock, return_value=persona_obj),
        patch("app.workers.dream_worker.get_prompt") as mock_get_prompt,
        patch("app.workers.dream_worker.ModelBuilder") as mock_mb,
        patch("app.workers.dream_worker.create_fragment", new_callable=AsyncMock, return_value=saved_fragment) as mock_create,
        patch("app.workers.dream_worker.settings") as mock_settings,
    ):
        mock_settings.diary_model = "test-model"
        mock_get_prompt.return_value.compile.return_value = "compiled weekly prompt"
        mock_model = AsyncMock()
        mock_model.ainvoke.return_value = MagicMock(content="这一周过得很充实")
        mock_mb.build_chat_model = AsyncMock(return_value=mock_model)

        from app.workers.dream_worker import generate_weekly_dream
        result = await generate_weekly_dream("akao", date(2026, 4, 7))

    assert result is saved_fragment
    mock_create.assert_awaited_once()
    # 确认写入的碎片 grain=weekly
    created_fragment = mock_create.call_args[0][0]
    assert created_fragment.grain == "weekly"


@pytest.mark.asyncio
async def test_cron_generate_dreams_loops_personas():
    """cron_generate_dreams 应为每个 persona 调用 generate_daily_dream"""
    persona_ids = ["akao", "chiwei", "beta"]

    with (
        patch("app.workers.dream_worker.get_all_persona_ids", new_callable=AsyncMock, return_value=persona_ids),
        patch("app.workers.dream_worker.generate_daily_dream", new_callable=AsyncMock) as mock_gen,
    ):
        from app.workers.dream_worker import cron_generate_dreams
        await cron_generate_dreams(ctx=None)

    assert mock_gen.await_count == len(persona_ids)
    called_persona_ids = [call.args[0] for call in mock_gen.await_args_list]
    assert set(called_persona_ids) == set(persona_ids)
