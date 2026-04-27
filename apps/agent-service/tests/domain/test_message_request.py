"""MessageRequest Data: transient 1-field envelope for the MQ entry edge.

The engine's ``_source_loop_mq`` decodes ``{"message_id": X}`` frames from
``Source.mq("vectorize")`` into ``MessageRequest(message_id=X)``; the
@node ``hydrate_message`` then resolves it to a real ``Message``.
"""
from __future__ import annotations

from app.domain.message_request import MessageRequest
from app.runtime.data import key_fields


def test_message_request_key_is_message_id():
    assert key_fields(MessageRequest) == ("message_id",)


def test_message_request_is_transient():
    assert getattr(MessageRequest.Meta, "transient", False) is True


def test_message_request_instance_round_trip():
    req = MessageRequest(message_id="m1")
    assert req.message_id == "m1"
