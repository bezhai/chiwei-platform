"""世界文档层 —— world 手里那棵文档树。

这一份守的是八条，每条都是"错了之后不会当场看出来"的那种：

一 · **路径逃不出根目录。** 这是安全边界，不是整洁问题。同一个卷上还挂着
   ``/data/skills``，而那里的 ``scripts/`` 会被沙箱软链进执行目录**执行** —— 一条能
   写出根目录的路径就是一条 RCE。所以逃逸按攻击面一条条列，不抽成一个。
二 · **读和写都有上界，截了要说出来。** world 每轮 ``read`` 一份文档，一份跑飞的文档
   能把整轮上下文顶掉；而读到一份"看起来完整其实被截过"的设定，它会当成世界的全貌。
三 · **``edit`` 的匹配串不唯一就报错。** 静默只改第一处 = 同一份文档里留下两句互相
   矛盾的说法，而 world 以为自己改完了。
四 · **失败要交回一句它能照着做的话**，不是 traceback。路径写错和树是空的是两种处境
   （同 :mod:`app.living.guides` 那条），给的话也得是两句。
五 · **根目录只由环境变量说了算。** 泳道隔离是部署事实：prod 和 coe 挂不同的子目录，
   代码里没有任何按泳道拼路径的分支。
六 · **这几只手不能落到她手里。** 第六节那条边界：文档是 world 的工作产物，只以"她看
   到的东西"的形式到达她，她不读文档。
七 · **没有人的写入会被静默丢掉。** 两个写者同改一份，整份重写是后写覆盖先写，而
   ``edit`` 是 read-modify-write —— 两种都不报错。守它的是每份文档一把锁（同一瞬间）
   加破坏性操作前要带指纹（跨调用：读一次、想一会儿、再写回来或者删掉的那条真实
   路径）。**删掉跟整份重写同一条规矩**：只给重写设这道门的话，带着过期指纹去盖会被
   拦下来，什么都不带直接删却放行，而删掉更狠。
八 · **锁要按住真正碰盘的那一段，不是那个协程。** 碰盘跑在线程里，而取消只到得了
   协程 —— 协程一没锁就放开了，那个线程还停在写盘前，待会儿落的一笔会盖掉后面那个
   **已经拿到成功确认**的人。单进程就能复现。

**这些工具不绑轮次上下文，这是有意的。** 其余 living 工具要 ``moment_scope()`` 是因为
lane 决定它们写到哪条轴上；文档层的隔离来自挂载的根目录，时间和 persona 一样都不用。
要求一个用不到的 context 只会多一个保护不了任何东西的失败面。
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path

import pytest

from app.living.documents import (
    DOCS_DIR_ENV,
    DOCUMENT_CUT_MARK,
    DOCUMENT_FINGERPRINT_MARK,
    DOCUMENT_TOOLS,
    MAX_DOCUMENT_CHARS,
    MAX_LISTING_ENTRIES,
    _delete,
    _write,
    delete_document,
    documents_root,
    edit_document,
    fingerprint_of,
    list_documents,
    list_tree,
    read_document,
    resolve_within,
    write_document,
)

LANE = "coe-living"


@pytest.fixture
def docs(tmp_path, monkeypatch) -> Path:
    """一棵空的文档树，挂载点指到 tmp，泳道是 coe-living。"""
    monkeypatch.setenv(DOCS_DIR_ENV, str(tmp_path / "mount"))
    monkeypatch.setenv("LANE", LANE)
    root = tmp_path / "mount" / LANE
    root.mkdir(parents=True)
    return root


def _text(outcome) -> str:
    """工具交回来的那段话；是失败 outcome 的话取它的 message。"""
    if isinstance(outcome, dict):
        return outcome["message"]
    return outcome


def _body_and_fingerprint(read_back: str) -> tuple[str, str]:
    """把 ``read_document`` 交回来的那段话拆成正文和指纹。

    模型也只能从这儿拿到指纹（目录里没有，见第七节那条），所以用例走的是跟它一样的
    那条路：读一次，把末尾那串抄下来，再带着它写回去。
    """
    body, mark, tail = read_back.partition(f"\n\n【{DOCUMENT_FINGERPRINT_MARK}：")
    assert mark, f"读回来没带指纹，它拿不到就只能盲写：{read_back!r}"
    return body, tail.split("，")[0]


# --------------------------------------------------------------------------
# 一 · 路径逃不出根目录
# --------------------------------------------------------------------------


def test_a_plain_relative_path_lands_inside_the_root(docs):
    """正常路径落在根下面，这是其余用例的对照组。"""
    assert resolve_within(docs, "设定/这座城市.md") == docs / "设定/这座城市.md"


@pytest.mark.parametrize(
    "attempt",
    [
        "../outside.md",
        "设定/../../outside.md",
        "/etc/passwd",
        "//etc/passwd",
        "../world-evil/x.md",  # 兄弟目录：按字符串前缀判会放过去
        "设定/./../../etc/passwd",
    ],
    ids=[
        "parent",
        "nested-parent",
        "absolute",
        "double-slash-absolute",
        "sibling-prefix",
        "dot-then-parent",
    ],
)
def test_no_path_escapes_the_root(docs, attempt):
    """逃出根目录的一律拒绝 —— 同一个卷上挂着会被执行的东西。"""
    with pytest.raises(ValueError):
        resolve_within(docs, attempt)


def test_a_symlink_pointing_outside_is_refused(docs, tmp_path):
    """符号链接是第二条出路：路径本身干净，解析完却在外面。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("不该被读到", encoding="utf-8")
    (docs / "门").symlink_to(outside)

    with pytest.raises(ValueError):
        resolve_within(docs, "门/secret.md")


def test_a_symlink_into_a_sibling_directory_is_refused(docs, tmp_path):
    """根是 ``.../world`` 时，``.../world-evil`` 的**字符串前缀是匹配的**。

    这条是专门用来区分两种写法的：按「解析后的路径以根目录开头」判（monitor-dashboard
    那一份就是），兄弟目录会被放行；按父目录包含关系判才拦得住。上面那条
    ``../world-evil/x.md`` 区分不了 —— 它在 ``..`` 那一关就被挡下了，两种写法都过不去。
    """
    evil = tmp_path / "world-evil"
    evil.mkdir()
    (evil / "x.md").write_text("不该被读到", encoding="utf-8")
    (docs / "门").symlink_to(evil)

    with pytest.raises(ValueError):
        resolve_within(docs, "门/x.md")


@pytest.mark.parametrize("blank", ["", "   ", ".", "/", "./"])
def test_an_empty_path_is_not_the_root_itself(docs, blank):
    """没说要哪一份就是没说，不能悄悄解析成根目录自己。"""
    with pytest.raises(ValueError):
        resolve_within(docs, blank)


def test_a_nul_byte_is_refused(docs):
    """``\\0`` 在 C 那层截断路径，是一条绕过检查的老路子。"""
    with pytest.raises(ValueError):
        resolve_within(docs, "设定/a\x00.md")


def test_the_mount_point_comes_from_the_environment(docs):
    """挂载点是部署事实，不是代码里写死的。"""
    assert documents_root() == docs

    os.environ.pop(DOCS_DIR_ENV, None)
    assert documents_root() != docs, "环境变量没了却还指着同一处，那就不是它说了算"


