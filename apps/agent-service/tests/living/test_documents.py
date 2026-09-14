"""世界文档层 —— world 手里那棵文档树。

这一份守的是六条，每条都是"错了之后不会当场看出来"的那种：

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

**这些工具不绑轮次上下文，这是有意的。** 其余 living 工具要 ``moment_scope()`` 是因为
lane 决定它们写到哪条轴上；文档层的隔离来自挂载的根目录，时间和 persona 一样都不用。
要求一个用不到的 context 只会多一个保护不了任何东西的失败面。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.living.documents import (
    DOCS_DIR_ENV,
    DOCUMENT_CUT_MARK,
    DOCUMENT_TOOLS,
    MAX_DOCUMENT_CHARS,
    MAX_LISTING_ENTRIES,
    delete_document,
    documents_root,
    edit_document,
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
    """文档最终会变成她看到的东西，一个字都不能被改写。"""
    body = "厨房\n\n灶台靠窗，窗外是那条老街。傍晚有人在楼下喊「回家吃饭」。\n"

    await write_document.invoke({"path": "地方/家/厨房.md", "content": body})
    back = await read_document.invoke({"path": "地方/家/厨房.md"})

    assert back == body
    assert (docs / "地方/家/厨房.md").read_text(encoding="utf-8") == body


async def test_writing_creates_the_parent_directories(docs):
    """目录结构由它自己维护，不该先建目录再写文件。"""
    await write_document.invoke({"path": "a/b/c/d.md", "content": "深"})

    assert (docs / "a/b/c/d.md").read_text(encoding="utf-8") == "深"


async def test_writing_again_replaces_the_whole_file(docs):
    """write 是整份重写；改一处要用 edit，两件事不能长一样。"""
    await write_document.invoke({"path": "当下/线.md", "content": "第一版"})
    await write_document.invoke({"path": "当下/线.md", "content": "第二版"})

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
    await delete_document.invoke({"path": "当下/文化祭.md"})
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
