"""Phase 7b Gap 12: RabbitMQ management HTTP API client."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from app.runtime.rabbitmq_management import RabbitMQManagementClient


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("RABBITMQ_HOST", "rabbit-host")
    monkeypatch.setenv("RABBITMQ_USER", "user")
    monkeypatch.setenv("RABBITMQ_PASSWORD", "secret")
    monkeypatch.setenv("RABBITMQ_MANAGEMENT_PORT", "15672")
    monkeypatch.setenv("RABBITMQ_VHOST", "/")


@pytest.mark.asyncio
async def test_peek_messages_calls_get_endpoint(env):
    client = RabbitMQManagementClient.from_env()
    fake_resp = [{"properties": {}, "payload": "{}", "redelivered": False}]
    with patch.object(client, "_post_json", new=AsyncMock(return_value=fake_resp)) as p:
        rows = await client.peek_messages(queue="some_dlq", limit=5)
        assert rows == fake_resp
        called_url, body = p.call_args[0]
        assert called_url.endswith("/api/queues/%2F/some_dlq/get")
        assert body["count"] == 5
        assert body["ackmode"] == "ack_requeue_true"  # peek mode


@pytest.mark.asyncio
async def test_management_uses_basic_auth(env):
    client = RabbitMQManagementClient.from_env()
    assert client.auth == ("user", "secret")
    assert client.base_url == "http://rabbit-host:15672"


@pytest.mark.asyncio
async def test_vhost_url_encoded(env, monkeypatch):
    monkeypatch.setenv("RABBITMQ_VHOST", "my-vhost")
    client = RabbitMQManagementClient.from_env()
    with patch.object(client, "_post_json", new=AsyncMock(return_value=[])):
        await client.peek_messages(queue="q", limit=1)
        url = client._post_json.call_args[0][0]
        assert "/api/queues/my-vhost/q/get" in url
