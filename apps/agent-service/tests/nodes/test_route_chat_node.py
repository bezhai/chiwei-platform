"""route_chat_node 单元测试（Task 4-6 累积）。"""
import pytest

from app.domain.chat_dataflow import ChatTrigger


@pytest.mark.asyncio
async def test_route_chat_node_raises_on_missing_message_id():
    """缺 message_id -> raise，不静默 fan-out 空 ChatRequest。"""
    from app.nodes.chat_node import route_chat_node

    t = ChatTrigger()  # 全部默认值，message_id=None
    with pytest.raises((ValueError, AssertionError)):
        await route_chat_node(t)
