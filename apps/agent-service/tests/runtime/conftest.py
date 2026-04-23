"""Runtime integration test fixtures.

提供 ``test_db`` —— 基于 testcontainers 启动真实 Postgres 容器的 fixture。

关键约束：
  - **网络隔离**：容器端口必须绑定到 ``127.0.0.1``，不得 ``0.0.0.0``。
  - **会话级容器**：每个 pytest 进程启动一个容器，结束时销毁。
  - **函数级 engine**：每个测试得到独立的 ``AsyncEngine``；测试结束后 drop 所有
    ``public`` 下的表，下一个测试从空库开始。
  - **monkeypatch session**：同时替换 ``app.data.session.engine`` 和
    ``app.data.session.async_session``，保证被测代码走测试容器而不是 prod DSN。
  - **Docker 不可用时 skip**：CI 环境可能没有 docker，统一 skip 而不是红。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest


@pytest.fixture(scope="session")
def test_db_dsn() -> object:
    """Session-scoped: 启动一个绑定到 127.0.0.1 的 Postgres 容器，返回 async DSN。"""
    pytest.importorskip("testcontainers.postgres")

    try:
        import docker as _docker

        _docker.from_env().ping()
    except Exception:
        pytest.skip("docker unavailable; skipping real-pg integration tests")

    from testcontainers.postgres import PostgresContainer

    # driver='asyncpg' 让 get_connection_url() 直接返回 postgresql+asyncpg://
    pg = PostgresContainer("postgres:16-alpine", driver="asyncpg")

    # 关键：绑定到 127.0.0.1，而不是默认的 0.0.0.0。
    # testcontainers 把 self.ports 直接传给 docker-py 的 run()，value 支持
    # (host_ip, host_port) 元组；host_port=None 让 docker 随机分配。
    pg.ports = {5432: ("127.0.0.1", None)}

    pg.start()
    try:
        # 校验绑定 IP 正确 —— 容器必须对外部不可达
        container = pg.get_wrapped_container()
        container.reload()
        ports_attr = container.attrs["NetworkSettings"]["Ports"]["5432/tcp"]
        bind_ip = ports_attr[0]["HostIp"]
        assert bind_ip == "127.0.0.1", (
            f"test_db container must bind to 127.0.0.1, got {bind_ip!r}; "
            f"container attrs: {ports_attr}"
        )

        yield pg.get_connection_url()
    finally:
        pg.stop()


@pytest.fixture
async def test_db(
    test_db_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[object, None]:
    """Function-scoped: 给 ``app.data.session`` 注入指向测试容器的 engine。

    yields the test ``AsyncEngine`` 供测试直接用。

    teardown 阶段会 drop ``public`` schema 下所有表，保证测试之间互不影响。
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )

    import app.data.session as session_mod

    test_engine = create_async_engine(
        test_db_dsn,
        echo=False,
        pool_pre_ping=True,
        future=True,
    )
    test_factory = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    monkeypatch.setattr(session_mod, "engine", test_engine)
    monkeypatch.setattr(session_mod, "async_session", test_factory)

    try:
        yield test_engine
    finally:
        # 清理 public schema，保证下一个测试从空库开始
        async with test_engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    DO $$
                    DECLARE r RECORD;
                    BEGIN
                        FOR r IN
                            SELECT tablename FROM pg_tables WHERE schemaname = 'public'
                        LOOP
                            EXECUTE 'DROP TABLE IF EXISTS public.'
                                || quote_ident(r.tablename) || ' CASCADE';
                        END LOOP;
                    END $$;
                    """
                )
            )
        await test_engine.dispose()
