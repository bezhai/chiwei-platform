"""POST /api/file-pipeline/process — fire-and-forget file byte caching endpoint.

channel-server posts {message_id, file_key} for an inbound file; the endpoint
downloads (type=file) + stores raw to TOS, returning {url, file_key, file_name}.
Mirrors the image-pipeline process endpoint envelope.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import file_pipeline as file_pipeline_api


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(
        file_pipeline_api,
        "process_file_pipeline",
        AsyncMock(
            return_value={
                "url": "https://tos/files/file_k",
                "file_key": "file_k",
                "file_name": "files/file_k",
            }
        ),
    )
    app = FastAPI()
    app.include_router(file_pipeline_api.router, prefix="/api/file-pipeline")
    return TestClient(app)


def test_process_returns_tos_reference(client):
    resp = client.post(
        "/api/file-pipeline/process",
        json={"message_id": "om_1", "file_key": "file_k"},
        headers={"X-App-Name": "chiwei"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["file_name"] == "files/file_k"
    assert body["data"]["file_key"] == "file_k"
    assert body["data"]["url"].startswith("https://tos/")
