"""从外部读写世界的文档树 —— 四个 HTTP 端点。

树上写歪的一份文档，在这之前只能等 world 自己发现自己改。这四个端点让人插得上手，
而插进去的这只手必须跟 world 那五只手**抢同一把锁、认同一套指纹**——否则"两个写者"
就退化成"谁后写谁赢"，先写的那一版连同它以为自己做成了的那件事一起消失。

这一份守的是六条：

一 · **正向路径真的走得通。** 新建 → 读回来 → 带着指纹整份重写 → 读回来确认改了 →
   带着新指纹删掉 → 确认删了。单独列成一条，是因为下面五条全是拒绝场景，**一个永远
   返回拒绝的实现能通过它们中的每一条**。
二 · **指纹语义跟那五只手一字不差**：不带指纹覆盖已存在的被拒、带过期指纹被拒、删除
   不带指纹被拒、目标已被删但带了指纹被拒；每一种都必须"盘上一个字没动"。
三 · **路径逃不出根目录**，判据跟那五只手共用一套。
四 · **每个回答都带着执行它的那个进程自己的泳道**，而且那个值读的是进程的部署环境，
   请求里塞不进去。这是"这次调用改的是我以为的那棵树"的唯一证据。
五 · **外部写入和 world 的轮次抢同一把锁。** 这一层实际上有两把（协程那把管排队和
   上限，线程那把覆盖真正碰盘的那一段），两把分别都要有用例按着：
   * 正在落盘的一轮没做完，外部写入排队等，不并行写盘；
   * 那一轮的协程被取消之后（协程锁已经放开，线程还在跑），外部写入仍然被按住，
     最终拿到的是"被拒"而不是一句会被盖掉的"写好了"；
   * 一份文档卡住的时候，队在它后面的外部写入不占线程，别的文档照常写得进去。
六 · **外面这一侧没有第二条写入路径。** handler 自己一行都不碰盘。

**用来选被测对象和构造期望值的东西一律写字面量**：路径、泳道名、环境变量名、指纹的
算法。从实现里 import 常量的话，实现收窄的同时期望值跟着收窄，用例照样绿。
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import importlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from app.infra import config
from app.living.documents import write_document
from app.runtime.http_source import register_http_sources

LANE = "coe-living"
LISTING = "/admin/world-documents/listing"
DOCUMENT = "/admin/world-documents/document"
BASE = "http://world-documents.test"

# 这四条在门后面（``INNER_HTTP_SECRET`` + ``Authorization: Bearer``），所以这一份里
# 的每一次调用都带着凭据 —— 验的是门**后面**那几件事。门本身验在
# ``test_documents_from_outside_credential.py``：谁进得来、谁进不来、挡的是不是正好
# 这四条。
SECRET = "inner-http-secret-for-the-door"


def _fingerprint(body: str) -> str:
    """这段正文的指纹：sha256 取前 12 位十六进制。

    **测试自己算，不从实现 import。** import 过来的话，实现换了摘要算法或者长度，
    期望值跟着一起换，而用例照样绿——它验的就变成了"实现跟自己一致"。
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:12]


async def _until(flag: threading.Event, seconds: float = 5.0) -> None:
    """等一面在别的线程里立起来的旗。

    不用 ``asyncio.to_thread(flag.wait)``：那会占掉一个 executor 槽，而"排队的人占不
    占线程"正是下面一条用例在数的东西。
    """
    deadline = time.monotonic() + seconds
    while not flag.is_set():
        assert time.monotonic() < deadline, "等的那件事在期限内没有发生"
        await asyncio.sleep(0.01)


@pytest.fixture
def docs(tmp_path, monkeypatch) -> Path:
    """一棵空的文档树，挂载点指到 tmp，进程的泳道是 coe-living。"""
    monkeypatch.setenv("WORLD_DOCS_DIR", str(tmp_path / "mount"))
    monkeypatch.setenv("LANE", LANE)
    root = tmp_path / "mount" / LANE
    root.mkdir(parents=True)
    return root


