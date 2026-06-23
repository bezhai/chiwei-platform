"""collect_images 入站图收集的契约。

复现并锁死 prod bad case（chat-turn dbde982e146840cc00610c393fc5820e）的根因：
用户发的未缓存图片走 ``image_client.process_image`` 下载时，必须带上接收消息的
bot_name —— 否则 tool-service 拿不到 X-App-Name、返回 422、图片处理全失败，
赤尾据此否认用户发过图。
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from app.chat.quick_search import QuickSearchResult


def _img_msg(mid: str, img_key: str, *, role: str = "user", chat_type: str = "p2p"):
    """一条带 image item、且未缓存（无 tos_file）的消息 → 走 process_image 下载。"""
    return QuickSearchResult(
        message_id=mid,
        content=json.dumps(
            {
                "v": 2,
                "text": "你看这个",
                "items": [
                    {"type": "text", "value": "你看这个"},
                    {"type": "image", "value": img_key},
                ],
            },
            ensure_ascii=False,
        ),
        user_id="u1",
        create_time=datetime(2026, 6, 23, 20, 0, 0),
        role=role,
        username="原智鸿",
        chat_type=chat_type,
        chat_id="oc_test",
    )


@pytest.mark.asyncio
async def test_collect_images_passes_bot_name_to_process(monkeypatch):
    """复现根因：collect_images 下载未缓存的 user 图时，process_image 必须收到
    传入的 bot_name（接收消息的 bot）。修复前 collect_images 不接受 bot_name、
    process_image 拿到的是 None → X-App-Name 缺失 → tool-service 422。"""
    from app.chat import _context_images as ci

    img_key = "img_v3_0212u_25501191"
    results = [_img_msg("m1", img_key)]

    calls: list[tuple] = []

    async def fake_process(file_key, message_id, bot_name=None):
        calls.append((file_key, message_id, bot_name))
        return {"url": "https://tos/x.png", "file_name": "1.png"}

    monkeypatch.setattr(ci.image_client, "process_image", fake_process)

    url_map, file_map = await ci.collect_images(results, "p2p", bot_name="bot-x")

    assert calls, "未缓存的 user 图必须触发 process_image 下载"
    assert calls[0][2] == "bot-x", (
        f"process_image 必须收到接收消息的 bot_name，实得 {calls[0][2]!r}"
        "（None 即复现 X-App-Name 缺失 → 422 的根因）"
    )
    assert url_map[img_key] == "https://tos/x.png"
    assert file_map[img_key] == "1.png"


@pytest.mark.asyncio
async def test_collect_images_warns_when_bot_name_missing(monkeypatch, caplog):
    """bot_name 缺失（旧 payload / MQ replay）时下载入站图会因 X-App-Name 缺失而
    422。这是上游数据缺失的已知限制——不静默吞掉，记一条明确指向 bot_name 的
    warning 便于排查（codex T1 必改 2）。"""
    import logging

    from app.chat import _context_images as ci

    img_key = "img_v3_0212u_missing"
    results = [_img_msg("m1", img_key)]

    received: list[object] = []

    async def fake_process(file_key, message_id, bot_name=None):
        received.append(bot_name)
        return None  # 缺凭证 → tool-service 422 → None

    monkeypatch.setattr(ci.image_client, "process_image", fake_process)

    with caplog.at_level(logging.WARNING):
        await ci.collect_images(results, "p2p", bot_name="")

    # 不伪造凭证：空 bot_name 原样下传，让下游暴露失败而非静默成功。
    assert received == [""], (
        f"bot_name 缺失时仍应以空串调 process_image，实得 {received!r}"
    )
    # warning 来自未缓存图片下载路径，且为 WARNING 级。
    warn_recs = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "bot_name" in r.message
    ]
    assert warn_recs, (
        "bot_name 缺失时应有一条 WARNING 级、指向 bot_name 的日志，"
        f"实得 warnings: {[r.message for r in caplog.records]!r}"
    )
