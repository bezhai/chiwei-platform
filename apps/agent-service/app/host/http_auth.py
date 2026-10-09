"""管理路由在 handler 之外的那几步：内网凭据、泳道核对、把请求读成路由的 Data，以及这几步拒绝
时回答的外壳（:func:`refusal_detail`）。插件用 ``ctx.route`` 登记路由，:mod:`app.host.http` 把它
挂上 app 时用这几步把 handler 包起来。

**内网 Bearer 校验** —— 声明了要凭据的那几条路由挂它（``ctx.route(..., inner_secret=True)``）。

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
（:func:`request_data`）。顺序反过来的话，一个没有凭据的人能拿 422 的内容
把参数结构一个字段一个字段探出来。

**泳道核对** 排在凭据之后（:func:`route_guards`）：没凭据的人连"你落到了哪条泳道"都不该问得出来。
"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable
from typing import Any

from fastapi import Depends, HTTPException, Request

from app.infra import config
from app.runtime.data import Data
from app.runtime.lane_policy import current_deployment_lane, normalize_deployment_lane

# 一句话 → 这条路由拒绝时 detail 长什么样（:func:`refusal_detail` 按路由的声明造）。
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

    挂在哪几条路由上由路由自己的声明决定（:func:`route_guards`）—— 这一层
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


def refusal_detail(answers_with_lane: bool) -> RefusalDetail:
    """框架在 handler 之外挡回去的那几种回答（401 / 503 / 409 / 422）的 detail 长什么样。

    没声明 ``answers_with_lane`` 的路由拿到的还是原来那句话本身，**一个字节都没变** —— 那几条
    运维口今天就是这样答的。

    声明了的路由多一个执行泳道。它读的是本进程的部署环境，回显不了请求里的任何东西：泳道不在
    注册表里时请求会静默落到 prod 的 pod 上并返回一个正常的回答，自报的落点是"这次调用打的是我
    以为的那棵树"唯一的证据，而被拒的时候调用方同样需要这个答案。形状跟 handler 自己那几种拒绝
    一致（``lane`` + ``message``），免得同一条路由的两类拒绝长成两种东西。
    """

    def detail(message: str) -> str | dict:
        if not answers_with_lane:
            return message
        return {"lane": current_deployment_lane() or "prod", "message": message}

    return detail


def lane_match_guard(detail_for: RefusalDetail) -> Callable[[Request], Any]:
    """请求要去的泳道和进程的部署泳道不一致就 409 的那个路由级依赖。

    请求要去的泳道读 ``x-ctx-lane``（sidecar 选路用的就是它），没有就是 prod。读请求头
    而不是 :func:`app.api.middleware.get_lane`：这一步不该依赖某个中间件先跑过。
    """

    async def check(request: Request) -> None:
        requested = normalize_deployment_lane(request.headers.get("x-ctx-lane"))
        executed = current_deployment_lane()
        if requested != executed:
            raise HTTPException(
                status_code=409,
                detail=detail_for(
                    f"request was meant for lane {requested or 'prod'} but reached "
                    f"lane {executed or 'prod'}; nothing was done"
                ),
            )

    return check


def route_guards(*, inner_secret: bool, lane_match: bool, detail_for: RefusalDetail) -> list:
    """一条路由的路由级依赖：要凭据的先验凭据，要核对泳道的再核对泳道。FastAPI 按列表顺序解。

    凭据只挂在声明了的路由上：覆盖范围在结构上限死 —— 没声明的路由（``/health``、那几条运维口）
    连这段代码都走不到，不需要任何路径白名单来"记得别挡它们"。
    """
    guards = [Depends(inner_secret_guard(detail_for))] if inner_secret else []
    if lane_match:
        guards.append(Depends(lane_match_guard(detail_for)))
    return guards


async def request_fields(request: Request, method: str) -> dict[str, Any]:
    """请求里给路由的 Data 的那些字段：query string 一律读；POST / PUT 再把 JSON body 盖上去。

    同名字段 body 赢：显式的 body 比顺带的 query 更像调用方的本意。body 是空的、不是 JSON、或者
    不是一个对象，都当作没有 body：只靠 query 也可能凑齐 Data，凑不齐就在 :func:`request_data`
    那里 422。
    """
    fields: dict[str, Any] = dict(request.query_params)
    if method in {"POST", "PUT"}:
        try:
            body = await request.json()
        except Exception:
            # Classification: HARMLESS per-request fallback: a missing or non-JSON body is "no
            # body fields"; validation below answers 422 if the query alone does not do.
            body = {}
        if isinstance(body, dict):
            fields.update(body)
    return fields


def request_data(data_cls: type[Data], fields: dict[str, Any], detail_for: RefusalDetail) -> Data:
    """把字段造成路由的 Data；造不出来是调用方的问题，回 422，detail 按路由的外壳包。"""
    try:
        return data_cls(**fields)
    except Exception as exc:
        # Classification: PER-REQUEST validation failure, the caller's to fix: 422 to the HTTP
        # caller. A route is request/response, not a polling loop, so contract §4.1 does not
        # apply.
        raise HTTPException(status_code=422, detail=detail_for(str(exc))) from exc