def test_two_lanes_never_share_a_tree(tmp_path, monkeypatch):
    """泳道那一段是代码拼的，所以**忘不掉**。

    靠每条泳道各配一个环境变量的话，一条 coe 泳道忘了覆盖就直接写进 prod 的设定集：
    world 去改线上那棵树，没有任何报错，而那个卷是 hostPath，没有备份可以退回去。
    """
    monkeypatch.setenv(DOCS_DIR_ENV, str(tmp_path / "mount"))

    monkeypatch.setenv("LANE", "prod")
    prod = documents_root()
    monkeypatch.setenv("LANE", "coe-living")
    coe = documents_root()

    assert prod != coe
    assert prod.parent == coe.parent, "两条泳道该在同一个挂载点下面，各占一个目录"


def test_a_process_without_a_lane_lands_on_prod(tmp_path, monkeypatch):
    """拿不到泳道时落到 prod，不是落到空串。

    空串会拼出一条以 ``/`` 结尾的路径、跟挂载点自己重合，于是两条泳道又混在一起了 ——
    跟 :func:`app.living.clock.living_lane` 那条"空串会开一条影子轴"是同一个道理。
    """
    monkeypatch.setenv(DOCS_DIR_ENV, str(tmp_path / "mount"))
    monkeypatch.delenv("LANE", raising=False)

    assert documents_root() == tmp_path / "mount" / "prod"


def test_a_lane_that_has_never_been_written_reads_as_an_empty_tree(
    tmp_path, monkeypatch
):
    """**这条泳道还没写过** ≠ **卷没挂上**。一个照着写就行，一个要找运维。

    实测（coe-living，2026-09-14 22:20）：新泳道第一轮 world 调 ``list_documents``
    拿回一句「卷没挂上」，而卷挂得好好的，只是这条泳道底下一份文档都还没有 ——
    泳道那一段是代码拼出来的，谁也没建过那个目录。那句话是喂给模型的**假诊断**：
    它会据此认为文档这条路整个不通，然后一轮都不再碰。
    """
    monkeypatch.setenv(DOCS_DIR_ENV, str(tmp_path / "mount"))
    (tmp_path / "mount").mkdir()  # 卷挂上了
    monkeypatch.setenv("LANE", "coe-living")  # 但这条泳道一份都没写过

    listed = list_tree(documents_root())

    assert "卷没挂上" not in listed, f"卷挂着却说没挂上。拿到：\n{listed}"
    assert "空" in listed, f"该说这棵树是空的。拿到：\n{listed}"


def test_a_missing_mount_is_still_called_out_as_a_missing_mount(
    tmp_path, monkeypatch
):
    """卷真没挂上的时候那句话还得在 —— 上面那条不能把它一起改没了。"""
    monkeypatch.setenv(DOCS_DIR_ENV, str(tmp_path / "never-mounted"))
    monkeypatch.setenv("LANE", "coe-living")

    listed = list_tree(documents_root())

    assert "卷没挂上" in listed, (
        f"卷真的没挂上，却说成树是空的 —— world 会照着写，一轮的产出全丢在容器的"
        f"可写层里，重启就没了。拿到：\n{listed}"
    )


# --------------------------------------------------------------------------
# 二 · list：看得见结构，有上界
# --------------------------------------------------------------------------


async def test_an_empty_tree_says_so_instead_of_handing_back_nothing(docs):
    """空树和"列失败了"是两种处境，空串两种都像。"""
    listing = await list_documents.invoke({})

    assert isinstance(listing, str) and listing.strip()
    assert "空" in listing


async def test_the_listing_is_recursive_sorted_and_marks_directories(docs):
    """它靠这份清单决定读哪一份，所以结构要看得出来。"""
    (docs / "设定").mkdir()
    (docs / "设定" / "这座城市.md").write_text("城", encoding="utf-8")
    (docs / "人").mkdir()
    (docs / "人" / "林小满.md").write_text("人", encoding="utf-8")
    (docs / "当下").mkdir()  # 空目录也得看得见，不然它以为没这一层

    listing = await list_documents.invoke({})

    paths = [
        line.strip()
        for line in listing.splitlines()
        if "/" in line or line.strip().endswith(".md")
    ]
    assert "人/林小满.md" in paths
    assert "设定/这座城市.md" in paths
    assert any(p.rstrip("/") == "当下" for p in paths), f"空目录没列出来：{paths}"
    assert paths == sorted(paths), "顺序不稳定，它每轮看到的清单就不一样"


async def test_a_missing_root_says_the_volume_is_not_there(tmp_path, monkeypatch):
    """卷没挂上是运维状态，不是它填错了路径 —— 两句话不能是同一句。"""
    monkeypatch.setenv(DOCS_DIR_ENV, str(tmp_path / "从来没有过"))

    listing = await list_documents.invoke({})

    assert isinstance(listing, str) and listing.strip()
    assert "空" not in listing, "把「卷没挂上」说成了「树是空的」"


async def test_a_huge_tree_is_cut_and_says_it_was_cut(docs):
    """截了不说，它就以为自己看到了全部，然后据此判断该写哪一份。"""
    for i in range(MAX_LISTING_ENTRIES + 20):
        (docs / f"{i:04d}.md").write_text("x", encoding="utf-8")

    listing = await list_documents.invoke({})

    assert DOCUMENT_CUT_MARK in listing


async def test_listing_under_a_subdirectory_only_shows_that_subtree(docs):
    """按需读的前提是能只看一层，否则每轮都得吞下整棵树。"""
    (docs / "设定").mkdir()
    (docs / "设定" / "这座城市.md").write_text("城", encoding="utf-8")
    (docs / "人").mkdir()
    (docs / "人" / "林小满.md").write_text("人", encoding="utf-8")

    listing = await list_documents.invoke({"under": "人"})

    assert "林小满" in listing
    assert "这座城市" not in listing


# --------------------------------------------------------------------------
# 三 · read / write：逐字往返，有上界
# --------------------------------------------------------------------------


async def test_what_was_written_comes_back_verbatim(docs):
    """文档最终会变成她看到的东西，一个字都不能被改写。

    正文后面跟着的那截指纹（第七节）不算改写：它挂在末尾、拆得掉，正文本身逐字
    还是原来那一份。这条钉的就是"拆掉那截之后必须一个字不差"。
    """
    body = "厨房\n\n灶台靠窗，窗外是那条老街。傍晚有人在楼下喊「回家吃饭」。\n"

    await write_document.invoke({"path": "地方/家/厨房.md", "content": body})
    back = await read_document.invoke({"path": "地方/家/厨房.md"})

    assert _body_and_fingerprint(back)[0] == body
    assert (docs / "地方/家/厨房.md").read_text(encoding="utf-8") == body


async def test_writing_creates_the_parent_directories(docs):
    """目录结构由它自己维护，不该先建目录再写文件。"""
    await write_document.invoke({"path": "a/b/c/d.md", "content": "深"})

    assert (docs / "a/b/c/d.md").read_text(encoding="utf-8") == "深"


async def test_writing_again_replaces_the_whole_file(docs):
    """write 是整份重写；改一处要用 edit，两件事不能长一样。

    第二次要先读一遍再带着指纹写（第七节）—— 覆盖别人写过的东西之前先看看它现在
    写的是什么，这条路必须是通的。
    """
    await write_document.invoke({"path": "当下/线.md", "content": "第一版"})
    _, fp = _body_and_fingerprint(await read_document.invoke({"path": "当下/线.md"}))
    await write_document.invoke(
        {"path": "当下/线.md", "content": "第二版", "fingerprint": fp}
    )

    assert (docs / "当下/线.md").read_text(encoding="utf-8") == "第二版"


