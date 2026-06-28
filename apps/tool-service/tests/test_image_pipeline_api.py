"""POST /api/image-pipeline/process — optional ``url`` for QQ inbound images.

channel-server (QQ) posts {message_id, file_key, url} for an inbound image whose
``file_key`` is itself a public http url; the endpoint must forward ``url`` to the
service so it downloads over HTTP instead of via the Lark SDK. Lark images (no
``url``) keep posting {message_id, file_key} and must reach the service with
``url=None`` (backward compatible).
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import image_pipeline as image_pipeline_api


@pytest.fixture
def client_and_spy(monkeypatch):
    spy = AsyncMock(
        return_value={"url": "https://tos/temp/x.jpg", "file_name": "temp/x.jpg"}
    )
    monkeypatch.setattr(image_pipeline_api, "process_image_pipeline", spy)
    app = FastAPI()
    app.include_router(image_pipeline_api.router, prefix="/api/image-pipeline")
    return TestClient(app), spy


def test_process_forwards_url_when_present(client_and_spy):
    client, spy = client_and_spy
    qq_url = "https://qq.cdn.example/a.png"

    resp = client.post(
        "/api/image-pipeline/process",
        json={"message_id": "cm_1", "file_key": qq_url, "url": qq_url},
        headers={"X-App-Name": "chiwei"},
    )

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert spy.await_count == 1
    assert spy.await_args.kwargs["url"] == qq_url
    assert spy.await_args.kwargs["file_key"] == qq_url


def test_process_defaults_url_none_for_lark(client_and_spy):
    """Lark path posts no ``url`` → service is called with ``url=None`` so the
    Lark SDK download branch is preserved."""
    client, spy = client_and_spy

    resp = client.post(
        "/api/image-pipeline/process",
        json={"message_id": "om_1", "file_key": "img_k"},
        headers={"X-App-Name": "chiwei"},
    )

    assert resp.status_code == 200
    assert spy.await_args.kwargs["file_key"] == "img_k"
    assert spy.await_args.kwargs["url"] is None
