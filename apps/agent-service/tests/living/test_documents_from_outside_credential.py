"""从外面改这棵树要带凭据 —— 一道门，只挡那四条。

这四条端点里有一条能删掉一份文档，而那份文档的正文是原样塞进模型眼前的：删掉之后
她走进那个地方什么都看不到，**而且不会有任何报错**。这个服务的路由前缀整段对外可达，
所以裸着的后果不是"重投一批死信"那个量级。

凭据用 ``INNER_HTTP_SECRET`` + ``Authorization: Bearer``：这个进程已经通过
``inter-service-auth`` 拿得到它，仓库里内网互信的既有口径就是这个 Bearer，换别的等于
开第二套。

这一份守的是六条：

一 · **带对凭据真的进得去。** 单独列成一条，是因为下面五条全是拒绝场景，**一个把所有
   人都挡在外面的门能通过它们中的每一条**，而那样的门跟没有门一样没用。
二 · **不带、带错、带得不成形，三种都进不去**，而且盘上一个字没动。
三 · **没配凭据的时候全拒，不是全放。** 这个进程现有那两处用它的地方都是出站
   （``if secret:`` 没配就不带头），那是出站的正确姿势；照抄到入站就等于没有门，
   而且是没有报错的那种。
四 · **只有这四条在门后面。** ``/health`` 和那几条运维口逐字不变 —— 包括响应体。
五 · **凭据先于参数。** 不带凭据的人不能靠 422 的内容把参数结构探出来。
六 · **比较是常量时间的。** 这一条行为上验不出来（``==`` 和 ``compare_digest`` 对外
   的回答一模一样），只能按住源码本身。

**用来选被测对象和构造期望值的东西一律写字面量**：四条路径、五条运维口、状态码、
指纹的算法。从实现里 import 的话，把门的覆盖范围收窄的同时也把这一份的检查范围收窄，
用例照样绿 —— 而这一份正是"门有没有漏"的唯一保证。
"""
from __future__ import annotations

import ast
import dataclasses
import hashlib
import importlib
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.api.routes import router as health_router
from app.infra import config
from app.runtime.http_source import register_http_sources

LANE = "coe-living"
SECRET = "inner-http-secret-for-the-door"
LISTING = "/admin/world-documents/listing"
DOCUMENT = "/admin/world-documents/document"
BASE = "http://world-documents.test"

KITCHEN = "厨房.md"
KITCHEN_BODY = "灶台靠窗。"
BRAND_NEW = "外面新建的.md"

# 门后面的那四条。写字面量：从实现的常量推出来的话，把实现收窄会同时把这个集合收窄。
BEHIND_THE_DOOR = frozenset(
    {
        ("/admin/world-documents/listing", "GET"),
        ("/admin/world-documents/document", "GET"),
        ("/admin/world-documents/document", "PUT"),
        ("/admin/world-documents/document", "DELETE"),
    }
)

# 门**外面**的那几条运维口。同样写字面量，理由同上。
IN_THE_OPEN = (
    ("POST", "/admin/search"),
    ("POST", "/admin/dlq/inspect"),
    ("POST", "/admin/dlq/clear-idempotent"),
    ("POST", "/admin/dlq/dry-run"),
    ("POST", "/admin/dlq/requeue"),
)

# 挡在门外的两种回答：拿错钥匙 401；这扇门根本没装锁芯 503。
REFUSED = 401
NO_LOCK_FITTED = 503


def _fingerprint(body: str) -> str:
    """这段正文的指纹：sha256 取前 12 位十六进制。**测试自己算，不从实现 import。**"""
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


def bearer(secret: str | bytes) -> dict:
    """一顶带着凭据的帽子，值是**线上那几个字节**。

    httpx 对 header 里的 ``str`` 按 ascii 编码，非 ASCII 的凭据在客户端就炸了，根本
    到不了门跟前 —— 那样验的是 httpx 不是这道门。所以这里统一按 utf-8 编成 bytes
    自己交出去，跟一个拿着同一串字节的真实调用方一样。
    """
    raw = secret if isinstance(secret, bytes) else secret.encode("utf-8")
    return {"Authorization": b"Bearer " + raw}