async def test_reading_a_missing_document_says_what_is_nearby(docs):
    """路径写错了要说得出附近有什么，不然它只能瞎猜第二次。"""
    (docs / "设定").mkdir()
    (docs / "设定" / "这座城市.md").write_text("城", encoding="utf-8")

    outcome = await read_document.invoke({"path": "设定/这作城市.md"})

    assert isinstance(outcome, dict), "读了一份不存在的文档却当成读成了"
    assert "这座城市" in _text(outcome)


async def test_reading_a_directory_is_not_silently_empty(docs):
    """目录读成空串，它会以为那一份是空的，然后一次 write 把它变成文件。"""
    (docs / "设定").mkdir()

    outcome = await read_document.invoke({"path": "设定"})

    assert isinstance(outcome, dict), f"读了一个目录却当成读成了：{outcome!r}"


async def test_an_oversized_document_is_cut_and_says_it_was_cut(docs):
    """读到一份被截过的设定却不知道，它会把残篇当成世界的全貌。"""
    (docs / "长.md").write_text("字" * (MAX_DOCUMENT_CHARS + 500), encoding="utf-8")

    body = await read_document.invoke({"path": "长.md"})

    assert DOCUMENT_CUT_MARK in body
    assert len(body) < MAX_DOCUMENT_CHARS + 500


async def test_an_oversized_write_is_refused_not_truncated(docs):
    """写截断 = 落盘的是残篇而它以为写全了。宁可这一次失败，让它重写短一点。"""
    outcome = await write_document.invoke(
        {"path": "长.md", "content": "字" * (MAX_DOCUMENT_CHARS + 1)}
    )

    assert isinstance(outcome, dict), "超长照写了"
    assert not (docs / "长.md").exists(), "拒了却还是落了盘"


async def test_a_written_document_shows_up_in_the_listing(docs):
    """写完看不见，等于下一轮它自己也找不回来。"""
    await write_document.invoke({"path": "当下/新的一条线.md", "content": "线"})
    listing = await list_documents.invoke({})

    assert "当下/新的一条线.md" in listing


# --------------------------------------------------------------------------
# 四 · edit：匹配串必须唯一
# --------------------------------------------------------------------------


async def test_editing_replaces_the_one_match(docs):
    (docs / "地方").mkdir()
    (docs / "地方" / "厨房.md").write_text("灶台靠窗，窗外下着雨。", encoding="utf-8")

    await edit_document.invoke(
        {"path": "地方/厨房.md", "find": "窗外下着雨", "replace": "窗外天晴了"}
    )

    assert (
        docs / "地方/厨房.md"
    ).read_text(encoding="utf-8") == "灶台靠窗，窗外天晴了。"


async def test_editing_with_no_match_fails_loudly(docs):
    """没改成却回一句成功，它就以为文档已经是新的了。"""
    (docs / "厨房.md").write_text("灶台靠窗。", encoding="utf-8")

    outcome = await edit_document.invoke(
        {"path": "厨房.md", "find": "不存在的句子", "replace": "x"}
    )

    assert isinstance(outcome, dict), "没命中却当成改成了"
    assert (docs / "厨房.md").read_text(encoding="utf-8") == "灶台靠窗。"


async def test_editing_an_ambiguous_match_fails_instead_of_taking_the_first(docs):
    """只改第一处 = 同一份文档里留下两句互相矛盾的说法。"""
    (docs / "厨房.md").write_text("在下雨。中间。在下雨。", encoding="utf-8")

    outcome = await edit_document.invoke(
        {"path": "厨房.md", "find": "在下雨", "replace": "天晴了"}
    )

    assert isinstance(outcome, dict), "命中两处却挑了第一处"
    assert "2" in _text(outcome), f"没说命中了几处：{_text(outcome)!r}"
    assert (
        docs / "厨房.md"
    ).read_text(encoding="utf-8") == "在下雨。中间。在下雨。"


async def test_editing_with_an_empty_find_is_refused(docs):
    """空串在每个位置都命中，那不是一次替换。"""
    (docs / "厨房.md").write_text("灶台靠窗。", encoding="utf-8")

    outcome = await edit_document.invoke({"path": "厨房.md", "find": "", "replace": "x"})

    assert isinstance(outcome, dict)
    assert (docs / "厨房.md").read_text(encoding="utf-8") == "灶台靠窗。"


async def test_editing_a_missing_document_fails_loudly(docs):
    outcome = await edit_document.invoke(
        {"path": "没有这一份.md", "find": "a", "replace": "b"}
    )

    assert isinstance(outcome, dict)


# --------------------------------------------------------------------------
# 五 · delete：一条线走完了就是把文件删掉
# --------------------------------------------------------------------------


async def test_deleting_removes_it_from_the_listing(docs):
    """第一节说一条线走完就是删掉文件 —— 没有这只手，文档只会越堆越多。"""
    await write_document.invoke({"path": "当下/文化祭.md", "content": "线"})
    await delete_document.invoke(
        {"path": "当下/文化祭.md", "fingerprint": fingerprint_of("线")}
    )
    listing = await list_documents.invoke({})

    assert "文化祭" not in listing
    assert not (docs / "当下/文化祭.md").exists()


async def test_deleting_a_missing_document_fails_loudly(docs):
    outcome = await delete_document.invoke({"path": "没有这一份.md"})

    assert isinstance(outcome, dict)


async def test_deleting_a_directory_is_refused(docs):
    """删一份文档和删掉整个「设定/」是两件事，不能是同一只手。"""
    (docs / "设定").mkdir()
    (docs / "设定" / "这座城市.md").write_text("城", encoding="utf-8")

    outcome = await delete_document.invoke({"path": "设定"})

    assert isinstance(outcome, dict), "一只手就把整个目录端了"
    assert (docs / "设定/这座城市.md").exists()


# --------------------------------------------------------------------------
# 六 · 逃逸经过工具那一层同样过不去；这几只手不在她手里
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "which,args",
    [
        ("read", {"path": "../outside.md"}),
        ("write", {"path": "../outside.md", "content": "x"}),
        ("edit", {"path": "../outside.md", "find": "a", "replace": "b"}),
        ("delete", {"path": "../outside.md"}),
        ("list", {"under": "../"}),
    ],
    ids=["read", "write", "edit", "delete", "list"],
)
async def test_every_tool_refuses_a_path_that_escapes(docs, tmp_path, which, args):
    """纯函数挡住了不等于工具挡住了 —— 五只手各走一遍。"""
    tool = {
        "read": read_document,
        "write": write_document,
        "edit": edit_document,
        "delete": delete_document,
        "list": list_documents,
    }[which]
    (tmp_path / "outside.md").write_text("不该被碰到", encoding="utf-8")

    outcome = await tool.invoke(args)

    assert isinstance(outcome, dict), f"{which} 放行了一条逃逸路径：{outcome!r}"
    assert (tmp_path / "outside.md").read_text(encoding="utf-8") == "不该被碰到"


def test_the_five_hands_are_registered_together():
    assert DOCUMENT_TOOLS == [
        list_documents,
        read_document,
        write_document,
        edit_document,
        delete_document,
    ]


