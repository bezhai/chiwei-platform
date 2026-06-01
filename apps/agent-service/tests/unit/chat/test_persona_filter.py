import pytest

from app.chat.persona_filter import MessageRouter


@pytest.mark.asyncio
async def test_group_route_consumes_channel_resolved_persona_ids():
    router = MessageRouter()
    out = await router.route(
        chat_id="018f0000-0000-7000-8000-000000000001",
        persona_ids=["p1", "p2", "p1"],
        bot_name="dev",
        is_p2p=False,
    )

    assert out == ["p1", "p2"]


@pytest.mark.asyncio
async def test_group_route_without_persona_ids_does_not_reply():
    router = MessageRouter()
    out = await router.route(
        chat_id="018f0000-0000-7000-8000-000000000001",
        persona_ids=[],
        bot_name="dev",
        is_p2p=False,
    )

    assert out == []
