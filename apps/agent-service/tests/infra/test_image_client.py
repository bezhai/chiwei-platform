"""_ImageClient 鉴权 header 契约 + 跟 tool-service 那两个端点的 wire 契约。

锁死 inbound 图片 bad case（trace dbde982e146840cc00610c393fc5820e）的直接机制：
``X-App-Name`` 仅在 app_name(bot_name) 非空时才发；为空时整个 header 缺失，
tool-service 直接 422、用户图片下载失败。这正是 collect_images 必须把「接收
消息的 bot」透传到 process_image 的原因——这两条断言钉住了「空就漏 header」
这个根因，配合 collect_images 透传测试，整条下载链才闭环。

下半部分锁死的是**永久句柄那条路**：``/api/image-pipeline/to-tos`` 同时给回
``url`` 和 ``file_name``，只取 ``url`` 就是拿了一个 1.5 小时后就死掉的地址、把
唯一能重新签名的东西扔了（tool-service ``tos_client.get_file_url`` 的
``expires=int(1.5*60*60)``）。这两条走 ``httpx.MockTransport``，报文形状照
tool-service 真的返回的那个信封写，不打网络。
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
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


# ---------------------------------------------------------------------------
# 永久句柄那条路：传上去拿到 file_name，之后拿 file_name 随时换一个能下载的地址
# ---------------------------------------------------------------------------


def _on_the_wire(monkeypatch, handler):
    """让 ``_ImageClient`` 的每一次 HTTP 都走 ``handler``，不出网。

    走 transport 层而不是 stub ``_post``：这两条要验的正是**报文本身**（打哪个
    路径、body 里带什么、tool-service 那个 ``{success, data, message}`` 信封怎么
    拆），stub 掉 ``_post`` 就把要验的东西一起 stub 掉了。
    """
    from app.infra import image as image_mod

    stub_router = MagicMock()
    stub_router.get_headers.return_value = {}
    stub_router.base_url.return_value = "http://tool-service:8000"
    monkeypatch.setattr(image_mod, "_lane_router", lambda: stub_router)

    real_client = httpx.AsyncClient

    def fake_client(*_args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(image_mod.httpx, "AsyncClient", fake_client)
    return image_mod._ImageClient()


@pytest.mark.asyncio
async def test_uploading_hands_back_the_permanent_handle_not_just_a_dying_url(
    monkeypatch,
):
    """``to-tos`` 回的 ``file_name`` 必须原样交到调用方手里。

    只交 ``url`` 的话，1.5 小时之后她做的那张图就再也取不回来了 —— 而且是静默的：
    库里那一行还在，点开是一个过期签名。
    """
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "url": "https://tos.example/temp/tos_ab_12.jpg?sig=short-lived",
                    "file_name": "temp/tos_ab_12.jpg",
                },
                "message": "ok",
            },
        )

    client = _on_the_wire(monkeypatch, handler)
    stored = await client.upload_to_tos("base64", "AAAA")

    assert seen["path"] == "/api/image-pipeline/to-tos"
    assert seen["body"] == {"source_type": "base64", "data": "AAAA"}
    assert stored is not None
    assert stored.file_name == "temp/tos_ab_12.jpg"
    assert stored.url.startswith("https://tos.example/")


@pytest.mark.asyncio
async def test_a_stored_file_name_signs_into_a_fresh_downloadable_address(
    monkeypatch,
):
    """拿库里那个 ``file_name`` 打 ``get-url``，换回一个当下能下载的地址。

    这就是"存句柄不存地址"成立的前提：地址随时可以再签一个，句柄不会死。
    """
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "url": "https://tos.example/temp/tos_ab_12.jpg?sig=fresh",
                    "file_name": "temp/tos_ab_12.jpg",
                },
                "message": "ok",
            },
        )

    client = _on_the_wire(monkeypatch, handler)
    url = await client.get_url("temp/tos_ab_12.jpg")

    assert seen["path"] == "/api/image-pipeline/get-url"
    assert seen["body"] == {"file_name": "temp/tos_ab_12.jpg"}
    assert url == "https://tos.example/temp/tos_ab_12.jpg?sig=fresh"


@pytest.mark.asyncio
async def test_an_upload_that_did_not_land_is_reported_as_nothing(monkeypatch):
    """tool-service 那边失败时交出 ``None`` —— 不回一个假的句柄让她存进记录里。"""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"detail": "tos down"})

    client = _on_the_wire(monkeypatch, handler)
    assert await client.upload_to_tos("base64", "AAAA") is None


@pytest.mark.asyncio
async def test_the_tool_upload_helper_hands_the_permanent_handle_to_its_caller(
    monkeypatch,
):
    """工具那侧的上传辅助交出的也是永久句柄，不是会死的地址。"""
    from app.agent.tools import _common

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "url": "https://tos.example/temp/tos_cd_34.jpg?sig=x",
                    "file_name": "temp/tos_cd_34.jpg",
                },
                "message": "ok",
            },
        )

    _on_the_wire(monkeypatch, handler)
    stored = await _common.upload_image("base64", "AAAA")
    assert stored is not None
    assert stored.file_name == "temp/tos_cd_34.jpg"


@pytest.mark.asyncio
async def test_the_tool_upload_helper_never_hands_back_the_bytes_it_was_given(
    monkeypatch,
):
    """上传没成就是 ``None``。

    旧契约在失败时把**入参原样回递**（``return data, None``），于是一串 base64
    会顺着"句柄"那条路走下去 —— 存进她的记录、当成 TOS 键去重签。失败就说失败。
    """
    from app.agent.tools import _common

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"detail": "upstream down"})

    _on_the_wire(monkeypatch, handler)
    assert await _common.upload_image("base64", "AAAA") is None


# ---------------------------------------------------------------------------
# 签出来的地址背后到底有没有东西
# ---------------------------------------------------------------------------
#
# 签名是纯计算（tool-service 的 ``get-url`` 把名字交给 ``tos_client.pre_signed_url``，
# 那个函数一眼都不看 bucket）。所以"签得出地址"和"图在那儿"是两件事：过期的 ``temp/``
# 对象、入站那一步从没存过的 key、根本就不是对象名的 key，都能签出一个格式完好的地址。
#
# 谁要把地址递给模型谁就得先验一次。Gemini 那侧会自己去下载并 ``raise_for_status``
# （``app.agent.adapters.gemini._fetch_remote_image``），所以一张取不到的图不是"她少
# 看见一张"，是**那一轮整个结束**。


def _fetching(monkeypatch, handler):
    """让 ``image_is_reachable`` 的那次取走 ``handler``，不出网。"""
    from app.infra import image as image_mod

    real_client = httpx.AsyncClient

    def fake_client(*_args, **kwargs):
        kwargs.pop("transport", None)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(image_mod.httpx, "AsyncClient", fake_client)
    return image_mod.image_is_reachable


@pytest.mark.asyncio
async def test_an_address_with_an_image_behind_it_is_reachable(monkeypatch):
    """取得到 = 可以摆到她眼前。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\xff\xd8\xff", headers={"content-type": "image/jpeg"})

    assert await _fetching(monkeypatch, handler)("https://tos.example/a.jpg") is True


@pytest.mark.asyncio
async def test_a_signed_address_with_nothing_behind_it_is_not_reachable(monkeypatch):
    """签得出来、对象不在 —— 这正是签名那一步分辨不了的那一半。"""
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="NoSuchKey")

    assert await _fetching(monkeypatch, handler)("https://tos.example/gone.jpg") is False


@pytest.mark.asyncio
async def test_a_fetch_that_blew_up_is_not_reachable(monkeypatch):
    """取的时候炸了也是取不到 —— 这一步的失败绝不能冒泡。

    它跑在"把地址递给模型"之前，冒泡就等于把一次对象存储抖动升级成她整轮看不了手机。
    """
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("object store unreachable")

    assert await _fetching(monkeypatch, handler)("https://tos.example/a.jpg") is False