def test_she_does_not_have_any_of_these_hands():
    """第六节那条边界：文档是 world 的工作产物，她不读文档。

    她该看到的是"你走进厨房，桌上还堆着没洗的碗"，不是一份可以 grep 的设定集。
    这五只手一旦落进 ``MOMENT_TOOLS``，整条边界就没了，而且不会有任何报错。
    """
    from app.living.moment import MOMENT_TOOLS

    overlap = [t for t in DOCUMENT_TOOLS if t in MOMENT_TOOLS]
    assert not overlap, f"她手里有文档工具：{[t.definition.name for t in overlap]}"



# --------------------------------------------------------------------------
# 七 · 覆盖一份已经存在的文档要带上指纹（跨调用的 read-modify-write）
# --------------------------------------------------------------------------


async def test_reading_hands_back_the_fingerprint_of_what_it_read(docs):
    """指纹跟着正文一起交回去 —— 它没有第二个地方能拿到。"""
    (docs / "厨房.md").write_text("灶台靠窗。", encoding="utf-8")

    body, fp = _body_and_fingerprint(await read_document.invoke({"path": "厨房.md"}))

    assert body == "灶台靠窗。"
    assert fp == fingerprint_of("灶台靠窗。")


async def test_a_document_that_does_not_exist_yet_needs_no_fingerprint(docs):
    """第一次写下一份不是覆盖 —— 没有谁的写入会被它吃掉，不该多一道门。"""
    outcome = await write_document.invoke({"path": "当下/新的线.md", "content": "线"})

    assert not isinstance(outcome, dict), f"新写一份被拒了：{outcome!r}"
    assert (docs / "当下/新的线.md").read_text(encoding="utf-8") == "线"


async def test_overwriting_an_existing_document_blind_is_refused(docs):
    """没看过现在写的是什么就整份换掉 = 把别人写的那一版静默丢掉。"""
    (docs / "厨房.md").write_text("灶台靠窗。", encoding="utf-8")

    outcome = await write_document.invoke({"path": "厨房.md", "content": "全新的一版"})

    assert isinstance(outcome, dict), "没带指纹就把一份已经存在的文档整份换掉了"
    assert "read_document" in _text(outcome), (
        f"拒了却没说该怎么办，它只会原样再来一次：{_text(outcome)!r}"
    )
    assert (docs / "厨房.md").read_text(encoding="utf-8") == "灶台靠窗。"


async def test_the_fingerprint_a_read_handed_back_lets_the_overwrite_through(docs):
    """读一次、照着末尾那串写回去 —— 这条路不通的话它一份都改不动。"""
    (docs / "厨房.md").write_text("灶台靠窗。", encoding="utf-8")
    _, fp = _body_and_fingerprint(await read_document.invoke({"path": "厨房.md"}))

    outcome = await write_document.invoke(
        {"path": "厨房.md", "content": "灶台靠窗，窗外下着雨。", "fingerprint": fp}
    )

    assert not isinstance(outcome, dict), f"带着刚读到的指纹还是被拒了：{outcome!r}"
    assert (docs / "厨房.md").read_text(encoding="utf-8") == "灶台靠窗，窗外下着雨。"


async def test_a_write_hands_back_the_fingerprint_it_just_created(docs):
    """刚写完的那一版它自己就是作者，接着改不该再逼它读一遍。"""
    said = await write_document.invoke({"path": "厨房.md", "content": "第一版"})

    assert fingerprint_of("第一版") in said, f"写完没说新指纹：{said!r}"

    again = await write_document.invoke(
        {
            "path": "厨房.md",
            "content": "第二版",
            "fingerprint": fingerprint_of("第一版"),
        }
    )

    assert not isinstance(again, dict), f"写完交回来的指纹自己不认：{again!r}"
    assert (docs / "厨房.md").read_text(encoding="utf-8") == "第二版"


async def test_a_fingerprint_that_went_stale_is_refused_and_says_to_read_again(docs):
    """**跨调用的 read-modify-write** —— 读完、中间别人改了、它照着旧正文写回来。

    这是真实的模型工作路径：``read_document`` 一次、想一会儿、再 ``write_document``。
    那中间没有任何东西拦得住，覆盖过去就是把别人写的整段丢掉，而且两边都不报错。
    """
    (docs / "当下").mkdir()
    (docs / "当下/文化祭.md").write_text("筹备中。", encoding="utf-8")
    _, stale = _body_and_fingerprint(
        await read_document.invoke({"path": "当下/文化祭.md"})
    )

    # 另一个写者在这中间落了一版
    (docs / "当下/文化祭.md").write_text("筹备中。林小满负责舞台。", encoding="utf-8")

    outcome = await write_document.invoke(
        {
            "path": "当下/文化祭.md",
            "content": "筹备中。已经定好日子。",
            "fingerprint": stale,
        }
    )

    assert isinstance(outcome, dict), "拿着过期的指纹把别人刚写的那一版盖掉了"
    assert "read_document" in _text(outcome), (
        f"拒了却没说该重新读一遍：{_text(outcome)!r}"
    )
    assert (
        docs / "当下/文化祭.md"
    ).read_text(encoding="utf-8") == "筹备中。林小满负责舞台。"


async def test_a_fingerprint_for_a_document_that_was_deleted_is_refused(docs):
    """删掉也是一次写入：一条线走完了，不能被一次过期的覆盖写回来。"""
    (docs / "当下").mkdir()
    (docs / "当下/文化祭.md").write_text("筹备中。", encoding="utf-8")
    _, stale = _body_and_fingerprint(
        await read_document.invoke({"path": "当下/文化祭.md"})
    )
    await delete_document.invoke({"path": "当下/文化祭.md", "fingerprint": stale})

    outcome = await write_document.invoke(
        {"path": "当下/文化祭.md", "content": "筹备中。", "fingerprint": stale}
    )

    assert isinstance(outcome, dict), "那条线已经走完了，却被一次过期的覆盖写了回来"
    assert not (docs / "当下/文化祭.md").exists()


async def test_deleting_an_existing_document_blind_is_refused(docs):
    """**删掉比覆盖更狠，不能反而更容易。**

    只给 write 设指纹的话，状态是这样的：带着一串过期指纹去覆盖会被拦下来，什么都
    不带直接删却一路放行 —— 而删掉是把那一份整个带走，连"盖过去的那一版"都不剩。

    真实路径跟覆盖那条一模一样：读到旧的那一版、想一会儿、决定这条线走完了；那中间
    别人刚把它更新过，删下去就是把那一段一起带走，两边都不知道。
    """
    (docs / "厨房.md").write_text("灶台靠窗。", encoding="utf-8")

    outcome = await delete_document.invoke({"path": "厨房.md"})

    assert isinstance(outcome, dict), "没看过它现在写的是什么就把它整份删了"
    assert "read_document" in _text(outcome), (
        f"拒了却没给出路，它只会原样再来一次：{_text(outcome)!r}"
    )
    assert (docs / "厨房.md").exists(), "拒了却还是删掉了"


async def test_the_fingerprint_a_read_handed_back_lets_the_delete_through(docs):
    """读一遍、带着末尾那串来删 —— 这条路不通的话一条线都走不完。"""
    (docs / "厨房.md").write_text("灶台靠窗。", encoding="utf-8")
    _, fp = _body_and_fingerprint(await read_document.invoke({"path": "厨房.md"}))

    outcome = await delete_document.invoke({"path": "厨房.md", "fingerprint": fp})

    assert not isinstance(outcome, dict), f"带着刚读到的指纹还是删不掉：{outcome!r}"
    assert not (docs / "厨房.md").exists()