@pytest.fixture
def credential(monkeypatch) -> None:
    """进程手里配着一把凭据 —— 不配的话这四条一律 503，下面一条都跑不起来。"""
    monkeypatch.setattr(
        config,
        "settings",
        dataclasses.replace(config.settings, inner_http_secret=SECRET),
    )


@pytest.fixture
def api(credential) -> FastAPI:
    """真实 wiring 注册出来的那个 app —— 不是用例自己临时 wire 的一条边。"""
    import app.wiring.admin as admin_wiring

    importlib.reload(admin_wiring)
    application = FastAPI()
    register_http_sources(application)
    return application


@pytest.fixture
async def outside(api):
    """外面那一侧：请求真的打进注册好的路由，跑在同一个事件循环里，带着凭据。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api),
        base_url=BASE,
        headers={"Authorization": f"Bearer {SECRET}"},
    ) as client:
        yield client


# --------------------------------------------------------------------------
# 零 · 这四条路由真的挂上了
# --------------------------------------------------------------------------


def test_the_four_ways_in_are_registered(api):
    """列目录、读一份、整份重写、删掉，四条路由各在各的方法上。

    路径和方法在这里写字面量：从实现的常量推出来的话，把实现的前缀改窄会同时把这条
    断言改窄，而它照样绿。
    """
    registered = {
        (route.path, method)
        for route in api.routes
        for method in (getattr(route, "methods", set()) or set()) - {"HEAD"}
    }
    assert {
        ("/admin/world-documents/listing", "GET"),
        ("/admin/world-documents/document", "GET"),
        ("/admin/world-documents/document", "PUT"),
        ("/admin/world-documents/document", "DELETE"),
    } <= registered, registered


# --------------------------------------------------------------------------
# 一 · 正向路径：一份文档从建出来到被删掉，全程从外面走
#
# 这一条单独存在，是因为这一份里其余的验收全是拒绝场景 —— 一个永远返回拒绝的实现
# 能通过它们中的每一条。
# --------------------------------------------------------------------------


async def test_a_document_can_be_made_read_rewritten_and_deleted_from_outside(
    outside, docs
):
    """新建 → 读回来核对 → 整份重写 → 读回来确认改了 → 删掉 → 确认删了。"""
    where = "设定/世界底子.md"

    made = await outside.put(
        DOCUMENT, json={"path": where, "content": "第一版的世界底子。"}
    )
    assert made.status_code == 200, made.text
    assert made.json()["outcome"] == "ok"
    assert made.json()["fingerprint"] == _fingerprint("第一版的世界底子。")

    read_back = await outside.get(DOCUMENT, params={"path": where})
    assert read_back.status_code == 200, read_back.text
    assert read_back.json()["content"] == "第一版的世界底子。"
    assert read_back.json()["path"] == where
    first = read_back.json()["fingerprint"]
    assert first == _fingerprint("第一版的世界底子。")

    rewritten = await outside.put(
        DOCUMENT,
        json={"path": where, "content": "第二版的世界底子。", "fingerprint": first},
    )
    assert rewritten.status_code == 200, rewritten.text
    assert rewritten.json()["outcome"] == "ok"
    second = rewritten.json()["fingerprint"]
    assert second == _fingerprint("第二版的世界底子。")

    changed = await outside.get(DOCUMENT, params={"path": where})
    assert changed.json()["content"] == "第二版的世界底子。"
    assert changed.json()["fingerprint"] == second

    gone = await outside.delete(
        DOCUMENT, params={"path": where, "fingerprint": second}
    )
    assert gone.status_code == 200, gone.text
    assert gone.json()["outcome"] == "ok"

    assert not (docs / where).exists()
    missing = await outside.get(DOCUMENT, params={"path": where})
    assert missing.status_code == 404, missing.text


async def test_a_new_document_shows_up_in_the_listing(outside, docs):
    """列目录看得见刚写下的那一份，目录带一个尾斜杠。"""
    await outside.put(DOCUMENT, json={"path": "地方/家/厨房.md", "content": "灶台靠窗。"})

    listed = await outside.get(LISTING)
    assert listed.status_code == 200, listed.text
    assert listed.json()["entries"] == ["地方/", "地方/家/", "地方/家/厨房.md"]
    assert listed.json()["mounted"] is True


async def test_the_listing_can_be_narrowed_to_one_directory(outside, docs):
    """带上 under 只列那一块。"""
    (docs / "设定").mkdir()
    (docs / "设定" / "世界底子.md").write_text("底子", encoding="utf-8")
    (docs / "事").mkdir()
    (docs / "事" / "文化祭.md").write_text("筹备中", encoding="utf-8")

    listed = await outside.get(LISTING, params={"under": "设定"})
    assert listed.json()["entries"] == ["设定/世界底子.md"]


async def test_an_empty_tree_lists_as_empty_not_as_an_error(outside, docs):
    """树是空的不是出错 —— 这是一条新泳道的正常初始状态。"""
    listed = await outside.get(LISTING)
    assert listed.status_code == 200, listed.text
    assert listed.json()["entries"] == []
    assert listed.json()["mounted"] is True


async def test_a_missing_mount_is_told_apart_from_an_empty_tree(
    outside, tmp_path, monkeypatch
):
    """卷没挂上和树是空的是两种处境，程序那一侧也要分得开。"""
    monkeypatch.setenv("WORLD_DOCS_DIR", str(tmp_path / "never-mounted"))
    monkeypatch.setenv("LANE", LANE)

    listed = await outside.get(LISTING)
    assert listed.status_code == 200, listed.text
    assert listed.json()["entries"] == []
    assert listed.json()["mounted"] is False


async def test_listing_says_which_kind_of_wrong_under_it_got(outside, docs):
    """``under`` 写错的三种，三个不同的码。

    尤其是指到一份文档那一种：``rglob`` 对一个文件交回空，不说一声的话"路径写错了"
    长得跟"这个目录是空的"一模一样，而这两种的下一步完全不同。
    """
    (docs / "厨房.md").write_text("灶台靠窗。", encoding="utf-8")

    a_document = await outside.get(LISTING, params={"under": "厨房.md"})
    not_there = await outside.get(LISTING, params={"under": "没有这个目录"})
    escaping = await outside.get(LISTING, params={"under": "../"})

    assert [
        a_document.status_code,
        not_there.status_code,
        escaping.status_code,
    ] == [400, 404, 400], (a_document.text, not_there.text, escaping.text)


async def test_a_document_longer_than_the_model_cap_comes_back_whole(outside, docs):
    """读给外面的那一份不截断。

    给模型的那只手会截，因为一份跑飞的文档能把整轮上下文顶掉。这一侧不能照抄：截过
    的正文配的是整份的指纹，拿回来改完写回去，指纹**对得上**，而尾巴被静默删掉了——
    那正是指纹这道门要挡的事。
    """
    whole = "很长的一段。" * 4000
    (docs / "长.md").write_text(whole, encoding="utf-8")

    read_back = await outside.get(DOCUMENT, params={"path": "长.md"})
    assert read_back.json()["content"] == whole
    assert read_back.json()["fingerprint"] == _fingerprint(whole)


# --------------------------------------------------------------------------
# 二 · 指纹语义跟那五只手一字不差
#
# 每一条都要连"盘上一个字没动"一起断言：只看状态码的话，一个"拒绝之后照样写下去"
# 的实现照样绿。
# --------------------------------------------------------------------------


async def test_overwriting_an_existing_document_blind_is_refused(outside, docs):
    (docs / "厨房.md").write_text("灶台靠窗。", encoding="utf-8")

    refused = await outside.put(
        DOCUMENT, json={"path": "厨房.md", "content": "全新的一版"}
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["outcome"] == "no_fingerprint"
    assert (docs / "厨房.md").read_text(encoding="utf-8") == "灶台靠窗。"


async def test_a_stale_fingerprint_does_not_get_to_overwrite(outside, docs):
    (docs / "厨房.md").write_text("第二版", encoding="utf-8")

    refused = await outside.put(
        DOCUMENT,
        json={
            "path": "厨房.md",
            "content": "照着第一版改的",
            "fingerprint": _fingerprint("第一版"),
        },
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["outcome"] == "stale_fingerprint"
    assert (docs / "厨房.md").read_text(encoding="utf-8") == "第二版"


async def test_writing_back_a_document_that_was_deleted_is_refused(outside, docs):
    """带着指纹写回一份已经不在了的文档：那条线多半已经走完了。"""
    refused = await outside.put(
        DOCUMENT,
        json={
            "path": "走完的线.md",
            "content": "写回来",
            "fingerprint": _fingerprint("第一版"),
        },
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["outcome"] == "gone"
    assert not (docs / "走完的线.md").exists()


async def test_deleting_an_existing_document_blind_is_refused(outside, docs):
    """删掉跟整份重写同一条规矩，不能比它松 —— 删掉更狠。"""
    (docs / "厨房.md").write_text("灶台靠窗。", encoding="utf-8")

    refused = await outside.delete(DOCUMENT, params={"path": "厨房.md"})
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["outcome"] == "no_fingerprint"
    assert (docs / "厨房.md").read_text(encoding="utf-8") == "灶台靠窗。"


async def test_a_stale_fingerprint_does_not_get_to_delete(outside, docs):
    (docs / "厨房.md").write_text("第二版", encoding="utf-8")

    refused = await outside.delete(
        DOCUMENT,
        params={"path": "厨房.md", "fingerprint": _fingerprint("第一版")},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["outcome"] == "stale_fingerprint"
    assert (docs / "厨房.md").read_text(encoding="utf-8") == "第二版"


async def test_deleting_a_document_that_is_already_gone_is_refused(outside, docs):
    refused = await outside.delete(
        DOCUMENT,
        params={"path": "已经没了.md", "fingerprint": _fingerprint("第一版")},
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"]["outcome"] == "gone"


async def test_a_directory_is_not_a_document(outside, docs):
    """目录不能当成一份文档读、写、删。"""
    (docs / "设定").mkdir()

    read_back = await outside.get(DOCUMENT, params={"path": "设定"})
    written = await outside.put(
        DOCUMENT, json={"path": "设定", "content": "把整棵树变成一个文件"}
    )
    deleted = await outside.delete(
        DOCUMENT, params={"path": "设定", "fingerprint": _fingerprint("设定")}
    )

    assert [read_back.status_code, written.status_code, deleted.status_code] == [
        400,
        400,
        400,
    ], (read_back.text, written.text, deleted.text)
    assert (docs / "设定").is_dir()


async def test_an_oversized_write_is_refused_not_truncated(outside, docs):
    """超上限一个字都不写：落进去的是残篇而调用方以为写全了。"""
    refused = await outside.put(
        DOCUMENT, json={"path": "长.md", "content": "字" * 12_001}
    )
    assert refused.status_code == 400, refused.text
    assert not (docs / "长.md").exists()


# --------------------------------------------------------------------------
# 三 · 路径逃不出根目录
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attempt",
    [
        "../outside.md",
        "设定/../../outside.md",
        "/etc/passwd",
        "../world-evil/x.md",
        "有\x00空字节.md",
        "",
    ],
    ids=["parent", "nested-parent", "absolute", "sibling-prefix", "nul", "empty"],
)
async def test_no_path_escapes_the_root(outside, docs, tmp_path, attempt):
    """读、写、删三只手都拒，而且写那一只没有在树外面留下任何东西。"""
    read_back = await outside.get(DOCUMENT, params={"path": attempt})
    written = await outside.put(
        DOCUMENT, json={"path": attempt, "content": "写到树外面去"}
    )
    deleted = await outside.delete(
        DOCUMENT, params={"path": attempt, "fingerprint": _fingerprint("x")}
    )

    assert [read_back.status_code, written.status_code, deleted.status_code] == [
        400,
        400,
        400,
    ], (attempt, read_back.text, written.text, deleted.text)
    assert sorted(p.name for p in tmp_path.rglob("*.md")) == []


async def test_a_symlink_pointing_outside_the_root_is_refused(outside, docs, tmp_path):
    """路径本身是干净的，解析完才在外面 —— 判归属靠父目录包含关系，不是字符串前缀。"""
    outside_file = tmp_path / "outside.md"
    outside_file.write_text("树外面的东西", encoding="utf-8")
    (docs / "看起来在里面.md").symlink_to(outside_file)

    read_back = await outside.get(DOCUMENT, params={"path": "看起来在里面.md"})
    written = await outside.put(
        DOCUMENT, json={"path": "看起来在里面.md", "content": "盖掉树外面那一份"}
    )

    assert [read_back.status_code, written.status_code] == [400, 400], (
        read_back.text,
        written.text,
    )
    assert outside_file.read_text(encoding="utf-8") == "树外面的东西"


# --------------------------------------------------------------------------
# 四 · 每个回答都带着执行它的那个进程自己的泳道
#
# 泳道不在注册表里时请求静默落 prod 并返回 200 —— 对调用方来说这跟送达成功长得一模
# 一样，而这组接口的每一次写入都不可逆。自报的落点是"这次改的是我以为的那棵树"的
# 唯一证据，所以它必须从进程自己的部署环境读，**回显请求里的任何东西等于什么都没验**。
# --------------------------------------------------------------------------


async def test_every_answer_says_which_lane_actually_ran_it(outside, docs, monkeypatch):
    """**每一种**回答都带着泳道 —— 这四条端点答得出来的七个状态码一个不落。

    原来只数了成功、409、400、404 这四种，而那四种全部产生在 handler 里。剩下三种
    （401、503，以及参数反序列化失败的 422）产生在 handler **之外**，漏掉它们的话，
    调用方恰恰在被拒的时候不知道是哪个进程拒的 —— 而泳道不在注册表里时请求会静默落到
    prod 的 pod 上，"我刚才改的是哪棵树"这条链路上只有这一个证据。
    """
    (docs / "厨房.md").write_text("灶台靠窗。", encoding="utf-8")

    listed = await outside.get(LISTING)
    made = await outside.put(DOCUMENT, json={"path": "新的.md", "content": "新的"})
    conflict = await outside.put(
        DOCUMENT, json={"path": "厨房.md", "content": "盲写"}
    )
    bad = await outside.get(DOCUMENT, params={"path": "../outside.md"})
    missing = await outside.get(DOCUMENT, params={"path": "没有这一份.md"})
    unparseable = await outside.get(LISTING, params={"lane": "prod"})
    refused = await outside.get(
        LISTING, headers={"Authorization": "Bearer not-the-key"}
    )

    answers = (listed, made, conflict, bad, missing, unparseable, refused)
    assert [a.status_code for a in answers] == [200, 200, 409, 400, 404, 422, 401], [
        a.text for a in answers
    ]
    assert [listed.json()["lane"], made.json()["lane"]] == [LANE, LANE]
    for refusal in (conflict, bad, missing, unparseable, refused):
        assert refusal.json()["detail"]["lane"] == LANE, refusal.text

    # 第七种：进程根本没配凭据，门自己开不了。答不上来的时候仍然要说是谁答不上来。
    monkeypatch.setattr(
        config,
        "settings",
        dataclasses.replace(config.settings, inner_http_secret=None),
    )
    no_lock = await outside.get(LISTING)
    assert no_lock.status_code == 503, no_lock.text
    assert no_lock.json()["detail"]["lane"] == LANE, no_lock.text


async def test_the_lane_follows_the_process_not_the_request(
    outside, tmp_path, monkeypatch
):
    """换一条部署泳道，自报的跟着换；换回没有泳道的进程，自报的是 prod。"""
    monkeypatch.setenv("WORLD_DOCS_DIR", str(tmp_path / "mount"))

    monkeypatch.setenv("LANE", "ppe-somewhere-else")
    elsewhere = await outside.get(LISTING)
    assert elsewhere.json()["lane"] == "ppe-somewhere-else"

    monkeypatch.delenv("LANE")
    nowhere = await outside.get(LISTING)
    assert nowhere.json()["lane"] == "prod"


async def test_nothing_in_the_request_can_say_which_lane_to_write(outside, docs):
    """请求里指定不了泳道，也回显不出来。

    header 这一层压根到不了 handler；查询串里塞一个 lane 会被当成多余字段拒掉 ——
    "接口里没有任何地方能指定泳道"在结构上成立，不是靠 handler 记得别去读它。
    """
    with_headers = await outside.get(
        LISTING, headers={"x-lane": "prod", "x-ctx-lane": "prod"}
    )
    assert with_headers.json()["lane"] == LANE

    with_param = await outside.get(LISTING, params={"lane": "prod"})
    assert with_param.status_code == 422, with_param.text

    in_the_body = await outside.put(
        DOCUMENT, json={"path": "新的.md", "content": "新的", "lane": "prod"}
    )
    assert in_the_body.status_code == 422, in_the_body.text
    assert not (docs / "新的.md").exists()


# --------------------------------------------------------------------------
# 五 · 外部写入和 world 的轮次抢同一把锁
#
# 这一层有两把锁，管的不是同一头，所以两把各有一条用例按着：
#
#   * ``hold``（asyncio 锁）管协程这一侧的排队，顺带给出 ``HELD_SECONDS`` 那个上限；
#   * ``_file_lock``（threading 锁）管线程这一侧，**覆盖真正碰盘的那一段** —— 协程被
#     取消时它不放开，那正是它存在的理由。
#
# 只按住其中一把都验不出全部：拆掉线程锁，下面第一条照样绿（外部请求还是会排在协程
# 锁上）；拆掉协程锁，前两条也照样绿（线程锁仍然把碰盘那一段串起来）。所以第三条数的
# 是"排队的人占不占线程"——那是协程锁唯一管得到的事。
# --------------------------------------------------------------------------


@pytest.fixture
def stall_the_first_landing(monkeypatch):
    """让第一次落盘停在写下去之前，等放行。

    停在 ``write_text`` 里 = 停在两把锁**都已经拿到**、指纹也已经比过之后的那一刻，
    正是"另一个写者这时候插进来会怎样"要问的那一刻。

    要先 ``arm`` 才开始拦，不然用例自己摆种子文档的那几次写入就把这一次用掉了。
    """
    armed = threading.Event()
    reached = threading.Event()
    release = threading.Event()
    original = Path.write_text

    def stall(self, *args, **kwargs):
        if armed.is_set() and not reached.is_set():
            reached.set()
            assert release.wait(10), "第一次落盘等放行等超时了"
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", stall)
    yield armed, reached, release
    release.set()


async def test_an_outside_rewrite_queues_behind_a_round_that_is_already_inside(
    outside, docs, stall_the_first_landing
):
    """world 的一轮正在落盘，外部写入**排队等**，不跟它并行写盘。

    两个人各写各的的话，盘上最后是谁的内容全看线程调度，而两边都拿到一句"写好了"。
    """
    armed, reached, release = stall_the_first_landing
    (docs / "厨房.md").write_text("第一版", encoding="utf-8")
    fp = _fingerprint("第一版")
    armed.set()

    a_round = asyncio.create_task(
        write_document.invoke(
            {"path": "厨房.md", "content": "world 写的", "fingerprint": fp}
        )
    )
    await _until(reached)

    from_outside = asyncio.create_task(
        outside.put(
            DOCUMENT,
            json={"path": "厨房.md", "content": "外面写的", "fingerprint": fp},
        )
    )
    await asyncio.sleep(0.3)

    assert not from_outside.done(), (
        "外部写入没有排队，它跟 world 的那一轮同时在碰同一份文档"
    )
    assert (docs / "厨房.md").read_text(encoding="utf-8") == "第一版"

    release.set()
    await a_round
    answer = await from_outside

    assert answer.status_code == 409, answer.text
    assert answer.json()["detail"]["outcome"] == "stale_fingerprint"
    assert (docs / "厨房.md").read_text(encoding="utf-8") == "world 写的"


async def test_a_cancelled_round_cannot_land_on_top_of_an_outside_write(
    outside, docs, stall_the_first_landing
):
    """轮次的协程被取消之后，它那个线程还停在落盘前 —— 外部写入仍然被按住。

    取消只到得了协程：``hold`` 那把 asyncio 锁跟着放开，线程一行都停不下来。这时候
    只剩线程锁挡在中间。它要是不在，外部写入会读到**旧**的那一版、指纹对得上、写下去、
    **拿到一句写好了**，然后轮次那个线程才落盘，把它那一版整个盖掉。
    """
    armed, reached, release = stall_the_first_landing
    (docs / "厨房.md").write_text("第一版", encoding="utf-8")
    fp = _fingerprint("第一版")
    armed.set()

    a_round = asyncio.create_task(
        write_document.invoke(
            {"path": "厨房.md", "content": "world 写的", "fingerprint": fp}
        )
    )
    await _until(reached)

    a_round.cancel()
    with pytest.raises(asyncio.CancelledError):
        await a_round

    from_outside = asyncio.create_task(
        outside.put(
            DOCUMENT,
            json={"path": "厨房.md", "content": "外面写的", "fingerprint": fp},
        )
    )
    await asyncio.sleep(0.3)
    assert not from_outside.done(), (
        "协程锁一放开外部写入就进去了 —— 碰盘那一段没有被线程锁按住"
    )

    release.set()
    answer = await from_outside

    assert answer.status_code == 409, answer.text
    assert answer.json()["detail"]["outcome"] == "stale_fingerprint"
    assert (docs / "厨房.md").read_text(encoding="utf-8") == "world 写的", (
        "外面那一侧拿到的是成功确认，盘上却是被取消的那一轮的内容"
    )


async def test_one_stalled_document_does_not_stop_the_rest_of_the_tree(
    outside, docs, stall_the_first_landing
):
    """一份文档卡住的时候，排在它后面的外部写入**不占线程**，别的文档照常写得进去。

    这条数的是协程锁唯一管得到的那件事。等在线程锁上的那个线程真的占着一个 executor
    槽，等在 asyncio 锁上不占 —— 少了外面那把，同一份文档的每一个等待者都会占掉一个
    槽，一份卡住的文档就能把整棵树的落盘拖停。
    """
    armed, reached, release = stall_the_first_landing
    (docs / "厨房.md").write_text("第一版", encoding="utf-8")
    (docs / "客厅.md").write_text("第一版", encoding="utf-8")
    fp = _fingerprint("第一版")
    armed.set()

    pool = ThreadPoolExecutor(max_workers=2)
    asyncio.get_running_loop().set_default_executor(pool)
    try:
        first = asyncio.create_task(
            outside.put(
                DOCUMENT,
                json={"path": "厨房.md", "content": "甲", "fingerprint": fp},
            )
        )
        await _until(reached)
        second = asyncio.create_task(
            outside.put(
                DOCUMENT,
                json={"path": "厨房.md", "content": "乙", "fingerprint": fp},
            )
        )
        await asyncio.sleep(0.3)

        elsewhere = await asyncio.wait_for(
            outside.put(
                DOCUMENT,
                json={"path": "客厅.md", "content": "丙", "fingerprint": fp},
            ),
            timeout=5,
        )
        assert elsewhere.status_code == 200, elsewhere.text
        assert (docs / "客厅.md").read_text(encoding="utf-8") == "丙"

        release.set()
        await first
        await second
    finally:
        release.set()
        pool.shutdown(wait=False)


# --------------------------------------------------------------------------
# 六 · 外面这一侧没有第二条写入路径
# --------------------------------------------------------------------------


def test_the_outside_handlers_never_touch_the_disk_themselves():
    """handler 里一行碰盘的代码都没有。

    自己开一条写入路径的话，那条路径不在锁里也不认指纹，而它跟 world 的五只手改的是
    同一棵树 —— 上面五条用例一条都拦不住它，因为它们验的是这四个端点走的那一条。
    """
    import app.nodes.world_documents as handlers

    source = Path(handlers.__file__).read_text(encoding="utf-8")
    touching_disk = [
        name
        for name in (
            "write_text",
            "read_text",
            "unlink",
            "mkdir",
            "rmtree",
            "os.remove",
            "open(",
        )
        if name in source
    ]
    assert touching_disk == [], (
        f"外面这一侧自己碰盘了：{touching_disk} —— 写入只能走文档层那一条加锁路径。"
    )
