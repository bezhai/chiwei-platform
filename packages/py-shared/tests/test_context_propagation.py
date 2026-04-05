import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI
from fastapi.testclient import TestClient
from inner_shared.middlewares.context_propagation import (
    create_context_propagation_middleware,
    get_context_headers,
)


@pytest.fixture
def app():
    app = FastAPI()
    app.add_middleware(create_context_propagation_middleware())

    @app.get("/test")
    async def test_endpoint():
        return get_context_headers()

    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def test_extracts_ctx_headers(client):
    resp = client.get(
        "/test",
        headers={
            "x-ctx-lane": "feat-test",
            "x-ctx-gray-group": "beta",
            "x-unrelated": "ignored",
        },
    )
    body = resp.json()
    assert body["x-ctx-lane"] == "feat-test"
    assert body["x-ctx-gray-group"] == "beta"
    assert "x-unrelated" not in body


def test_no_ctx_headers(client):
    resp = client.get("/test")
    body = resp.json()
    assert body == {}


def test_ctx_headers_in_response(client):
    resp = client.get("/test", headers={"x-ctx-lane": "dev"})
    assert resp.headers.get("x-ctx-lane") == "dev"