async def test_a_stale_fingerprint_does_not_get_to_delete_the_newer_version(docs):
    """读完、中间别人更新了、它照着旧的那一版决定删掉 —— 新的那一段会被一起带走。"""
    (docs / "当下").mkdir()
    (docs / "当下/文化祭.md").write_text("筹备中。", encoding="utf-8")
    _, stale = _body_and_fingerprint(
        await read_document.invoke({"path": "当下/文化祭.md"})
    )

    # 另一个写者在这中间落了一版
    (docs / "当下/文化祭.md").write_text("筹备中。林小满负责舞台。", encoding="utf-8")

    outcome = await delete_document.invoke(
        {"path": "当下/文化祭.md", "fingerprint": stale}
    )

    assert isinstance(outcome, dict), "拿着过期的指纹把别人刚写的那一版删掉了"
    assert "read_document" in _text(outcome), (
        f"拒了却没说该重新读一遍：{_text(outcome)!r}"
    )
    assert (
        docs / "当下/文化祭.md"
    ).read_text(encoding="utf-8") == "筹备中。林小满负责舞台。"


async def test_the_listing_does_not_hand_out_fingerprints(docs):
    """指纹是"我读过现在这一版"的凭据，不是版本号。

    目录里一并给出来的话，它不用读就能拿到一个能过关的指纹，于是盲写又通了 ——
    而这道门挡的正是"没看过现在写的是什么就整份换掉"。目录每轮必看，那也是白花的
    上下文。
    """
    (docs / "厨房.md").write_text("灶台靠窗。", encoding="utf-8")

    listing = await list_documents.invoke({})

    assert fingerprint_of("灶台靠窗。") not in listing
    assert DOCUMENT_FINGERPRINT_MARK not in listing


# --------------------------------------------------------------------------
# 八 · 两个写者同时动同一份（每份文档一把锁）
# --------------------------------------------------------------------------


@pytest.fixture
def slow_disk(monkeypatch):
    """每一次读文件前后各等 50 毫秒，好让两个写者一定叠在一起。

    打的是 ``Path.read_text``：``_write`` 比对指纹、``_edit`` 的 read-modify-write、
    ``_read_with_fingerprint`` 的正文和指纹都经过它。

    **前后都要等，缺一边就会得到一条假绿的用例**，两边各自实测过（把锁拆掉再跑）：

    * 只在读完之后等：``_write`` 的"读到写"之间被拉开了，两个写者确实会撞上；但一次
      ``read_document`` 里的两次读之间几乎没有间隔（第二次的实际读紧接着第一次返回），
      别人的写入插不进去，于是
      ``test_a_read_never_hands_back_a_fingerprint_for_another_version`` 照样绿。
    * 只在真正读之前等：两次读之间拉开了，但"读到写"之间又没了 —— 后起的那个写者读到
      的已经是前一个写下的内容，于是
      ``test_two_overwrites_at_once...`` 和 ``test_two_edits_at_once_both_land`` 照样绿。
    """
    original = Path.read_text

    def slow(self, *args, **kwargs):
        time.sleep(0.05)
        body = original(self, *args, **kwargs)
        time.sleep(0.05)
        return body

    monkeypatch.setattr(Path, "read_text", slow)


async def test_two_overwrites_at_once_leave_one_winner_and_a_loud_loser(
    docs, slow_disk
):
    """同一瞬间两个写者整份重写同一份：一个落地，另一个必须**知道自己没写成**。

    指纹是在锁外比对的话这一条就绿不了 —— 两边读到的都是同一版、都对得上，于是
    都写下去，先写的那一版连同它以为写成的那件事一起没了。
    """
    (docs / "厨房.md").write_text("灶台靠窗。", encoding="utf-8")
    _, fp = _body_and_fingerprint(await read_document.invoke({"path": "厨房.md"}))

    outcomes = await asyncio.gather(
        write_document.invoke(
            {"path": "厨房.md", "content": "甲写的", "fingerprint": fp}
        ),
        write_document.invoke(
            {"path": "厨房.md", "content": "乙写的", "fingerprint": fp}
        ),
    )

    failed = [o for o in outcomes if isinstance(o, dict)]
    assert len(failed) == 1, f"两个写者都以为自己写成了：{outcomes!r}"
    landed = [
        content
        for content, outcome in zip(("甲写的", "乙写的"), outcomes, strict=True)
        if not isinstance(outcome, dict)
    ]
    assert (docs / "厨房.md").read_text(encoding="utf-8") == landed[0]


async def test_two_edits_at_once_both_land(docs, slow_disk):
    """``edit`` 是 read-modify-write：两处各改各的，不能有一处被吃掉。

    没有锁的话两边读到同一份原文，后写的那一次把先写的那一处又盖回去了 —— 而且
    两边都回一句"改好了"。
    """
    (docs / "厨房.md").write_text("灶台靠窗，窗外下着雨。", encoding="utf-8")

    outcomes = await asyncio.gather(
        edit_document.invoke(
            {"path": "厨房.md", "find": "灶台靠窗", "replace": "灶台靠门"}
        ),
        edit_document.invoke(
            {"path": "厨房.md", "find": "窗外下着雨", "replace": "窗外天晴了"}
        ),
    )

    assert not [o for o in outcomes if isinstance(o, dict)], f"{outcomes!r}"
    assert (
        docs / "厨房.md"
    ).read_text(encoding="utf-8") == "灶台靠门，窗外天晴了。"


async def test_a_read_never_hands_back_a_fingerprint_for_another_version(
    docs, slow_disk
):
    """交回去的指纹必须配得上同一段正文。

    读正文和算指纹是两次触盘。中间被人写了一版的话，交回去的就是"A 的正文配 B 的
    指纹"——它照着改完带指纹写回来，一路畅通，而它改的是一份自己从没读过的正文。
    这比直接覆盖更坏：门开着，还看起来是关的。
    """
    (docs / "厨房.md").write_text("第一版", encoding="utf-8")

    reading = asyncio.create_task(read_document.invoke({"path": "厨房.md"}))
    await asyncio.sleep(0.01)  # 让读先进门
    await write_document.invoke(
        {"path": "厨房.md", "content": "第二版", "fingerprint": fingerprint_of("第一版")}
    )

    body, fp = _body_and_fingerprint(await reading)
    assert fp == fingerprint_of(body), f"交回去的指纹配的不是这段正文：{body!r} / {fp}"


async def test_two_documents_are_not_behind_the_same_lock(docs, monkeypatch):
    """一把锁一份文档，不是整棵树一把。

    整棵树一把的话，改「人/林小满」要排在改「地方/厨房」后面，而这两件事之间没有
    任何关系 —— 代价是 world 一轮里的每一次写入都得等上一次走完。

    这里用一个两人集合点：两次写入都必须走到"读完文件"那一步才放行。整棵树一把锁
    的话第二个人根本进不来，集合点超时。
    """
    for name in ("甲.md", "乙.md"):
        (docs / name).write_text("原文", encoding="utf-8")
    fp = fingerprint_of("原文")

    both_inside = threading.Barrier(2, timeout=5)
    armed = True
    original = Path.read_text

    def wait_for_the_other(self, *args, **kwargs):
        body = original(self, *args, **kwargs)
        if armed:
            both_inside.wait()
        return body

    monkeypatch.setattr(Path, "read_text", wait_for_the_other)
    outcomes = await asyncio.gather(
        write_document.invoke({"path": "甲.md", "content": "甲", "fingerprint": fp}),
        write_document.invoke({"path": "乙.md", "content": "乙", "fingerprint": fp}),
    )
    armed = False

    assert not [o for o in outcomes if isinstance(o, dict)], (
        f"两份互不相干的文档卡在同一把锁上，谁也等不到对方：{outcomes!r}"
    )


