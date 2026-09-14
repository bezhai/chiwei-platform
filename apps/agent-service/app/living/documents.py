"""世界文档层 —— world 手里那棵文档树。

world 维护的是**世界观**，不是物理状态的记账。这棵树就是这个世界的设定集：这是个什么
世界、三姐妹什么关系、这座城市什么样、林小满是谁、厨房长什么样、现在正展开着哪几条线。
它每轮先看目录，按需读，改完写回去。

**为什么是文档不是表。** 一类内容一张表、一个工具，是在给自己造一台状态机：加一条线
要加一个字段，线走完了要加一个状态值。文档树里一条线走完了就是把文件删掉。人能直接
打开看世界走到哪了，能直接改；能进 git，世界的演化史就是 commit 历史。而且每轮不用读
全部 —— 先看目录再按需读，天然满足"输入不能随运行时间增长"。

**什么进库什么进文档：只有需要程序精确判定的才进库。** 人在哪（感知判定要做精确的位置
匹配）、发生了什么（要按游标拉增量）这两样在库里；设定、阶段、正在展开的线、地方的
样子、世界里的其他人，全在这儿。

**她不读文档。** 文档是 world 的工作产物，只以"她看到的东西"的形式到达她 —— 走进厨房
时看到的是"桌上还堆着没洗的碗"，不是一份可以 grep 的设定集。所以这几只手只挂在 world
的轮次上，:data:`DOCUMENT_TOOLS` 跟她那 22 只手是不相交的两组。

安全边界
--------

**根目录是挂载出来的，路径一步都不许走出去。** 同一个 RWX 卷上还挂着 ``/data/skills``，
而那里的 ``scripts/`` 会被 sandbox-worker 软链进执行目录**执行**：一条能写出根目录的
路径就是一条 RCE。所以这里不是"整洁问题"，:func:`resolve_within` 是这一层唯一的门，
五只手全部只经它拿路径。

``..``、绝对路径、``\\0`` 直接拒；符号链接靠 ``resolve()`` 之后重新判归属，因为链接本身
的路径是干净的、解析完才在外面。判归属用的是**父目录包含关系**而不是字符串前缀 ——
根是 ``/data/world`` 时 ``/data/world-evil`` 的字符串前缀是匹配的
（``apps/monitor-dashboard/src/routes/skills.ts`` 里那一份正是这么写的，别照抄）。

**这道门挡的是"路径本身指到外面"，不是"校验完到写入之间有人换了目录"。** 后者
（:func:`resolve_within` 返回之后、``mkdir`` / ``write_text`` 重新解析目录项之前，
有人把某一层父目录换成指向根外的符号链接）这一层确实拦不住，要拦得改成按文件描述符
逐段打开、全程 ``O_NOFOLLOW``。这次没改，因为要用上它得先有人能往这棵树里放一个符号
链接，而两条路都不通：**这五只手里没有任何一只能创建符号链接**，而同一个卷上的另一个
写入方（``/data/skills``）挂的是另一段 subPath，够不到这棵树。**哪天这两条里有一条不
成立了，这段就得改成 fd 逐段打开** —— 那时候它就是一条真的越界写入路径。

**泳道隔离是结构性的。** 根目录是 ``$WORLD_DOCS_DIR/<泳道>``：挂载点来自部署，泳道那
一段由代码拼（:func:`documents_root`）。代码里没有任何"当前泳道是不是 coe"的判断 ——
泳道名只是一段路径，对每条泳道一视同仁。**不让每条泳道各配一个环境变量**，是因为忘了
配的后果是静默写进 prod 的设定集，而那个卷没有备份。

**这几只手不绑轮次上下文，这是有意的。** 其余 living 工具要 ``moment_scope()`` 是因为
lane 决定它们写到哪条轴上；这里的隔离来自根目录，时间和 persona 一样都不用。要求一个
用不到的 context 只会多一个保护不了任何东西的失败面。日志里印的是解析后的真实路径，
本来就比 lane 更能说明这一次碰到了哪儿。
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Annotated

from pydantic import Field

from app.agent.tooling import tool
from app.agent.tools._common import tool_error
from app.runtime.lane_policy import current_deployment_lane

logger = logging.getLogger(__name__)

# 挂载点只认这一个环境变量，泳道那一段由 :func:`documents_root` 拼。没配时落在挂载点
# 上——**不是**落在仓库里的某个目录：真读写起来才发现写进了容器的可写层、重启就没了，
# 那种失败要等到"世界忘了昨天"才看得见。
DOCS_DIR_ENV = "WORLD_DOCS_DIR"
DEFAULT_DOCS_DIR = "/data/world"

# 一份文档最多读回来多少字。挂在裁剪的口径上：``trim_target_tokens`` 是 100k，而按仓库
# 自己那条 3 字节 ≈ 1 token 的换算，中文一个字就是一个 token。12k 字 = 一轮里连读几份
# 也吃不掉裁剪目标的一半；再大就意味着一份文档能把整轮上下文顶掉。
MAX_DOCUMENT_CHARS = 12_000

# 目录一次最多列多少项。同样是给上下文兜底：树迟早会长，而"每轮先看目录"是这套设计
# 省钱的前提，目录本身无界就白省了。
MAX_LISTING_ENTRIES = 400

# 截断了就说出来。**这句话必须出现在正文里**，不能只记一行日志：它读到一份被截过的
# 设定却不知道，就会把残篇当成世界的全貌，然后据此改写别的文档。
DOCUMENT_CUT_MARK = "这里被截断了"

_SEP = "/"


def documents_mount() -> Path:
    """卷挂在哪 —— ``$WORLD_DOCS_DIR``，泳道那一段还没拼上的那一层。

    它单独存在只为一件事：**分得清"卷没挂上"和"这条泳道还没写过"**。根目录不存在时
    这两种都成立，而给出的话得是两句 —— 一句要去找运维，一句照着写就行。泳道那一段是
    代码拼的（:func:`documents_root`），所以一条新泳道的根目录本来就不存在，直到第一次
    写入才被建出来。实测（coe-living，2026-09-14）：新泳道第一轮 world 拿到「卷没挂上」，
    然后整轮都不再碰文档。
    """
    return Path(os.environ.get(DOCS_DIR_ENV) or DEFAULT_DOCS_DIR)


def documents_root() -> Path:
    """本进程这条泳道的文档树的根：``$WORLD_DOCS_DIR/<泳道>``。

    每次现取，不缓存 —— 缓存下来等于把部署事实钉进进程生命周期。

    **泳道那一段是代码拼的，不是靠每条泳道各自配一个环境变量。** 靠配置的话，
    一条 coe 泳道忘了覆盖就直接写进 prod 的设定集：world 会去改线上那棵树，而这件事
    没有任何报错、也没有备份可以退回去（那个卷是 hostPath，无副本无备份）。拼在这里
    则是忘不掉的。

    这不是"按泳道分支"——没有任何一处在判"当前是不是 coe"，只是把泳道名当成一段路径，
    对每条泳道一视同仁。

    拿不到泳道时落到 ``"prod"``，跟 :func:`app.living.clock.living_lane` 同一条理由
    （空串会开一条谁也读不到的影子轴）。**这里没有直接调它**：``clock`` 在模块顶层
    import ``world``，而 ``world`` 要拿这几只手，直接用会绕成环。
    """
    return documents_mount() / (current_deployment_lane() or "prod")


def resolve_within(root: Path, path: str) -> Path:
    """把一条相对路径解析成根目录下的真实路径；走得出去就 ``ValueError``。

    这是整层唯一的门。五只手全部经它，包括 ``list`` 的 ``under``。

    拒绝的四类，各自堵的是不同的走法：

    * **空 / ``.`` / ``/``** —— "没说要哪一份"不能悄悄解析成根目录自己。读它会拿到一个
      目录、写它会试图把整棵树变成一个文件。
    * **``\\0``** —— 在 C 那层截断路径，检查看到的和真正打开的不是同一条。
    * **绝对路径、``..``** —— 字面上的逃逸。``..`` 一律拒而不是"算完看落在哪"：
      ``a/../b`` 确实没出去，但放行它就得让判定去理解路径代数，而这道门的正确性
      不该依赖那个。
    * **解析后不在根下** —— 符号链接。路径本身干净，``resolve()`` 之后才在外面。
    """
    raw = path.strip()
    if not raw:
        raise ValueError("没说是哪一份文档。写一条相对路径，例如「地方/家/厨房.md」。")
    if "\x00" in raw:
        raise ValueError("路径里有非法字符。")
    if raw.startswith(_SEP) or raw.startswith("\\"):
        raise ValueError(f"「{path}」是绝对路径。文档树里的路径都是相对的。")

    segments = [seg for seg in raw.replace("\\", _SEP).split(_SEP) if seg and seg != "."]
    if not segments:
        raise ValueError("没说是哪一份文档。写一条相对路径，例如「地方/家/厨房.md」。")
    if ".." in segments:
        raise ValueError(f"「{path}」里有 ..，文档树外面的东西碰不到。")

    base = root.resolve()
    target = (base / _SEP.join(segments)).resolve()
    if target != base and base not in target.parents:
        # 走到这儿只可能是符号链接：字面检查全过了，解析完却在外面。
        raise ValueError(f"「{path}」指到了文档树外面。")
    return target


def _relative(root: Path, target: Path) -> str:
    """印给模型看的那条路径 —— 相对根目录，不暴露挂载点。"""
    try:
        return target.relative_to(root.resolve()).as_posix()
    except ValueError:  # pragma: no cover - resolve_within 已经保证在根下
        return target.name


def _entries(base: Path, root: Path) -> list[str]:
    """``base`` 底下所有文件和目录，相对根目录的路径，目录带一个尾 ``/``。

    目录也列出来（包括空目录）：目录结构由 world 自己维护，看不见"当下/"这一层就
    不知道它已经建过，于是每次都重新建一套。
    """
    found: list[str] = []
    for child in base.rglob("*"):
        rel = _relative(root, child)
        found.append(rel + _SEP if child.is_dir() else rel)
    return sorted(found)


def list_tree(root: Path, under: str = "") -> str:
    """摆给 world 的那份目录。

    ``list_documents`` 那只手和界桩上重铺的那一份走的是同一个函数：两边各渲染一次的话
    它会在界桩上看到一种格式、自己列一次看到另一种，而那种漂移没有任何报错。

    **根目录不存在有两种，得分开说**（:func:`documents_mount`）：挂载点也不在 = 卷没挂
    上，要去找运维；挂载点在、只是这条泳道底下还没有过东西 = 树是空的，照着写就行 ——
    泳道那一段是代码拼的，所以一条新泳道的根本来就不存在，直到第一次写入才被建出来。
    混成一句的下场实测过（coe-living，2026-09-14）：world 第一轮拿到「卷没挂上」，
    整轮再没碰过文档。
    """
    base = root if under.strip() in ("", ".", "./", _SEP) else resolve_within(root, under)
    if not base.exists():
        if base == root.resolve() or base == root:
            if not documents_mount().exists():
                return (
                    f"（读不到文档树的挂载点 {documents_mount()} —— "
                    f"这不是路径写错了，是卷没挂上。）"
                )
            return "（这棵文档树现在是空的 —— 还没有写过任何一份。）"
        raise FileNotFoundError(f"「{under}」这个目录不存在。")

    found = _entries(base, root)
    if not found:
        where = "这棵文档树" if base == root.resolve() else f"「{under}」底下"
        return f"（{where}现在是空的 —— 还没有写过任何一份。）"

    head = "这棵文档树现在是这样："
    if len(found) > MAX_LISTING_ENTRIES:
        shown = found[:MAX_LISTING_ENTRIES]
        tail = (
            f"【{DOCUMENT_CUT_MARK}：一共 {len(found)} 项，"
            f"只列了前 {MAX_LISTING_ENTRIES} 项。想看别处就带上 under 单独列。】"
        )
        return "\n".join([head, *shown, tail])
    return "\n".join([head, *found])


def _nearby(root: Path, target: Path) -> str:
    """路径写错时，说得出它旁边有什么 —— 不然它只能瞎猜第二次。

    先看同一个目录，那个目录也不在就退到根。同 :mod:`app.living.guides` 那条：
    "名字写错了"和"一份都没有"是两种处境，给的话也得是两句。
    """
    folder = target.parent if target.parent.exists() else root.resolve()
    if not folder.exists():
        # 跟 list_tree 同一条分法：挂载点也不在才是卷没挂上，否则只是这条泳道还没写过。
        if not documents_mount().exists():
            return f"文档树的挂载点 {documents_mount()} 都读不到 —— 卷没挂上。"
        return "这棵文档树现在还是空的。"
    siblings = sorted(
        _relative(root, child) + (_SEP if child.is_dir() else "")
        for child in folder.iterdir()
    )
    if not siblings:
        return "这棵文档树现在还是空的。"
    return "这儿有的是：\n" + "\n".join(siblings)


def _read(root: Path, path: str) -> str:
    target = resolve_within(root, path)
    if target.is_dir():
        raise IsADirectoryError(
            f"「{path}」是一个目录不是一份文档。想看它底下有什么，用 list_documents 带上 under。"
        )
    if not target.exists():
        raise FileNotFoundError(f"没有「{path}」这一份。{_nearby(root, target)}")

    body = target.read_text(encoding="utf-8")
    if len(body) > MAX_DOCUMENT_CHARS:
        return body[:MAX_DOCUMENT_CHARS] + (
            f"\n\n【{DOCUMENT_CUT_MARK}：这一份有 {len(body)} 字，"
            f"只读到前 {MAX_DOCUMENT_CHARS} 字。太长了，拆成几份吧。】"
        )
    return body


def _write(root: Path, path: str, content: str) -> str:
    target = resolve_within(root, path)
    if len(content) > MAX_DOCUMENT_CHARS:
        # **不截断落盘**：落进去的是残篇而它以为写全了，下一轮读回来就当成全貌。
        # 宁可这一次失败，让它重写短一点或者拆开。
        raise ValueError(
            f"这一份有 {len(content)} 字，超过了 {MAX_DOCUMENT_CHARS} 字的上限，"
            "一个字都没写。拆成几份，或者写短一点再来。"
        )
    if target.is_dir():
        raise IsADirectoryError(f"「{path}」是一个目录，不能当成一份文档写。")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"写好了：{_relative(root, target)}（{len(content)} 字）"


def _edit(root: Path, path: str, find: str, replace: str) -> str:
    """替换唯一的一处。**不唯一就报错，不挑第一处。**

    静默只改第一处的下场：同一份文档里留下两句互相矛盾的说法，而 world 以为自己改完了，
    下一轮读回来照着那份自相矛盾的设定继续推。
    """
    if not find:
        raise ValueError("find 是空的。空串在每个位置都命中，那不是一次替换。")

    target = resolve_within(root, path)
    if not target.exists() or target.is_dir():
        raise FileNotFoundError(f"没有「{path}」这一份。{_nearby(root, target)}")

    body = target.read_text(encoding="utf-8")
    hits = body.count(find)
    if hits == 0:
        raise ValueError(
            f"「{path}」里找不到这一段，一个字都没改。先 read_document 看一眼原文，"
            "照着抄一段独一无二的再来。"
        )
    if hits > 1:
        raise ValueError(
            f"这一段在「{path}」里出现了 {hits} 处，没法确定改哪一处，一个字都没改。"
            "把 find 写长一点，带上前后文让它只命中一处。"
        )

    updated = body.replace(find, replace, 1)
    if len(updated) > MAX_DOCUMENT_CHARS:
        raise ValueError(
            f"改完会有 {len(updated)} 字，超过 {MAX_DOCUMENT_CHARS} 字的上限，"
            "一个字都没改。"
        )
    target.write_text(updated, encoding="utf-8")
    return f"改好了：{_relative(root, target)}"


def _delete(root: Path, path: str) -> str:
    """删掉一份文档。**只删文件。**

    删一份和端掉整个「设定/」不能是同一只手：前者是一条线走完了，后者是世界没了。
    目录要清空的话一份一份删，那个笨拙本身就是刹车。
    """
    target = resolve_within(root, path)
    if target.is_dir():
        raise IsADirectoryError(
            f"「{path}」是一个目录。这只手只删单份文档 —— 整个目录要清，一份一份来。"
        )
    if not target.exists():
        raise FileNotFoundError(f"没有「{path}」这一份。{_nearby(root, target)}")

    target.unlink()
    return f"删掉了：{_relative(root, target)}"


@tool
@tool_error("列文档树失败")
async def list_documents(
    under: Annotated[
        str,
        Field(description="只看哪个目录底下，例如「地方」；留空就是整棵树"),
    ] = "",
) -> str:
    """看看这个世界的设定集里现在都有些什么。

    交回来的是路径清单，目录带一个尾「/」。**只有路径，没有正文** —— 想知道某一份里
    写的是什么，拿它的路径去 read_document。

    每轮先看这一份，再决定要读哪几份，是这套东西成立的前提：整棵树一次全读进来，它会
    越长越贵，而你大部分轮次只关心其中一两份。

    树大了会只列前面一部分，截了会写在末尾；那时带上 under 单独列你关心的那一块。

    Args:
        under: 只看哪个目录底下；留空 = 整棵树。

    Returns:
        一份路径清单；树是空的、或者卷没挂上时，是说明这件事的一句话。
    """
    root = documents_root()
    listing = await asyncio.to_thread(list_tree, root, under)
    logger.info("world documents 列目录 root=%s under=%r", root, under)
    return listing


@tool
@tool_error("这一份没读成")
async def read_document(
    path: Annotated[
        str,
        Field(description="这一份的路径，例如「地方/家/厨房.md」；照 list_documents 列出来的抄"),
    ],
) -> str:
    """把设定集里的一份从头读一遍。

    路径照 list_documents 列出来的抄，别自己编。写错了它会把那附近有什么摆给你，
    照着挑一个再来。

    一份太长会只读到前面一部分，截了会写在末尾 —— 看到那句话就知道你读到的不是全的，
    别拿它当这一份的全貌去改别的文档。

    Args:
        path: 这一份的路径。

    Returns:
        那一份的正文。
    """
    root = documents_root()
    body = await asyncio.to_thread(_read, root, path)
    logger.info("world documents 读 root=%s path=%r（%d 字）", root, path, len(body))
    return body


@tool
@tool_error("这一份没写成")
async def write_document(
    path: Annotated[
        str,
        Field(description="写到哪，例如「当下/文化祭.md」；目录不存在会自动建"),
    ],
    content: Annotated[str, Field(description="这一份的全部正文")],
) -> str:
    """写下一份文档，或者把已有的一份整份换掉。

    **这是整份重写，不是追加。** 已经有这一份的话，原来的内容全部被换成你这次给的。
    只改其中一句用 edit_document。

    路径里的目录不存在会自动建，不用先建目录。

    **写自然的描述，不要写属性表。** 这些内容最终会变成她看到的东西 —— 写成
    「# 厨房 / ## 布局 / 灶台靠窗」会很出戏，写成「灶台靠窗，窗外是那条老街」才是
    一个人走进厨房时看到的样子。

    一份有字数上限，超了会**一个字都不写**并且告诉你 —— 那时候拆成几份，别硬塞。

    Args:
        path: 写到哪。
        content: 这一份的全部正文。

    Returns:
        一句确认。
    """
    root = documents_root()
    said = await asyncio.to_thread(_write, root, path, content)
    logger.info("world documents 写 root=%s path=%r（%d 字）", root, path, len(content))
    return said


@tool
@tool_error("这一处没改成")
async def edit_document(
    path: Annotated[str, Field(description="改哪一份，例如「地方/家/厨房.md」")],
    find: Annotated[
        str,
        Field(description="要被换掉的那一段原文，逐字照抄；必须在这一份里独一无二"),
    ],
    replace: Annotated[str, Field(description="换成什么；留空就是把那一段删掉")],
) -> str:
    """把一份文档里的某一段换成别的，其余一个字不动。

    find 要**逐字照抄原文**，而且必须在这一份里**只出现一处**：出现了两处它不会替你
    挑一处，会报错并告诉你有几处 —— 那时候把 find 写长一点，带上前后文。

    找不到也会报错。两种情况都是一个字都没改，可以放心重来。

    改一整份用 write_document；改一句用这只手。

    Args:
        path: 改哪一份。
        find: 要被换掉的那一段原文（逐字、唯一）。
        replace: 换成什么；留空 = 删掉那一段。

    Returns:
        一句确认。
    """
    root = documents_root()
    said = await asyncio.to_thread(_edit, root, path, find, replace)
    logger.info("world documents 改 root=%s path=%r", root, path)
    return said


@tool
@tool_error("这一份没删成")
async def delete_document(
    path: Annotated[str, Field(description="删哪一份，例如「当下/文化祭.md」")],
) -> str:
    """把一份文档删掉。

    一条线走完了就是把它那一份删掉 —— 不用留一个"已结束"的标记，设定集里不该有
    只为了记状态而存在的东西。

    **只删单份文档，删不了目录。** 整个目录要清就一份一份来。

    Args:
        path: 删哪一份。

    Returns:
        一句确认。
    """
    root = documents_root()
    said = await asyncio.to_thread(_delete, root, path)
    logger.info("world documents 删 root=%s path=%r", root, path)
    return said


# world 一轮里操作设定集的五只手。**她手里没有这几只**（第六节那条边界），
# ``tests/living/test_documents.py`` 钉着这两组不相交。
DOCUMENT_TOOLS = [
    list_documents,
    read_document,
    write_document,
    edit_document,
    delete_document,
]
