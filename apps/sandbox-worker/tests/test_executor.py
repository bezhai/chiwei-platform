"""Tests for sandbox executor"""

import pytest

from app.executor import execute


class TestExecute:
    @pytest.mark.asyncio
    async def test_simple_echo(self):
        result = await execute("echo hello world")
        assert result.exit_code == 0
        assert "hello world" in result.stdout
        assert result.duration_ms > 0

    @pytest.mark.asyncio
    async def test_stderr_output(self):
        result = await execute("echo error >&2")
        assert "error" in result.stderr

    @pytest.mark.asyncio
    async def test_nonzero_exit_code(self):
        result = await execute("exit 42")
        assert result.exit_code == 42

    @pytest.mark.asyncio
    async def test_timeout(self):
        result = await execute("sleep 10", timeout=1)
        assert result.exit_code == 124
        assert "timed out" in result.stderr

    @pytest.mark.asyncio
    async def test_python_execution(self):
        result = await execute('python3 -c "print(2 + 3)"')
        assert result.exit_code == 0
        assert "5" in result.stdout

    @pytest.mark.asyncio
    async def test_env_vars(self):
        result = await execute("echo $MY_VAR", envs={"MY_VAR": "test_value"})
        assert "test_value" in result.stdout

    @pytest.mark.asyncio
    async def test_tmpdir_isolation(self):
        """每次执行的工作目录不同"""
        result1 = await execute("pwd")
        result2 = await execute("pwd")
        # 两次执行的 tmpdir 路径不同
        assert result1.stdout.strip() != result2.stdout.strip()

    @pytest.mark.asyncio
    async def test_empty_command(self):
        result = await execute("")
        # 空命令也能正常返回
        assert result.exit_code == 0

    @pytest.mark.asyncio
    async def test_multiline_output(self):
        result = await execute("echo line1; echo line2; echo line3")
        assert result.exit_code == 0
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 3


class TestExecuteAPI:
    """测试 FastAPI 端点"""

    @pytest.fixture
    def client(self):
        from fastapi.testclient import TestClient

        from app.main import app

        return TestClient(app)

    def test_exec_endpoint(self, client):
        resp = client.post("/exec", json={"command": "echo api_test"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["exit_code"] == 0
        assert "api_test" in data["stdout"]

    def test_exec_missing_command(self, client):
        resp = client.post("/exec", json={"command": ""})
        assert resp.status_code == 400

    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_auth_required(self, client, monkeypatch):
        monkeypatch.setattr("app.main.INNER_HTTP_SECRET", "secret123")
        resp = client.post("/exec", json={"command": "echo test"})
        assert resp.status_code == 403

    def test_auth_valid(self, client, monkeypatch):
        monkeypatch.setattr("app.main.INNER_HTTP_SECRET", "secret123")
        resp = client.post(
            "/exec",
            json={"command": "echo test"},
            headers={"Authorization": "Bearer secret123"},
        )
        assert resp.status_code == 200