# --------------------------------------------------------------------------
# 九 · 协程被取消了，那个线程还在跑
#
# 落盘跑在 ``asyncio.to_thread`` 里。协程被取消时 ``async with hold`` 会退出、锁跟着
# 放开，**可那个线程一行都停不下来** —— 取消是协程这一侧的事，线程收不到。于是"锁按住
# 的那一段"和"真正碰盘的那一段"错开了，后者伸到锁外面去。
# --------------------------------------------------------------------------

# 被取消的那一次事后补写这件事，只能用"等一会儿看它有没有动静"来判。这个窗口给的是
# 前一个写者放开之后、排在它后面那个线程醒过来所需的时间 —— 微秒级的事，一秒是给
# 满载的 CI 留的余量。红的时候它会立刻返回，不花这一秒。
GHOST_WRITE_WINDOW = 1.0


class _StagedDisk:
    """把某一次落盘停在"写下去之前"，由用例说什么时候放行。

    ``slow_disk`` 那种靠 sleep 拉开窗口的写法在这儿不够用：要复现的是"锁已经放开、
    线程还停在写盘前"这个**确定的**时刻，sleep 只能让它大概率发生。
    """

    def __init__(self) -> None:
        self.armed = False
        self.at_the_brink = threading.Event()  # 头一次落盘走到了写下去之前
        self.let_it_land = threading.Event()   # 放它写下去
        self.landed = threading.Event()        # 它写完了
        self.wrote_again = threading.Event()   # 那之后还有人落过盘

    def arm(self) -> None:
        """用例摆好初始文件之后再开闸，免得把摆场景那几次写入也拦下来。"""
        self.armed = True


@pytest.fixture
def staged_disk(monkeypatch) -> _StagedDisk:
    stage = _StagedDisk()
    original = Path.write_text
    first = True

    def staged(self, *args, **kwargs):
        nonlocal first
        if not stage.armed:
            return original(self, *args, **kwargs)
        if first:
            first = False
            stage.at_the_brink.set()
            assert stage.let_it_land.wait(10), "头一次落盘一直没被放行"
            try:
                return original(self, *args, **kwargs)
            finally:
                stage.landed.set()
        stage.wrote_again.set()
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", staged)
    return stage


async def test_a_cancelled_overwrite_cannot_land_on_top_of_a_later_one(
    docs, staged_disk
):
    """取消之后那个线程补上的一笔，会盖掉后面那个**已经拿到确认**的人。

    时序：甲过了指纹检查、还没写盘时被取消，锁放开；乙拿到锁，盘上确实还是甲读到的
    那一版，于是指纹对得上、写下去、**拿到一句写好了**；甲的线程这时才落盘，把乙刚
    写的那一版盖掉。乙以为自己写成了，盘上是甲的内容 —— 而这道门存在的全部意义就是
    "没有人的写入会被静默丢掉"。

    所以锁要按住的是**真正碰盘的那一整段**，不是那个协程 —— 协程随时会没。
    """
    (docs / "厨房.md").write_text("原来那一版。", encoding="utf-8")
    fp = fingerprint_of("原来那一版。")
    staged_disk.arm()

    jia = asyncio.create_task(
        write_document.invoke(
            {"path": "厨房.md", "content": "甲写的", "fingerprint": fp}
        )
    )
    assert await asyncio.to_thread(staged_disk.at_the_brink.wait, 10), "甲没走到写盘前"

    jia.cancel()
    with pytest.raises(asyncio.CancelledError):
        await jia

    yi = asyncio.create_task(
        write_document.invoke(
            {"path": "厨房.md", "content": "乙写的", "fingerprint": fp}
        )
    )
    await asyncio.sleep(0.2)  # 让乙走到它能走到的最远处
    staged_disk.let_it_land.set()
    assert await asyncio.to_thread(staged_disk.landed.wait, 10), "甲的线程没写完"
    outcome = await yi

    on_disk = (docs / "厨房.md").read_text(encoding="utf-8")
    if isinstance(outcome, dict):
        assert on_disk == "甲写的", (
            f"乙被拒了，盘上却既不是甲的也不是乙的：{on_disk!r}"
        )
    else:
        assert on_disk == "乙写的", (
            f"乙拿到的是一句写好了，盘上却是「{on_disk}」—— "
            "被取消的那一次仍然在写，把乙刚落下的那一版盖掉了"
        )


async def test_a_cancelled_edit_cannot_swallow_a_later_one(docs, staged_disk):
    """``edit`` 整段 read-modify-write 都在线程里，所以同一条时序更直接。

    甲读完原文、算好替换结果、还没写盘时被取消，锁放开；乙读到的还是原文，改自己那
    一处、写下去、拿到一句改好了；甲的线程这时才落盘，把乙那一处又抹回去。两处各改
    各的本来互不相干，结果一处被吃掉了。
    """
    (docs / "厨房.md").write_text("灶台靠窗，窗外下着雨。", encoding="utf-8")
    staged_disk.arm()

    jia = asyncio.create_task(
        edit_document.invoke(
            {"path": "厨房.md", "find": "灶台靠窗", "replace": "灶台靠门"}
        )
    )
    assert await asyncio.to_thread(staged_disk.at_the_brink.wait, 10), "甲没走到写盘前"

    jia.cancel()
    with pytest.raises(asyncio.CancelledError):
        await jia

    yi = asyncio.create_task(
        edit_document.invoke(
            {"path": "厨房.md", "find": "窗外下着雨", "replace": "窗外天晴了"}
        )
    )
    await asyncio.sleep(0.2)
    staged_disk.let_it_land.set()
    assert await asyncio.to_thread(staged_disk.landed.wait, 10), "甲的线程没写完"
    outcome = await yi

    on_disk = (docs / "厨房.md").read_text(encoding="utf-8")
    if not isinstance(outcome, dict):
        assert "窗外天晴了" in on_disk, (
            f"乙拿到的是一句改好了，盘上却是「{on_disk}」—— "
            "被取消的那一次落盘晚了一步，把乙改的那一处抹了回去"
        )


