"""内网 Bearer 校验 —— 声明了 ``requires_inner_secret=True`` 的那几条路由挂它。

凭据是 ``INNER_HTTP_SECRET`` + ``Authorization: Bearer <token>``：这个进程已经通过
``inter-service-auth`` 这个 ConfigBundle 拿得到它（:mod:`app.infra.config` 里的
``inner_http_secret``），而仓库里内网互信的既有口径就是这个 Bearer
（``packages/ts-shared/src/middleware/auth.ts``、``apps/sandbox-worker``、渠道服务
之间的泳道交接）。换一套等于同一件事有两种做法，而第二套没有人在维护。

三件事是写这一层的时候必须想清楚的：

**一 · 没配凭据的时候全拒，不是全放。** 这个进程现有那两处用 ``inner_http_secret``
的地方（``app/capabilities/sandbox.py``、``app/infra/image.py``）都是 ``if secret:``
配了才带头 —— 那是**出站**的正确姿势：没配就不带，让对面决定。照抄到入站就变成"没配
就放行"，等于没有门，而且是没有报错的那种：线上一次配置漏配，门整个不在了而看上去
一切正常。所以这里没配是 503，一个也不放。

**二 · 比较落在字节上，而且是常量时间的。** ``hmac.compare_digest`` 对 ``str`` 只
接受 ASCII，喂一个非 ASCII 的进去直接抛 ``TypeError`` —— 于是一个拿错钥匙的人能把
服务打出一个 500，而那在日志里看起来像个 bug 不像一次敲门。两边各自编回自己那几个
字节再比：header 那一侧 ASGI 是按 latin-1 解的，配置那一侧 ``os.environ`` 是按 utf-8
解的，各自编回去拿到的就是线上和配置里原本的字节，中间没有猜编码的一步。

**三 · 拒绝发生在参数被反序列化之前。** 这是靠挂法保证的：它是路由级的
``Depends``，FastAPI 在调 handler 之前就把它解完了，而参数反序列化在 handler 体内
（:mod:`app.runtime.http_source`）。顺序反过来的话，一个没有凭据的人能拿 422 的内容
把参数结构一个字段一个字段探出来。
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable

from fastapi import HTTPException, Request

from app.infra import config

# 一句话 → 这条路由拒绝时 detail 长什么样。由 :mod:`app.runtime.http_source` 按路由
# 的声明造好传进来：这一层管凭据，回答的外壳（比如带不带执行泳道）是那一层的事。
RefusalDetail = Callable[[str], str | dict]

logger = logging.getLogger(__name__)

_SCHEME = "Bearer "

# 拿错钥匙 401；这扇门根本没装锁芯 503。分开是因为下一步不是同一件事：一个是调用方
# 换凭据，一个是去看这个 app 的 ConfigBundle 有没有引 inter-service-auth。全用 401
# 的话，漏配的那一次会被当成"我 token 写错了"，人会去查错的地方。
_REFUSED = 401
_NO_LOCK_FITTED = 503


def _presented_credential(request: Request) -> bytes | None:
    """请求带来的那串字节；没带成 ``Authorization: Bearer <token>`` 就是 ``None``。

    ASGI 把 header 按 latin-1 解成 ``str``，latin-1 编回去拿到的就是线上那几个字节。
    """
    header = request.headers.get("authorization", "")
    if not header.startswith(_SCHEME):
        return None
    return header[len(_SCHEME) :].encode("latin-1")


def _configured_credential() -> bytes | None:
    """这个进程手里的那串字节；没配（或配成空串）就是 ``None``。

    每次现取，不在 import 时定死：定死的话，测试和运维都只能重启进程才换得掉，而
    "这个进程到底配了没有"正是这一层要回答的问题。
    """
    configured = config.settings.inner_http_secret
    if not configured:
        return None
    # os.environ 是按 utf-8 + surrogateescape 解出来的，编回去拿到的就是配进来的字节。
    return configured.encode("utf-8", "surrogateescape")


def inner_secret_guard(detail_for: RefusalDetail) -> Callable[[Request], None]:
    """造一个"没带对凭据就别往下走"的路由级依赖。

    挂在哪几条路由上由 ``Source.http(requires_inner_secret=True)`` 决定 —— 这一层
    **不认路径**：认路径的话，"哪些该挡"就变成两处各写一遍的东西，而新增一条路由时
    没有任何东西会提醒你去改第二处。

    ``detail_for`` 把一句话包成这条路由的回答外壳。这一层不自己拼 detail，是因为
    "回答里带不带执行泳道"是路由的声明，不是凭据校验的事；两边各拼一份的话，同一条
    路由的 401 和 422 会长成两种形状。
    """

    def require_inner_secret(request: Request) -> None:
        expected = _configured_credential()
        if expected is None:
            logger.error(
                "refused %s %s: INNER_HTTP_SECRET is not configured in this "
                "process, so this route cannot authenticate anyone — check the "
                "app's inter-service-auth ConfigBundle reference",
                request.method,
                request.url.path,
            )
            raise HTTPException(
                _NO_LOCK_FITTED,
                detail=detail_for(
                    "inner credential is not configured on this service"
                ),
            )

        presented = _presented_credential(request)
        if presented is None or not hmac.compare_digest(presented, expected):
            raise HTTPException(
                _REFUSED,
                detail=detail_for("missing or invalid credential"),
                headers={"WWW-Authenticate": "Bearer"},
            )

    return require_inner_secret
