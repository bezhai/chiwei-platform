"""CommonMessageContentSynced 端到端持久化往返（地基刀 0 / Task B）.

``CommonMessageContentSynced`` 早就声明了结构化字段
``messages_json: list[dict]`` / ``image_key_to_file: dict[str, str]`` 且 wire 是
``.durable()``——durable consumer 走 ``insert_idempotent`` 落库（``durable.py``）。
在地基刀补上 JSONB 持久化之前，这个 Data 一旦流经 durable 写库就是 ``DataError``
崩点。这里钉死它真正可持久化：经 ``insert_idempotent`` 写库 → 按自然键读回 →
两个结构化字段（含嵌套）原样还原。
"""

from __future__ import annotations

import pytest

from app.domain.chat_events import CommonMessageContentSynced
from app.runtime.persist import insert_idempotent, select_latest
from tests.runtime.conftest import migrate

_MESSAGES = [
    {"message_id": "m1", "content": "早安", "tos": {"img1": "tos://a"}},
    {"message_id": "m2", "content": "在做饭", "refs": ["img1", "img2"]},
    {},  # 空 dict 也要保真
]
_IMAGE_MAP = {"img1": "tos://a", "img2": "tos://b"}


@pytest.fixture
async def chat_events_db(test_db):
    await migrate(CommonMessageContentSynced, test_db)
    yield test_db


@pytest.mark.integration
async def test_common_message_content_synced_roundtrips_jsonb(chat_events_db):
    """list[dict] / dict[str,str] 字段经 durable 写库（insert_idempotent）能落能读回。"""
    n = await insert_idempotent(
        CommonMessageContentSynced(
            message_id="trigger-1",
            messages_json=_MESSAGES,
            image_key_to_file=_IMAGE_MAP,
        )
    )
    assert n == 1

    got = await select_latest(
        CommonMessageContentSynced, {"message_id": "trigger-1"}
    )
    assert got is not None
    assert got.messages_json == _MESSAGES
    assert got.image_key_to_file == _IMAGE_MAP


@pytest.mark.integration
async def test_common_message_content_synced_empty_structures(chat_events_db):
    """空 list / 空 dict 字段也能往返（边界形态不丢）。"""
    await insert_idempotent(
        CommonMessageContentSynced(
            message_id="trigger-2",
            messages_json=[],
            image_key_to_file={},
        )
    )

    got = await select_latest(
        CommonMessageContentSynced, {"message_id": "trigger-2"}
    )
    assert got is not None
    assert got.messages_json == []
    assert got.image_key_to_file == {}


@pytest.mark.integration
async def test_common_message_content_synced_idempotent_redelivery(chat_events_db):
    """同 message_id 重投（mq redelivery）幂等：第二次 ON CONFLICT DO NOTHING 返回 0。"""
    n1 = await insert_idempotent(
        CommonMessageContentSynced(
            message_id="trigger-3",
            messages_json=_MESSAGES,
            image_key_to_file=_IMAGE_MAP,
        )
    )
    n2 = await insert_idempotent(
        CommonMessageContentSynced(
            message_id="trigger-3",
            messages_json=[{"changed": "second attempt"}],
            image_key_to_file={"x": "y"},
        )
    )

    assert n1 == 1
    assert n2 == 0

    got = await select_latest(
        CommonMessageContentSynced, {"message_id": "trigger-3"}
    )
    assert got is not None
    # 第一次为准、不被覆盖
    assert got.messages_json == _MESSAGES
    assert got.image_key_to_file == _IMAGE_MAP