async def test_a_cancelled_call_does_not_write_once_it_finally_gets_in(
    docs, staged_disk
):
    """没有人在等结果了，它就不该再开一次新的写入。

    锁改成按住整段之后，被取消的那一次会**排在锁外面等** —— 轮到它时那个协程早就没
    了，可它照样能把 find 那一段换掉。那是一次谁也没在等、谁也不知道的写入：不会出现
    在任何一次工具返回里，下一轮读回来却已经变了。

    ``hold`` 的 900 秒上限会把卡住的那一次掐断、下一拍重来，所以这条不是假想：一次
    挂住的落盘后面能排下一整串补写，挂住的解开之后它们会挨个补上。
    """
    (docs / "厨房.md").write_text("灶台靠窗，窗外下着雨。", encoding="utf-8")
    fp = fingerprint_of("灶台靠窗，窗外下着雨。")
    staged_disk.arm()

    jia = asyncio.create_task(
        write_document.invoke(
            {
                "path": "厨房.md",
                "content": "灶台靠门，窗外下着雨。",
                "fingerprint": fp,
            }
        )
    )
    assert await asyncio.to_thread(staged_disk.at_the_brink.wait, 10), "甲没走到写盘前"
    jia.cancel()
    with pytest.raises(asyncio.CancelledError):
        await jia

    yi = asyncio.create_task(
        edit_document.invoke(
            {"path": "厨房.md", "find": "窗外下着雨", "replace": "窗外天晴了"}
        )
    )
    await asyncio.sleep(0.2)  # 乙排到了这一份的锁上，一个字还没碰
    yi.cancel()
    with pytest.raises(asyncio.CancelledError):
        await yi

    staged_disk.let_it_land.set()
    assert await asyncio.to_thread(staged_disk.landed.wait, 10), "甲的线程没写完"

    ghost = await asyncio.to_thread(staged_disk.wrote_again.wait, GHOST_WRITE_WINDOW)
    assert not ghost, "取消之后轮到它了，它还是补写了一次"
    assert (
        docs / "厨房.md"
    ).read_text(encoding="utf-8") == "灶台靠门，窗外下着雨。"


# --------------------------------------------------------------------------
# 十 · 工具说明里不摆一条具体路径，也不摆一个具体地名
#
# 举例就是词表，会被逐字抄走 —— 这件事在她那侧炸过两次（``家/楼上/我房间`` 被两个人
# 同时抄走，``学校/二年三班教室`` 被抄成了设定集上不存在的地方）。world 这侧的举例
# 更直接：``地方/家/厨房.md``、``当下/文化祭.md`` 既是路径形状的说明，**也是世界的
# 内容** —— 树上那份文档改名、删掉、重写之后，代码里这几个字仍然在教它写一条早就不
# 成立的路径，而且这棵树本来就是它自己在写的。
#
# 路径形状（几层、用什么隔开、末一段是什么）说得清楚，不需要样本。
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hand", DOCUMENT_TOOLS, ids=[t.definition.name for t in DOCUMENT_TOOLS]
)
def test_a_document_hand_shows_no_path_sample(hand):
    """五只手交给模型的每一段字，都不含一条具体路径、也不含一个具体地名。"""
    from tests.living.conftest import (
        model_facing_text,
        names_of_places_in,
        path_samples,
    )

    for where, text in model_facing_text(hand).items():
        assert not path_samples(text), (
            f"{hand.definition.name} 的{where}里摆着一条路径样本 "
            f"{path_samples(text)!r} —— 它会逐字抄走。原文：\n{text}"
        )
        assert not names_of_places_in(text), (
            f"{hand.definition.name} 的{where}里写着具体地名 "
            f"{names_of_places_in(text)!r} —— 那是树上的内容，改完名之后这几个字还在。"
            f"原文：\n{text}"
        )


@pytest.mark.parametrize(
    "hand", DOCUMENT_TOOLS, ids=[t.definition.name for t in DOCUMENT_TOOLS]
)
def test_the_path_parameters_quote_no_example_at_all(hand):
    """路径那几个参数的描述里不出现引号 —— 挡的是**没有斜杠的**那种样本。

    ``path_samples`` 判的是"斜杠两边贴着字"，而一个顶层目录名只有一段、没有斜杠
    （``under`` 那个参数举的正是这种），照样是一个可以照着填的名字。
    """
    from tests.living.conftest import model_facing_text

    props = hand.definition.parameters.get("properties", {})
    for name in ("path", "under"):
        described = props.get(name, {}).get("description", "")
        assert "「" not in described, (
            f"{hand.definition.name} 的 {name} 描述里引着一个样本：{described!r}"
        )
        assert "例如" not in described, (
            f"{hand.definition.name} 的 {name} 描述里在举例：{described!r}"
        )
    assert model_facing_text(hand), "这只手一个字都没交给模型，用例失去意义"


def test_the_write_hand_still_says_what_shape_a_path_is():
    """清掉样本不等于不说形状。

    ``write_document`` 是唯一一只写**新**路径的手 —— 别的几只都能照 ``list_documents``
    列出来的抄，只有它没有可抄的东西，所以形状必须由参数描述自己讲清楚。
    """
    described = write_document.definition.parameters["properties"]["path"]["description"]

    assert "/" in described, f"没说层与层之间用什么隔开：{described!r}"
    assert ".md" in described, f"没说末一段带什么后缀：{described!r}"


@pytest.mark.parametrize("empty", ["", "   ", ".", "./"], ids=["空", "空白", "点", "点斜杠"])
async def test_refusing_an_empty_path_shows_no_path_sample(docs, empty):
    """"没说是哪一份"那句报错同样是喂给模型的字。

    它只在写错的时候出现，而那一刻正是最可能照着眼前这句话改的时候。
    """
    from tests.living.conftest import names_of_places_in, path_samples

    outcome = await read_document.invoke({"path": empty})
    said = str(outcome)

    assert not path_samples(said), f"报错里摆着路径样本：{said!r}"
    assert not names_of_places_in(said), f"报错里写着具体地名：{said!r}"


# --------------------------------------------------------------------------
# 十一 · 结果和措辞分开：模型读那句话，程序读那几个字段
#
# 整份重写和删掉现在有**两个受众**。中文句子里没有任何机器可读的东西 ——「写好了」
# 和「指纹过期被拒」只差在措辞上，程序要分辨就只能去解析中文，而措辞一改它就静默
# 失效。分不清冲突 = 「靠指纹挡冲突」这条契约对程序那一侧根本不成立。
#
# 所以这一节钉两头：
#
# * **程序那一侧**：成功、缺指纹、指纹过期、目标已被删，四种各有自己的 outcome 值。
#   那几个值本身就是对外契约，所以用例把字符串写死，不从实现里 import 枚举 ——
#   import 过来的话实现和用例会被一起改窄，而用例照样绿。
# * **模型那一侧**：**逐字不变**，而"模型看到的东西"不只是那句话。被拒是抛出去、由
#   ``@tool_error`` 包成 outcome dict 的，包的时候 ``type(exc).__name__`` 会被写进
#   ``detail["original_error_type"]``，**那个字段同样进模型的上下文** —— 为了结构化
#   换一个自造的异常类型，模型看到的东西就已经变了。所以这里断言的是**完整**返回，
#   而且那几段字是这次改动之前从当时的实现上原样跑出来的，不是照着新实现抄的：照着
#   新实现抄的快照只能证明它跟自己一致。
# --------------------------------------------------------------------------


def test_a_program_can_tell_the_four_write_outcomes_apart(docs):
    """四种结果四个不同的值。塌成同一个值 = 程序分不出冲突和成功。"""
    (docs / "甲.md").write_text("灶台靠窗。", encoding="utf-8")
    (docs / "乙.md").write_text("筹备中。林小满负责舞台。", encoding="utf-8")

    assert {
        "写成了": _write(docs, "新的线.md", "线").outcome,
        "没带指纹": _write(docs, "甲.md", "全新的一版").outcome,
        "指纹过期": _write(docs, "乙.md", "另一版", fingerprint_of("筹备中。")).outcome,
        "那一份已经没了": _write(
            docs, "丙.md", "写回来", fingerprint_of("筹备中。")
        ).outcome,
    } == {
        "写成了": "ok",
        "没带指纹": "no_fingerprint",
        "指纹过期": "stale_fingerprint",
        "那一份已经没了": "gone",
    }