@pytest.fixture
def docs(tmp_path, monkeypatch) -> Path:
    """一棵文档树，里面已经有一份厨房。"""
    monkeypatch.setenv("WORLD_DOCS_DIR", str(tmp_path / "mount"))
    monkeypatch.setenv("LANE", LANE)
    root = tmp_path / "mount" / LANE
    root.mkdir(parents=True)
    (root / KITCHEN).write_text(KITCHEN_BODY, encoding="utf-8")
    return root


@pytest.fixture
def credential(monkeypatch):
    """进程手里的那把钥匙，交回一个能换掉它的手（``None`` = 根本没配）。"""

    def configured_as(secret: str | None) -> None:
        monkeypatch.setattr(
            config,
            "settings",
            dataclasses.replace(config.settings, inner_http_secret=secret),
        )

    configured_as(SECRET)
    return configured_as


@pytest.fixture
def api(credential) -> FastAPI:
    """真实 wiring 注册出来的那个 app，外加 ``/health`` —— 跟 main.py 同一套路由。"""
    import app.wiring.admin as admin_wiring

    importlib.reload(admin_wiring)
    application = FastAPI()
    application.include_router(health_router)
    register_http_sources(application)
    return application


@pytest.fixture
async def outside(api):
    """外面那一侧。**默认不带任何凭据** —— 要带的那几条自己戴帽子。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api), base_url=BASE
    ) as client:
        yield client


async def _knock_on_all_four(client, headers: dict | None) -> list[int]:
    """四条各敲一次，交回四个状态码。

    敲的参数都是**真的会改盘**的那种：写的是一份还不存在的文档（没有指纹要求），删的
    带着厨房那一版的正确指纹。门要是不在，这两下会真的落盘 —— 下面那条"盘上一个字
    没动"才有杀伤力。
    """
    listing = await client.get(LISTING, headers=headers)
    read = await client.get(DOCUMENT, params={"path": KITCHEN}, headers=headers)
    write = await client.put(
        DOCUMENT,
        json={"path": BRAND_NEW, "content": "外面写的"},
        headers=headers,
    )
    delete = await client.delete(
        DOCUMENT,
        params={"path": KITCHEN, "fingerprint": _fingerprint(KITCHEN_BODY)},
        headers=headers,
    )
    return [r.status_code for r in (listing, read, write, delete)]


def _nothing_moved(docs: Path) -> None:
    """盘上一个字没动：厨房还在、内容没变，外面那一份没被建出来。"""
    assert (docs / KITCHEN).read_text(encoding="utf-8") == KITCHEN_BODY
    assert not (docs / BRAND_NEW).exists()


# --------------------------------------------------------------------------
# 一 · 带对凭据真的进得去
#
# 这一条单独存在，是因为这一份里其余的验收全是拒绝场景 —— 一个全拒的门能通过它们
# 中的每一条。
# --------------------------------------------------------------------------


async def test_the_right_credential_gets_through_all_four(outside, docs):
    """列、读、写、删，四条都真的执行了，不是被门挡回来的 200。"""
    listed = await outside.get(LISTING, headers=bearer(SECRET))
    assert listed.status_code == 200, listed.text
    assert listed.json()["entries"] == [KITCHEN]

    read = await outside.get(DOCUMENT, params={"path": KITCHEN}, headers=bearer(SECRET))
    assert read.status_code == 200, read.text
    assert read.json()["content"] == KITCHEN_BODY

    rewritten = await outside.put(
        DOCUMENT,
        json={
            "path": KITCHEN,
            "content": "灶台改到北墙。",
            "fingerprint": read.json()["fingerprint"],
        },
        headers=bearer(SECRET),
    )
    assert rewritten.status_code == 200, rewritten.text
    assert (docs / KITCHEN).read_text(encoding="utf-8") == "灶台改到北墙。"

    gone = await outside.delete(
        DOCUMENT,
        params={"path": KITCHEN, "fingerprint": rewritten.json()["fingerprint"]},
        headers=bearer(SECRET),
    )
    assert gone.status_code == 200, gone.text
    assert not (docs / KITCHEN).exists()


# --------------------------------------------------------------------------
# 二 · 不带、带错、带得不成形，三种都进不去
# --------------------------------------------------------------------------


async def test_no_credential_gets_nothing(outside, docs):
    assert await _knock_on_all_four(outside, None) == [REFUSED] * 4
    _nothing_moved(docs)


async def test_a_wrong_credential_gets_nothing(outside, docs):
    assert await _knock_on_all_four(outside, bearer("不是那把钥匙")) == [REFUSED] * 4
    _nothing_moved(docs)


@pytest.mark.parametrize(
    "header",
    [
        {"Authorization": SECRET},
        {"Authorization": f"Basic {SECRET}"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": ""},
        {"X-Inner-Secret": SECRET},
    ],
    ids=["bare", "basic", "scheme-only", "empty-token", "empty", "wrong-header"],
)
async def test_a_credential_that_is_not_a_bearer_token_gets_nothing(
    outside, docs, header
):
    """凭据认的是 ``Authorization: Bearer <token>`` 这一种，别的形状都不算。"""
    assert await _knock_on_all_four(outside, header) == [REFUSED] * 4
    _nothing_moved(docs)


# --------------------------------------------------------------------------
# 三 · 没配凭据的时候全拒，不是全放
# --------------------------------------------------------------------------


async def test_a_service_with_no_credential_configured_refuses_everyone(
    outside, docs, credential
):
    """没配 = 这扇门没装锁芯，谁也开不了 —— 包括拿着"正确"凭据来的人。

    照抄出站那个 ``if secret:`` 的话，这四条在没配的时候是**全放**，而且没有任何报错：
    线上一次配置漏配，门就整个不在了，而看上去一切正常。
    """
    credential(None)

    assert await _knock_on_all_four(outside, None) == [NO_LOCK_FITTED] * 4
    assert await _knock_on_all_four(outside, bearer(SECRET)) == [NO_LOCK_FITTED] * 4
    _nothing_moved(docs)


async def test_an_empty_credential_counts_as_not_configured(outside, docs, credential):
    """配成空串跟没配是同一处境 —— 不能变成"带一个空 token 就进得去"。"""
    credential("")

    assert await _knock_on_all_four(outside, None) == [NO_LOCK_FITTED] * 4
    assert await _knock_on_all_four(outside, bearer("")) == [NO_LOCK_FITTED] * 4
    _nothing_moved(docs)


# --------------------------------------------------------------------------
# 三之二 · 非 ASCII 的凭据是 401，不是 500
#
# ``hmac.compare_digest`` 对 ``str`` 只接受 ASCII：喂一个非 ASCII 的进去直接抛异常，
# 于是门自己 500 —— 一个拿错钥匙的人把服务打出一个五百，而日志里看起来像个 bug 不像
# 一次敲门。比较必须落在字节上。
# --------------------------------------------------------------------------


async def test_a_non_ascii_credential_is_refused_not_crashed(outside, docs):
    assert await _knock_on_all_four(outside, bearer("口令-漢字")) == [REFUSED] * 4
    _nothing_moved(docs)


async def test_a_non_ascii_secret_still_opens_its_own_door(outside, docs, credential):
    """钥匙本身是非 ASCII 的时候，拿着它的人进得去，拿着别的进不去。

    帽子里写的是**线上那几个字节**（utf-8），不是让 httpx 替我们猜一个编码：这条要
    回答的就是"两边比的是不是同一串字节"。
    """
    secret = "口令-漢字"
    credential(secret)

    listed = await outside.get(LISTING, headers=bearer(secret.encode("utf-8")))
    assert listed.status_code == 200, listed.text

    assert await _knock_on_all_four(
        outside, bearer("另一个口令".encode())
    ) == [REFUSED] * 4
    _nothing_moved(docs)


# --------------------------------------------------------------------------
# 四 · 只有这四条在门后面
# --------------------------------------------------------------------------


async def test_only_those_four_routes_ask_for_a_credential(outside, api):
    """把这个 app 上**每一条**路由都不带凭据敲一遍，被挡的必须正好是那四条。

    两个方向都堵上：门挂宽了（比如挂成全局中间件），``/health`` 和运维口会落进
    ``asked`` 里；门挂窄了，那四条里会有人掉出来。期望值是上面那个字面量集合，不是
    从实现的路径前缀推出来的 —— 那样收窄实现的同时也收窄了期望值。
    """
    everything = {
        (route.path, method)
        for route in api.routes
        for method in (getattr(route, "methods", set()) or set()) - {"HEAD"}
    }
    assert BEHIND_THE_DOOR <= everything, everything

    asked: set[tuple[str, str]] = set()
    for path, method in sorted(everything):
        answer = await outside.request(
            method, path, json={} if method in {"POST", "PUT"} else None
        )
        if answer.status_code in (401, 403, NO_LOCK_FITTED):
            asked.add((path, method))

    assert asked == set(BEHIND_THE_DOOR)


async def test_health_answers_the_same_with_or_without_a_credential(outside):
    """``/health`` 逐字不变：带不带凭据、带对带错，回答是同一个。"""
    answers = [
        await outside.get("/health"),
        await outside.get("/health", headers=bearer("不是那把钥匙")),
        await outside.get("/health", headers=bearer(SECRET)),
    ]
    assert [a.status_code for a in answers] == [200, 200, 200]

    # timestamp 每次都不一样，是这个端点本来的样子，比的是除它之外的整份回答。
    shapes = [
        {k: v for k, v in a.json().items() if k != "timestamp"} for a in answers
    ]
    assert shapes[0] == shapes[1] == shapes[2], shapes
    assert shapes[0]["status"] == "ok"
    assert shapes[0]["service"] == "agent-service"


@pytest.mark.parametrize(("method", "path"), IN_THE_OPEN)
async def test_the_ops_routes_answer_the_same_with_or_without_a_credential(
    outside, method, path
):
    """搜索和 DLQ 那几条逐字不变 —— 连响应体一起比，不只是状态码。

    它们今天是裸的。这次要加的是**那四条**的门，不是顺手把整段前缀关起来：这几条一旦
    跟着要凭据，现有的调用方会在下一次运维的时候才发现自己进不去了。

    （这条比的是"门有没有扩大到它们身上"。"行为跟改前逐字相同"是另一回事，钉在
    ``tests/wiring/test_ops_routes_baseline.py``：那一份拿改前的实现捕获了完整回答。）
    """
    answers = [
        await outside.request(method, path, json={}),
        await outside.request(method, path, json={}, headers=bearer(SECRET)),
        await outside.request(
            method, path, json={}, headers=bearer("不是那把钥匙")
        ),
    ]
    assert [a.status_code for a in answers] == [422, 422, 422], [a.text for a in answers]
    assert answers[0].text == answers[1].text == answers[2].text

    # 自报泳道也只给那四条。这几条的回答里不能凭空长出一个 lane 字段 —— 那同样是
    # 改了既有路由的回答。
    assert "lane" not in answers[0].text, answers[0].text


# --------------------------------------------------------------------------
# 四之二 · 门自己挡回去的那几种回答，也要说是谁挡的
#
# 契约是"每个响应都带上实际执行它的那个进程自己的泳道"。401 / 503 / 422 产生在
# handler 之外，原来不带 —— 于是调用方被拒的时候恰恰不知道是哪个进程拒的，而泳道不在
# 注册表里时请求会静默落到 prod 的 pod 上，"我打到哪棵树了"这条链路上只有这一个证据。
# --------------------------------------------------------------------------


async def test_every_refusal_from_the_door_says_which_lane_refused_it(
    outside, docs, credential
):
    """不带凭据、带错凭据、进程没配凭据，三种被拒的回答都自报泳道。"""
    no_credential = await outside.get(LISTING)
    wrong = await outside.get(LISTING, headers=bearer("不是那把钥匙"))
    assert [no_credential.status_code, wrong.status_code] == [REFUSED, REFUSED]
    assert no_credential.json()["detail"]["lane"] == LANE, no_credential.text
    assert wrong.json()["detail"]["lane"] == LANE, wrong.text

    credential(None)
    no_lock = await outside.get(LISTING)
    assert no_lock.status_code == NO_LOCK_FITTED, no_lock.text
    assert no_lock.json()["detail"]["lane"] == LANE, no_lock.text


async def test_a_parameter_that_does_not_parse_also_says_which_lane(outside, docs):
    """参数反序列化失败那条（422）产生在 handler 之外，同样要自报。"""
    extra_field = await outside.get(
        LISTING, params={"lane": "prod"}, headers=bearer(SECRET)
    )
    assert extra_field.status_code == 422, extra_field.text
    assert extra_field.json()["detail"]["lane"] == LANE, extra_field.text


@pytest.mark.parametrize(
    "deployed_as", ["coe-living", "ppe-somewhere-else", None], ids=["coe", "ppe", "none"]
)
async def test_a_refusal_reports_the_same_lane_the_handler_would(
    outside, tmp_path, monkeypatch, deployed_as
):
    """被拒时自报的泳道，跟同一个进程成功时自报的**是同一个值**。

    两边各算一遍的话它们会漂，而漂了之后自报的落点仍然看起来像个正确答案 —— 这正是
    这个字段唯一要防的事。所以判据不是"两边都有 lane"，是"两边相等"，而且换几条部署
    泳道都相等（没有 LANE 的进程自报 prod）。
    """
    monkeypatch.setenv("WORLD_DOCS_DIR", str(tmp_path / "mount"))
    if deployed_as is None:
        monkeypatch.delenv("LANE", raising=False)
    else:
        monkeypatch.setenv("LANE", deployed_as)

    landed = await outside.get(LISTING, headers=bearer(SECRET))
    refused = await outside.get(LISTING)

    assert landed.status_code == 200, landed.text
    assert refused.status_code == REFUSED, refused.text
    assert refused.json()["detail"]["lane"] == landed.json()["lane"]
    assert landed.json()["lane"] == (deployed_as or "prod")


# --------------------------------------------------------------------------
# 五 · 凭据先于参数
# --------------------------------------------------------------------------


async def test_the_credential_is_checked_before_the_parameters_are(outside, docs):
    """没带凭据的请求在参数被反序列化**之前**就被拒。

    反过来的话，一个没有凭据的人可以拿 422 的内容把参数结构一个字段一个字段探出来：
    哪些字段必填、叫什么名、什么类型。门后面有什么，不该从门外看得见。
    """
    unguessable = await outside.get(LISTING, params={"lane": "prod"})
    assert unguessable.status_code == REFUSED, unguessable.text

    # 同一个请求带上凭据是 422 —— 说明上面那一下确实是被门挡住的，不是这条路径本来
    # 就不会走到参数校验。
    guessed = await outside.get(
        LISTING, params={"lane": "prod"}, headers=bearer(SECRET)
    )
    assert guessed.status_code == 422, guessed.text

    # 而被挡回来的那一份回答里，参数结构一个字都没漏出去。（``lane`` 不在这份名单里：
    # 它是被拒时自报的落点，见上面那一节，跟"门后面有哪些参数"是两件事。）
    leaked = [
        word
        for word in ("under", "extra", "forbid", "validation error")
        if word in unguessable.text
    ]
    assert leaked == [], unguessable.text

    nonsense = await outside.put(DOCUMENT, json={"没有这个字段": 1})
    assert nonsense.status_code == REFUSED, nonsense.text
    assert [
        word for word in ("path", "content", "fingerprint") if word in nonsense.text
    ] == [], nonsense.text
    _nothing_moved(docs)


# --------------------------------------------------------------------------
# 六 · 比较是常量时间的
# --------------------------------------------------------------------------


def test_the_credential_is_compared_in_constant_time():
    """按住源码本身：这一条行为上验不出来。

    ``==`` 一个字节一个字节比，第一个不同就返回 —— 花多长时间泄露的是"你猜对了前几
    个字节"。对外的回答跟 ``compare_digest`` 一模一样，所以没有任何一条 HTTP 用例分
    得出这两种，只能按住写下来的那一行。
    """
    import app.runtime.http_auth as door

    tree = ast.parse(Path(door.__file__).read_text(encoding="utf-8"))

    naive = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and any(isinstance(op, ast.Eq | ast.NotEq) for op in node.ops)
    ]
    assert naive == [], (
        f"门里出现了 == / != 比较（第 {naive} 行）—— 凭据只能用常量时间的比较。"
    )

    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "compare_digest" in called, called
