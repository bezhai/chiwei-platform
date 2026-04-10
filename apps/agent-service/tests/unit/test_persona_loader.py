"""PersonaLoader 单元测试"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

MODULE = "app.services.persona_loader"


def _clear_cache():
    from app.services.persona_loader import _persona_cache

    _persona_cache.clear()


@pytest.fixture(autouse=True)
def _clean_cache():
    """每个测试前后清空 persona 缓存"""
    _clear_cache()
    yield
    _clear_cache()


@pytest.mark.asyncio
async def test_load_persona_returns_context():
    """mock get_bot_persona, verify PersonaContext fields"""
    mock_persona = MagicMock()
    mock_persona.display_name = "赤尾"
    mock_persona.persona_lite = "元气活泼傲娇少女"

    with patch(f"{MODULE}.get_bot_persona", new_callable=AsyncMock, return_value=mock_persona):
        from app.services.persona_loader import load_persona

        pc = await load_persona("akao")

    assert pc.persona_id == "akao"
    assert pc.display_name == "赤尾"
    assert pc.persona_lite == "元气活泼傲娇少女"


@pytest.mark.asyncio
async def test_load_persona_fallback_when_not_found():
    """persona is None -> fallback to persona_id"""
    with patch(f"{MODULE}.get_bot_persona", new_callable=AsyncMock, return_value=None):
        from app.services.persona_loader import load_persona

        pc = await load_persona("unknown-bot")

    assert pc.persona_id == "unknown-bot"
    assert pc.display_name == "unknown-bot"
    assert pc.persona_lite == ""


@pytest.mark.asyncio
async def test_load_persona_caches_result():
    """second call with same persona_id doesn't hit DB"""
    mock_persona = MagicMock()
    mock_persona.display_name = "赤尾"
    mock_persona.persona_lite = "元气活泼傲娇少女"

    with patch(f"{MODULE}.get_bot_persona", new_callable=AsyncMock, return_value=mock_persona) as mock_get:
        from app.services.persona_loader import load_persona

        pc1 = await load_persona("akao")
        pc2 = await load_persona("akao")

    assert mock_get.call_count == 1
    assert pc1 is pc2
    assert pc1.display_name == "赤尾"


@pytest.mark.asyncio
async def test_load_persona_different_ids_separate_cache():
    """different persona_ids get separate cache entries"""
    mock_persona_a = MagicMock()
    mock_persona_a.display_name = "赤尾"
    mock_persona_a.persona_lite = "元气活泼傲娇少女"

    mock_persona_b = MagicMock()
    mock_persona_b.display_name = "蓝猫"
    mock_persona_b.persona_lite = "安静的猫"

    with patch(
        f"{MODULE}.get_bot_persona",
        new_callable=AsyncMock,
        side_effect=[mock_persona_a, mock_persona_b],
    ) as mock_get:
        from app.services.persona_loader import load_persona

        pc_a = await load_persona("akao")
        pc_b = await load_persona("blue-cat")

    assert mock_get.call_count == 2
    assert pc_a.display_name == "赤尾"
    assert pc_b.display_name == "蓝猫"