def test_a_program_can_tell_the_four_delete_outcomes_apart(docs):
    """删掉跟整份重写共用同一套结果值 —— 两只手各一套的话调用方得认两份契约。"""
    (docs / "甲.md").write_text("灶台靠窗。", encoding="utf-8")
    (docs / "乙.md").write_text("筹备中。", encoding="utf-8")
    (docs / "丙.md").write_text("筹备中。林小满负责舞台。", encoding="utf-8")

    assert {
        "删掉了": _delete(docs, "乙.md", fingerprint_of("筹备中。")).outcome,
        "没带指纹": _delete(docs, "甲.md").outcome,
        "指纹过期": _delete(docs, "丙.md", fingerprint_of("筹备中。")).outcome,
        "那一份已经没了": _delete(docs, "丁.md", fingerprint_of("筹备中。")).outcome,
    } == {
        "删掉了": "ok",
        "没带指纹": "no_fingerprint",
        "指纹过期": "stale_fingerprint",
        "那一份已经没了": "gone",
    }


def test_a_write_that_landed_hands_the_program_the_new_fingerprint(docs):
    """写成之后接着改，程序跟模型一样不该被逼着再读一遍。

    模型从句子末尾那一串拿，程序从这个字段拿 —— 同一个东西的两种呈现。
    """
    change = _write(docs, "当下/新的线.md", "线")

    assert change.path == "当下/新的线.md"
    assert change.fingerprint == fingerprint_of("线")


def test_a_refused_change_carries_no_fingerprint(docs):
    """这个字段说的是"这次落下去的是哪一版"。一个字都没写，就没有这一版。"""
    (docs / "甲.md").write_text("灶台靠窗。", encoding="utf-8")

    blind = _write(docs, "甲.md", "全新的一版")
    stale = _write(docs, "甲.md", "全新的一版", fingerprint_of("筹备中。"))
    gone = _delete(docs, "丁.md", fingerprint_of("筹备中。"))

    assert [blind.fingerprint, stale.fingerprint, gone.fingerprint] == ["", "", ""]


async def test_what_the_model_reads_back_from_a_write_is_word_for_word(docs):
    """整份重写：模型那一侧收到的**完整**返回逐字不变。

    这几段字（含那几串十六进制和 ``original_error_type`` 里的类型名）是改动之前从
    当时的实现上原样跑出来的。
    """
    (docs / "甲.md").write_text("灶台靠窗。", encoding="utf-8")
    (docs / "乙.md").write_text("筹备中。", encoding="utf-8")
    (docs / "厨房.md").write_text("灶台靠窗。", encoding="utf-8")

    assert await write_document.invoke(
        {"path": "当下/新的线.md", "content": "线"}
    ) == (
        "写好了：当下/新的线.md（1 字）。"
        "【这一份现在的指纹：83ee4811d833，接着改它就带上这一串，不用再读一遍。】"
    )

    assert await write_document.invoke(
        {
            "path": "厨房.md",
            "content": "灶台靠窗，窗外下着雨。",
            "fingerprint": fingerprint_of("灶台靠窗。"),
        }
    ) == (
        "写好了：厨房.md（11 字）。"
        "【这一份现在的指纹：868bf622865b，接着改它就带上这一串，不用再读一遍。】"
    )

    assert await write_document.invoke(
        {"path": "甲.md", "content": "全新的一版"}
    ) == {
        "kind": "tool_error",
        "message": (
            "这一份没写成: 「甲.md」已经有了（5 字），一个字都没写 —— "
            "整份换掉之前得先看看它现在写的是什么。先 read_document 读一遍，"
            "把末尾那个指纹带上再写。"
        ),
        "detail": {"original_error_type": "ValueError"},
    }

    stale = fingerprint_of("筹备中。")
    (docs / "乙.md").write_text("筹备中。林小满负责舞台。", encoding="utf-8")
    assert await write_document.invoke(
        {"path": "乙.md", "content": "筹备中。已经定好日子。", "fingerprint": stale}
    ) == {
        "kind": "tool_error",
        "message": (
            "这一份没写成: 「乙.md」在你读到之后被改过了"
            "（你带的指纹是 8c4a88c5d9a2，现在是 cf0ca795b84b），一个字都没写。"
            "重新 read_document 读一遍，在新的那一版上改，再带着新指纹写回来 —— "
            "直接盖过去会把别人刚写下的那一段丢掉。"
        ),
        "detail": {"original_error_type": "ValueError"},
    }

    assert await write_document.invoke(
        {"path": "丙.md", "content": "筹备中。", "fingerprint": stale}
    ) == {
        "kind": "tool_error",
        "message": (
            "这一份没写成: 「丙.md」在你读到之后被删掉了（你带的指纹是 8c4a88c5d9a2），"
            "一个字都没写。它那条线多半已经走完了；确实要重新起一份的话，"
            "不带指纹再来一次。"
        ),
        "detail": {"original_error_type": "ValueError"},
    }


async def test_what_the_model_reads_back_from_a_delete_is_word_for_word(docs):
    """删掉：同上。**那一份不在了是 FileNotFoundError，不是 ValueError** ——

    两只手的拒绝在程序那一侧是同一个 outcome 值，可模型那一侧收到的类型名不一样，
    而类型名进它的上下文。结构化的时候把两边拉齐成同一个异常，模型看到的就变了。
    """
    (docs / "丁.md").write_text("灶台靠窗。", encoding="utf-8")
    (docs / "戊.md").write_text("筹备中。", encoding="utf-8")
    (docs / "己.md").write_text("灶台靠窗。", encoding="utf-8")
    (docs / "独").mkdir()
    (docs / "独/还在.md").write_text("还在。", encoding="utf-8")

    assert await delete_document.invoke(
        {"path": "己.md", "fingerprint": fingerprint_of("灶台靠窗。")}
    ) == "删掉了：己.md"

    assert await delete_document.invoke({"path": "丁.md"}) == {
        "kind": "tool_error",
        "message": (
            "这一份没删成: 「丁.md」还在（5 字），一个字都没动 —— "
            "删掉它之前得先看看它现在写的是什么。先 read_document 读一遍，"
            "把末尾那个指纹带上再来删。"
        ),
        "detail": {"original_error_type": "ValueError"},
    }

    stale = fingerprint_of("筹备中。")
    (docs / "戊.md").write_text("筹备中。林小满负责舞台。", encoding="utf-8")
    assert await delete_document.invoke(
        {"path": "戊.md", "fingerprint": stale}
    ) == {
        "kind": "tool_error",
        "message": (
            "这一份没删成: 「戊.md」在你读到之后被改过了"
            "（你带的指纹是 8c4a88c5d9a2，现在是 cf0ca795b84b），一个字都没动。"
            "重新 read_document 读一遍，确认那条线真的走完了，再带着新指纹来删 —— "
            "照着旧的那一版删下去会把别人刚写进去的那一段一起带走。"
        ),
        "detail": {"original_error_type": "ValueError"},
    }

    assert await delete_document.invoke(
        {"path": "独/走完了.md", "fingerprint": stale}
    ) == {
        "kind": "tool_error",
        "message": "这一份没删成: 没有「独/走完了.md」这一份。这儿有的是：\n独/还在.md",
        "detail": {"original_error_type": "FileNotFoundError"},
    }
