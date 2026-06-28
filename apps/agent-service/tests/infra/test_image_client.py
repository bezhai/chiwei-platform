"""_ImageClient 鉴权 header 契约。

锁死 inbound 图片 bad case（trace dbde982e146840cc00610c393fc5820e）的直接机制：
``X-App-Name`` 仅在 app_name(bot_name) 非空时才发；为空时整个 header 缺失，
tool-service 直接 422、用户图片下载失败。这正是 collect_images 必须把「接收
消息的 bot」透传到 process_image 的原因——这两条断言钉住了「空就漏 header」
这个根因，配合 collect_images 透传测试，整条下载链才闭环。
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _client_with_stub_router(monkeypatch):
    from app.infra import image as image_mod

    stub_router = MagicMock()
    stub_router.get_headers.return_value = {}
    monkeypatch.setattr(image_mod, "_lane_router", lambda: stub_router)
    return image_mod._ImageClient()


def test_auth_headers_sets_x_app_name_when_bot_present(monkeypatch):
    """有 bot_name → 请求带 X-App-Name，tool-service 才肯下载飞书图。"""
    client = _client_with_stub_router(monkeypatch)
    headers = client._auth_headers(app_name="bot-x")
    assert headers["X-App-Name"] == "bot-x"


def test_auth_headers_omits_x_app_name_when_empty(monkeypatch):
    """空 app_name → 不发 X-App-Name → tool-service 422。这就是 bad case 根因，
    所以下载入站图必须把接收消息的 bot 一路透传进来、不能为空。"""
    client = _client_with_stub_router(monkeypatch)
    headers = client._auth_headers(app_name="")
    assert "X-App-Name" not in headers


@pytest.mark.asyncio
async def test_process_image_forwards_url_in_payload(monkeypatch):
    """QQ 入站图：process_image 收到 url 时，把它放进 /process 的 payload，
    tool-service 据此走 HTTP 下载分支。"""
    from app.infra import image as image_mod

    client = image_mod._ImageClient()
    captured: dict[str, object] = {}

    async def fake_post(path, payload, **kwargs):
        captured["path"] = path
        captured["payload"] = payload
        return {"url": "https://tos/x.jpg", "file_name": "temp/x.jpg"}

    monkeypatch.setattr(client, "_post", fake_post)

    qq_url = "https://qq.cdn.example/a.png"
    await client.process_image(
        file_key=qq_url, message_id="cm_1", bot_name="bot-x", url=qq_url
    )

    assert captured["path"] == "/api/image-pipeline/process"
    assert captured["payload"]["url"] == qq_url
    assert captured["payload"]["file_key"] == qq_url


@pytest.mark.asyncio
async def test_process_image_url_none_for_lark(monkeypatch):
    """飞书路径不传 url → payload 里 url 为 None，飞书 SDK 下载分支不变。"""
    from app.infra import image as image_mod

    client = image_mod._ImageClient()
    captured: dict[str, object] = {}

    async def fake_post(path, payload, **kwargs):
        captured["payload"] = payload
        return {"url": "https://tos/x.jpg", "file_name": "temp/x.jpg"}

    monkeypatch.setattr(client, "_post", fake_post)

    await client.process_image(file_key="img_k", message_id="om_1", bot_name="bot-x")

    assert captured["payload"]["file_key"] == "img_k"
    assert captured["payload"]["url"] is None
