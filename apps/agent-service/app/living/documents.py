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

**根目录是挂载出来的，路径一步都不许走出去。** 走出去就是往容器自己的文件系统里写，
而这五只手的路径参数全部来自模型。所以这里不是"整洁问题"，:func:`resolve_within` 是
这一层唯一的门，五只手全部只经它拿路径。

``..``、绝对路径、``\\0`` 直接拒；符号链接靠 ``resolve()`` 之后重新判归属，因为链接本身
的路径是干净的、解析完才在外面。判归属用的是**父目录包含关系**而不是字符串前缀 ——
根是 ``/data/world`` 时 ``/data/world-evil`` 的字符串前缀是匹配的
（``apps/monitor-dashboard/src/routes/skills.ts`` 里那一份正是这么写的，别照抄）。

**这道门挡的是"路径本身指到外面"，不是"校验完到写入之间有人换了目录"。** 后者
（:func:`resolve_within` 返回之后、``mkdir`` / ``write_text`` 重新解析目录项之前，
有人把某一层父目录换成指向根外的符号链接）这一层确实拦不住，要拦得改成按文件描述符
逐段打开、全程 ``O_NOFOLLOW``。这次没改，因为要用上它得先有人能往这棵树里放一个符号
链接，而两条路都不通：**这五只手里没有任何一只能创建符号链接**，而这个卷在整个集群里
只有 agent-service 一个挂载点（``pvc-world-docs``，挂在根上，没有别人）。**哪天这两条里
有一条不成立了，这段就得改成 fd 逐段打开** —— 那时候它就是一条真的越界写入路径。

第二条曾经是假的，而且假了两天：这棵树本来是 ``pvc-shared-skills`` 上的一段 subPath，
而 monitor-dashboard 把**同一个卷的根**挂成可写，它那个 skill 文件 CRUD 接口用
``name=world`` 就正常够得到这棵树，连穿越都不用。2026-09-16 把树迁到独立卷才让这句话
成立。所以这条不是背景描述，是**迁出去的理由**；谁要是把它挪回共享卷，这段推理连同
上面那个"没改"的结论一起作废。

**泳道隔离是结构性的。** 根目录是 ``$WORLD_DOCS_DIR/<泳道>``：挂载点来自部署，泳道那
一段由代码拼（:func:`documents_root`）。代码里没有任何"当前泳道是不是 coe"的判断 ——
泳道名只是一段路径，对每条泳道一视同仁。**不让每条泳道各配一个环境变量**，是因为忘了
配的后果是静默写进 prod 的设定集，而那个卷没有备份。

**这几只手不绑轮次上下文，这是有意的。** 其余 living 工具要 ``moment_scope()`` 是因为
lane 决定它们写到哪条轴上；这里的隔离来自根目录，时间和 persona 一样都不用。要求一个
用不到的 context 只会多一个保护不了任何东西的失败面。日志里印的是解析后的真实路径，
本来就比 lane 更能说明这一次碰到了哪儿。

两个写者
--------

**没有人的写入可以被静默丢掉。** 文件系统本身不拦任何东西：``write_text`` 是后写覆盖
先写，``_edit`` 是 read-modify-write —— 两个写者同改一份，先写的那一版连同它以为自己
做成了的那件事一起消失，两边都拿到一句"写好了"。这棵树是世界的设定集，丢掉的那一段
不会有第二个地方留着。

挡住它的是两件事，各管一种竞争：

* **每份文档一把锁**。管的是*同一瞬间*：读指纹和写下去之间、``_edit`` 读到改完之间，
  不会有别人插进来。按文档分键而不是整棵树一把 —— 两份不相干的文档之间没有任何关系，
  共用一把锁只是让每次写入都排在上一次后面。这把锁实际上是两把，见下一节。
* **对一份已经存在的文档做破坏性的事要带上指纹**（:func:`fingerprint_of`）。整份重写
  （``write_document``）和删掉（``delete_document``）都算。管的是*跨调用*那条真实路径：
  模型 ``read_document`` 一次、想一会儿、再写回来或者删掉，那中间是它自己的思考，锁按
  住不了。指纹只从 ``read_document`` 交回来（目录里没有），所以带得出指纹 == 看过现在
  写的是什么；对不上就拒，并让它重读一遍再决定。

  **删掉和覆盖同一条规矩，不能比覆盖松。** 只给覆盖设这道门的话，带着过期指纹去盖会
  被拦下来、什么都不带直接删却放行 —— 而删掉更狠：覆盖至少还留下新的那一版，删掉是把
  那一份整个带走。

``edit_document`` 不要指纹：``find`` 必须唯一命中本来就是一次 CAS —— 别人改过那一段，
锚点就找不到了，它照旧会被拒。锁保证"找到"和"改完"之间没人插队。

**失败一律出声。** 拒绝的那几种（没带指纹、指纹过期、文档被删了）都是"一个字都没动"
外加一句它能照着做的话，跟 :class:`app.living.continuity.TranscriptConflict` 同一条：
默默覆盖等于把另一个人刚写下的一整段丢掉，而且没有任何痕迹。

**而那句话不是结果本身。** 整份重写和删除交回来的是 :class:`DocumentChange`：成功和
那三种拒绝各有一个 :class:`ChangeOutcome` 值，那句中文只是它的一种呈现。分开是因为
这条路径有两个受众 —— 模型读句子，程序读字段，而句子里没有任何机器可读的东西，靠
解析中文来分辨冲突会在措辞一改的时候静默失效。两种呈现从同一处来，不会各说各的。

第三个写者：从外面来的那一只手
------------------------------

这棵树还有一条从进程外面进来的入口（:mod:`app.nodes.world_documents` 那四个端点）。
树上写歪的一份文档在那之前只能等 world 自己发现自己改，而它发现不了的那些就一直留着。

**它和 world 是对等的两个写者，谁也不比谁高一级。** 成立的前提是它走的是同一条路：
:func:`listing` / :func:`read_whole` / :func:`rewrite` / :func:`remove` 是这一层交给
外面的四个入口，每一个都落在 :func:`_touch_disk` 上，抢的就是 world 那五只手真正碰盘
时抢的同一把 per-file 锁，认的也是同一套指纹。换任何别的进程去写这个卷，这两把锁立刻
失效，指纹 CAS 也跟着变成摆设 —— 读和写之间不再有共同的锁，中间可以插进任意多次别人的
完整写入。

它们跟那五只手的差别只在**呈现**：交回去的是 :class:`TreeListing` / :class:`WholeDocument`
/ :class:`DocumentChange`，不是渲染给模型看的那几段中文。

:func:`read_whole` 还有一处是**故意跟 :func:`_read` 不一样的**：它不截断。给模型的那只手
截是因为一份跑飞的文档能把整轮上下文顶掉；这一侧照抄的话，交出去的就是"截过的正文配整份
的指纹"，拿回来改完写回去指纹**对得上**，而尾巴被静默删掉了 —— 正是指纹这道门要挡的事。

锁按住的是碰盘那一段，不是那个协程
----------------------------------

碰盘跑在 ``asyncio.to_thread`` 里，而**取消只到得了协程，到不了线程**：协程被取消时
``async with`` 会退出、锁跟着放开，那个线程一行都停不下来。只有一把 asyncio 锁的话，
"锁按住的那一段"和"真正碰盘的那一段"就错开了，后者伸到锁外面去：

1. 甲拿到锁，过了指纹检查，还没写盘；
2. 甲那个协程被取消 —— 锁放开，甲的线程接着跑；
3. 乙拿到锁，盘上确实还是甲读到的那一版，于是指纹对得上、写下去、**拿到一句写好了**；
4. 甲的线程这时才落盘，把乙那一版盖掉。

乙拿到的是成功确认，最终文件是甲的内容 —— 正是这一层承诺不会发生的那件事，单进程就能
复现。所以这里是两把锁，各管一头：

* :func:`app.living.serial.hold`（asyncio 锁）管协程这一侧的排队，顺带带来
  :data:`app.living.serial.HELD_SECONDS` 那个上限 —— 一次挂死的落盘不会把这一份永久扣住。
* :func:`_file_lock`（``threading.Lock``）管线程这一侧。**它才是覆盖真正碰盘那一段的
  那把**：它在线程里拿、在线程里放，协程有没有被取消跟它无关。于是上面第 3 步的乙会
  一直等到甲的线程真的写完，然后读到甲写下的内容、发现指纹对不上、**被拒**。

两把锁的获取顺序永远是 asyncio 锁在外、线程锁在内，同一次调用各只拿一把、不嵌套，
所以不会死锁；线程锁按住的那一段里没有任何 await，不会把事件循环拖住。

**代价是等在线程锁上的那个线程真的占着一个 executor 槽**（等在 asyncio 锁上是不占的）。
同一个键在任一瞬间最多有一个这样的等待者 —— 外层那把 asyncio 锁本来就只放一个进来 ——
外加被取消那几次留下的、还没跑完的线程，而 900 秒那个上限决定了后者最快每 15 分钟才多
一个。真要把这一点也拿掉，得给线程锁的等待带上限，那是另一件事。

取消之后线程还会做完手上这一次（**已经动过手的半途收手更坏**：``_edit`` 的
read-modify-write 断在中间就是一份被改了一半的文档），但**不会再开一次新的**：拿到线程
锁时发现没人等这个结果了就什么都不做。不然一次挂住的落盘后面会排下一整串补写，挂住的
解开之后它们挨个补上 —— 那些写入不出现在任何一次工具返回里，下一轮读回来树却已经变了。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, TypeVar

from pydantic import Field

from app.agent.tooling import tool
from app.agent.tools._common import tool_error
from app.living.serial import hold
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

# 指纹跟着正文一起交回去，这句话是模型认出它的记号。**它只出现在 read / write 交回
# 去的那段话里**，目录里没有：指纹要是能不读就拿到，它就成了一个纯版本号，而这道门
# 挡的正是"没看过现在写的是什么就整份换掉"。
DOCUMENT_FINGERPRINT_MARK = "这一份现在的指纹"

# 指纹取多少位。摘要越短越省上下文，而它只需要区分同一份文档的两个版本：12 位十六
# 进制 = 48 bit，撞上的概率远低于这套东西里任何一条别的假设。
FINGERPRINT_CHARS = 12

_SEP = "/"

# 碰盘那一段（:func:`_touch_disk`）交回来的是它跑的那个函数交回来的东西：读是一段
# 正文，整份重写和删除是一个 :class:`DocumentChange`。
T = TypeVar("T")

# ---------------------------------------------------------------------------
# 说清路径的形状，但一条具体路径都不摆出来
#
# 这几只手的参数描述里原来举着 ``地方/家/厨房.md``、``当下/文化祭.md``、``地方``。
# 它们既是路径形状的说明，**也是世界的内容** —— 而这棵树正是 world 自己在写的：那几
# 份文档改名、搬走、删掉之后，代码里这几个字仍然在教它写一条早就不成立的路径。
#
# 同一个病在她那侧炸过两次（见 :data:`app.living.place.PLACE_SHAPE` 上方那段）：
# 举例就是词表，会被逐字抄走。换一批新的写死字符串只是把过期时间往后推。
#
# 路径形状不需要样本：几层、用什么隔开、末一段是什么，直说就行。占位符
# （``A/B``、``<目录>/<文件>.md``）同样不给 —— 那还是一个可以照着填的模板。
#
# **只有 write_document 需要说形状**：另外四只手写的都是树上已经有的路径，
# ``list_documents`` 列出来的那份清单就是它们的样本来源，照着抄即可。
# ---------------------------------------------------------------------------

NO_DOCUMENT_NAMED = "没说是哪一份文档。写一条相对树根的路径，层与层之间用 / 隔开。"

WHERE_TO_WRITE = (
    "写到哪：一条相对树根的路径，层与层之间用 / 隔开，"
    "末一段是这份文档的文件名，带 .md 后缀；中间的目录不存在会自动建"
)


def fingerprint_of(body: str) -> str:
    """这一段正文的指纹。**认的是内容，不是时间也不是长度。**

    换成 mtime 的话，同一秒内的两次写入分不出来；换成长度的话，改一个字换一个字就是
    同一个指纹。内容摘要没有这两种盲区：正文变了指纹一定变。
    """
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:FINGERPRINT_CHARS]


def documents_mount() -> Path:
    """卷挂在哪 —— ``$WORLD_DOCS_DIR``，泳道那一段还没拼上的那一层。

    它单独存在只为一件事：**分得清"卷没挂上"和"这条泳道还没写过"**。根目录不存在时
    这两种都成立，而给出的话得是两句 —— 一句要去找运维，一句照着写就行。泳道那一段是
    代码拼的（:func:`documents_root`），所以一条新泳道的根目录本来就不存在，直到第一次
    写入才被建出来。实测（coe-living，2026-09-14）：新泳道第一轮 world 拿到「卷没挂上」，
    然后整轮都不再碰文档。
    """
    return Path(os.environ.get(DOCS_DIR_ENV) or DEFAULT_DOCS_DIR)


def documents_lane() -> str:
    """本进程这棵树落在哪条泳道上 —— :func:`documents_root` 末一段那个名字。

    **只从进程自己的部署环境读。** 从外面改这棵树的那几个端点每次都把它交回去，而它
    要回答的是"这次调用改的是哪棵树"：回显请求里带来的任何东西等于什么都没回答。
    请求送进哪个 pod 决定动哪棵树，而那件事只有收到请求的这个进程知道。

    跟根目录共用同一个表达式，不是另算一遍：算两遍的话它们会漂，而漂了之后自报的
    落点仍然看起来像个正确答案。
    """
    return current_deployment_lane() or "prod"


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
    return documents_mount() / documents_lane()


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
        raise ValueError(NO_DOCUMENT_NAMED)
    if "\x00" in raw:
        raise ValueError("路径里有非法字符。")
    if raw.startswith(_SEP) or raw.startswith("\\"):
        raise ValueError(f"「{path}」是绝对路径。文档树里的路径都是相对的。")

    segments = [seg for seg in raw.replace("\\", _SEP).split(_SEP) if seg and seg != "."]
    if not segments:
        raise ValueError(NO_DOCUMENT_NAMED)
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


def _fingerprint_footer(fingerprint: str) -> str:
    """挂在正文后面的那截。模型认的是 :data:`DOCUMENT_FINGERPRINT_MARK` 这句话。

    跟 :data:`DOCUMENT_CUT_MARK` 同一条路子：**必须出现在交回去的正文里**，只记一行
    日志等于没说。挂在末尾而不是开头，是为了让正文那一段还能被逐字取下来。
    """
    return (
        f"\n\n【{DOCUMENT_FINGERPRINT_MARK}：{fingerprint}，"
        f"想整份换掉它（write_document）或者删掉它（delete_document）就把这一串带上 —— "
        f"中途被别人改过会当场拦下来，而不是把那一版悄悄抹掉。】"
    )


def _read_with_fingerprint(root: Path, path: str) -> str:
    """:func:`_read` 的正文，后面缀上这一份此刻的指纹。

    **只有这只手（和写入那只）交指纹。** :func:`_read` 自己一个字不动：她走进厨房看到
    的那段描述走的是同一个函数（:mod:`app.living.moment`），那里冒出一串十六进制就是
    出戏。

    正文和指纹是两次触盘，所以这个函数必须在那把锁里跑 —— 中间被写了一版的话，交回去
    的就是"上一版的正文配下一版的指纹"，它照着改完写回来一路畅通，而它改的是一份自己
    从没读过的正文。那比直接放行覆盖更坏：门开着，还看起来是关的。
    """
    body = _read(root, path)
    whole = resolve_within(root, path).read_text(encoding="utf-8")
    return body + _fingerprint_footer(fingerprint_of(whole))


class ChangeOutcome(StrEnum):
    """整份重写或删除的结果，**程序那一侧读的就是它**。

    这几个值是对外契约的一部分（谁照着它分辨成功和冲突，改一个值就是改契约），所以
    它们是稳定的英文标识而不是那句中文 —— 中文是措辞，措辞会改。

    :attr:`GONE` 两只手共用一个值：``write`` 那边是"读到之后被删掉了"，``delete``
    那边是"要删的那一份已经不在了"。对调用方是同一件事 —— 那一份没了，重读一遍再
    决定 —— 所以不拆成两个值。
    """

    OK = "ok"
    NO_FINGERPRINT = "no_fingerprint"
    STALE_FINGERPRINT = "stale_fingerprint"
    GONE = "gone"


@dataclass(frozen=True)
class DocumentChange:
    """一次整份重写或删除的结果。**一处定义，两种呈现。**

    这条写入路径现在有两个受众，它们看的不是同一样东西：

    * **模型**读 :attr:`said` 那句中文。它是唯一入口：那句话既要说清一个字都没写，
      也要说清下一步该做什么。
    * **程序**读 :attr:`outcome`、:attr:`path`、:attr:`fingerprint`。中文句子里没有
      任何机器可读的东西，靠解析它来分辨"写成了"和"指纹过期被拒"是脆的，而且会在
      措辞一改的时候静默失效 —— 而这条路径的整个契约就是"靠指纹挡冲突"，分不清冲突
      等于这份契约对程序那一侧不成立。

    **模型看到的东西不只是那句话。** 被拒的时候 :attr:`said` 不是返回值，是被抛出去、
    由 :func:`app.agent.tools._common.tool_error` 包成 outcome dict 的；包的时候
    ``type(exc).__name__`` 会被写进 ``detail["original_error_type"]``，**那个字段同样
    进模型的上下文**。所以异常类型也是这份呈现的一部分，由 :attr:`refused_as` 定死：
    整份重写的三种拒绝都是 ``ValueError``，删除的"那一份不在了"是 ``FileNotFoundError``
    （它本来就是这么抛的）。为了结构化而换一个自造的异常类型 = 改了模型看到的东西。

    :attr:`fingerprint` 说的是**这次落下去的是哪一版**，所以只有写成的时候有值，跟句子
    末尾交回去的那一串是同一个。一个字都没写就没有这一版。
    """

    outcome: ChangeOutcome
    path: str
    said: str
    fingerprint: str = ""
    refused_as: type[Exception] | None = None

    def raise_if_refused(self) -> None:
        """被拒就按模型那一侧原本的类型和措辞抛出去；写成了什么都不做。"""
        if self.refused_as is not None:
            raise self.refused_as(self.said)


def _write(
    root: Path, path: str, content: str, fingerprint: str = ""
) -> DocumentChange:
    """整份重写。**覆盖一份已经存在的文档必须带上读到的那一版的指纹。**

    没带 = 它没看过现在写的是什么，覆盖过去就是把别人写的那一段静默丢掉；带的那个对
    不上 = 它读完之后有人改过，照着旧正文写回来同样是丢掉那一段。两种都拒，都是一个
    字都没写。

    新建一份不要指纹：那时候没有任何人的写入会被吃掉。

    这三种拒绝交的是 :class:`DocumentChange` 而不是异常 —— 它们是这条路径的**结果**，
    两个受众都要认（见那个类的 docstring）。路径逃逸、超长、目标是个目录仍然抛：那些
    是参数不对，不是两个写者撞上了。
    """
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

    here = _relative(root, target)
    given = fingerprint.strip()
    on_disk = target.read_text(encoding="utf-8") if target.is_file() else None
    if on_disk is None:
        if given:
            # 删掉也是一次写入：一条线走完了就是把它那一份删掉，不能被一次过期的覆盖
            # 写回来 —— 那一份会以"它还在"的样子接着进世界。
            return DocumentChange(
                outcome=ChangeOutcome.GONE,
                path=here,
                said=(
                    f"「{path}」在你读到之后被删掉了（你带的指纹是 {given}），"
                    "一个字都没写。它那条线多半已经走完了；确实要重新起一份的话，"
                    "不带指纹再来一次。"
                ),
                refused_as=ValueError,
            )
    else:
        current = fingerprint_of(on_disk)
        if not given:
            return DocumentChange(
                outcome=ChangeOutcome.NO_FINGERPRINT,
                path=here,
                said=(
                    f"「{path}」已经有了（{len(on_disk)} 字），一个字都没写 —— "
                    "整份换掉之前得先看看它现在写的是什么。先 read_document 读一遍，"
                    "把末尾那个指纹带上再写。"
                ),
                refused_as=ValueError,
            )
        if given != current:
            return DocumentChange(
                outcome=ChangeOutcome.STALE_FINGERPRINT,
                path=here,
                said=(
                    f"「{path}」在你读到之后被改过了（你带的指纹是 {given}，"
                    f"现在是 {current}），一个字都没写。重新 read_document 读一遍，"
                    "在新的那一版上改，再带着新指纹写回来 —— 直接盖过去会把别人刚写下的"
                    "那一段丢掉。"
                ),
                refused_as=ValueError,
            )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    landed = fingerprint_of(content)
    return DocumentChange(
        outcome=ChangeOutcome.OK,
        path=here,
        said=(
            f"写好了：{here}（{len(content)} 字）。"
            f"【{DOCUMENT_FINGERPRINT_MARK}：{landed}，"
            f"接着改它就带上这一串，不用再读一遍。】"
        ),
        fingerprint=landed,
    )


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


def _delete(root: Path, path: str, fingerprint: str = "") -> DocumentChange:
    """删掉一份文档。**只删文件，而且必须带上读到的那一版的指纹。**

    删一份和端掉整个目录不能是同一只手：前者是一条线走完了，后者是世界没了。目录要
    清空的话一份一份删，那个笨拙本身就是刹车。

    指纹那道门跟 :func:`_write` 同一条规矩，理由见模块 docstring：删掉比覆盖更狠，
    不能反而更容易。没带 = 它没看过现在写的是什么；带的那个对不上 = 它读完之后有人
    改过，删下去就把那一段一起带走了。两种都拒，都是一个字没动。

    结果的形状跟 :func:`_write` 一样是 :class:`DocumentChange`，**"那一份不在了"
    那一种交的仍然是 ``FileNotFoundError``**：它跟整份重写那边的 ``GONE`` 在程序那
    一侧是同一个值，可模型那一侧收到的类型名不一样，而类型名进它的上下文。
    """
    target = resolve_within(root, path)
    if target.is_dir():
        raise IsADirectoryError(
            f"「{path}」是一个目录。这只手只删单份文档 —— 整个目录要清，一份一份来。"
        )
    here = _relative(root, target)
    if not target.exists():
        return DocumentChange(
            outcome=ChangeOutcome.GONE,
            path=here,
            said=f"没有「{path}」这一份。{_nearby(root, target)}",
            refused_as=FileNotFoundError,
        )

    given = fingerprint.strip()
    on_disk = target.read_text(encoding="utf-8")
    current = fingerprint_of(on_disk)
    if not given:
        return DocumentChange(
            outcome=ChangeOutcome.NO_FINGERPRINT,
            path=here,
            said=(
                f"「{path}」还在（{len(on_disk)} 字），一个字都没动 —— "
                "删掉它之前得先看看它现在写的是什么。先 read_document 读一遍，"
                "把末尾那个指纹带上再来删。"
            ),
            refused_as=ValueError,
        )
    if given != current:
        return DocumentChange(
            outcome=ChangeOutcome.STALE_FINGERPRINT,
            path=here,
            said=(
                f"「{path}」在你读到之后被改过了（你带的指纹是 {given}，"
                f"现在是 {current}），一个字都没动。重新 read_document 读一遍，"
                "确认那条线真的走完了，再带着新指纹来删 —— 照着旧的那一版删下去会把"
                "别人刚写进去的那一段一起带走。"
            ),
            refused_as=ValueError,
        )

    target.unlink()
    return DocumentChange(
        outcome=ChangeOutcome.OK, path=here, said=f"删掉了：{here}"
    )


def _one_document(root: Path, path: str) -> str:
    """这一份文档那把锁的键。两把锁（协程那把、线程那把）共用它。

    键取**解析后的真实路径**，所以同一份文件的几种写法（同一条路径多写一个分隔符、
    一条指过来的符号链接）共用同一把锁。解析不出来（逃逸路径）在这儿就抛，跟没有锁
    的时候是同一句话。

    两把锁都是**进程内**的，前提是 agent-service 单副本（同 :mod:`app.living.serial`
    那条）。多副本下它们拦不住跨进程的两个写者 —— 那时候要么给这棵树换一层带 CAS 的
    存储，要么先做 leader election。
    """
    return f"world-docs:{resolve_within(root, path)}"


# 每份文档的线程锁，键同 :func:`_one_document`。为什么光有 ``hold`` 那把 asyncio 锁
# 不够，见模块 docstring 最后一节。
#
# 不按事件循环分桶（``serial`` 那边要分是因为 ``asyncio.Lock`` 第一次排队时会绑死当
# 时的循环）：``threading.Lock`` 跟循环无关，一把就够。条目只增不减，但键是这棵树上
# 的文档路径，数量被树本身压着。
_file_locks: dict[str, threading.Lock] = {}
_file_locks_guard = threading.Lock()


def _file_lock(key: str) -> threading.Lock:
    """``key`` 那一份文档的线程锁；还没有就现建一把。"""
    with _file_locks_guard:
        lock = _file_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _file_locks[key] = lock
        return lock


def _inside_the_lock(
    key: str, dropped: threading.Event, work: Callable[..., T], *args: object
) -> T | None:
    """按住 ``key`` 那一份，把 ``work`` 做完。**这一整段都在同一个线程里。**

    ``dropped`` 是"等这个结果的那个协程已经没了"。拿到锁时它已经立起来的话，说明这一
    次还一个字都没碰过盘 —— 那就什么都不做。做了也没人接得到那句话：它不出现在任何一
    次工具返回里，可下一轮读回来树已经变了。

    检查放在锁里、``work`` 之前，过了这一关就不再看它：**已经动过手的那一次要做完**，
    半途收手更坏 —— :func:`_edit` 的 read-modify-write 断在中间就是一份被改了一半的
    文档。返回值这时候没有人接，是什么都不重要。
    """
    with _file_lock(key):
        if dropped.is_set():
            logger.info(
                "world documents 这一次还没碰盘就已经没人等结果了，什么都没做 key=%s",
                key,
            )
            return None
        return work(*args)


async def _touch_disk(key: str, work: Callable[..., T], *args: object) -> T:
    """把 ``work`` 放进线程跑，全程按住 ``key`` 那一份文档。

    两把锁各管一头，缺一不可（为什么，见模块 docstring 最后一节）：``hold`` 管协程那
    一侧的排队和 900 秒上限，:func:`_file_lock` 管线程那一侧、覆盖真正碰盘的那一段。

    取消从这里传给线程：``await`` 被取消时线程还在跑，立一面旗让它在动手之前看得到。
    旗立得晚了（它已经进了 ``work``）就没用，那时候按"做完"算。
    """
    dropped = threading.Event()
    async with hold(key):
        try:
            # ``_inside_the_lock`` 只在"已经没人等结果了"那一种情况下交回 None，而
            # 那一刻这个 await 已经被取消，交回来的东西到不了任何人手里。
            return await asyncio.to_thread(  # type: ignore[return-value]
                _inside_the_lock, key, dropped, work, *args
            )
        except asyncio.CancelledError:
            dropped.set()
            raise


@tool
@tool_error("列文档树失败")
async def list_documents(
    under: Annotated[
        str,
        Field(
            description="只看树上的哪个目录底下，写它在树里的路径；留空就是整棵树"
        ),
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
        Field(description="这一份的路径，照 list_documents 列出来的逐字抄"),
    ],
) -> str:
    """把设定集里的一份从头读一遍。

    路径照 list_documents 列出来的抄，别自己编。写错了它会把那附近有什么摆给你，
    照着挑一个再来。

    一份太长会只读到前面一部分，截了会写在末尾 —— 看到那句话就知道你读到的不是全的，
    别拿它当这一份的全貌去改别的文档。

    **末尾还有一串指纹，那是你读到的这一版的记号。** 想整份换掉这一份（write_document）
    就把它带上：中间要是有别人改过，你的覆盖会被拦下来，而不是把那一版悄悄盖掉。

    Args:
        path: 这一份的路径。

    Returns:
        那一份的正文，末尾缀着这一版的指纹。
    """
    root = documents_root()
    body = await _touch_disk(
        _one_document(root, path), _read_with_fingerprint, root, path
    )
    logger.info("world documents 读 root=%s path=%r（%d 字）", root, path, len(body))
    return body


@tool
@tool_error("这一份没写成")
async def write_document(
    path: Annotated[
        str,
        Field(description=WHERE_TO_WRITE),
    ],
    content: Annotated[str, Field(description="这一份的全部正文")],
    fingerprint: Annotated[
        str,
        Field(
            description=(
                "你上一次 read_document（或 write_document）交回来的那串指纹；"
                "换掉一份已经存在的文档时必填，第一次写下一份留空"
            )
        ),
    ] = "",
) -> str:
    """写下一份文档，或者把已有的一份整份换掉。

    **这是整份重写，不是追加。** 已经有这一份的话，原来的内容全部被换成你这次给的。
    只改其中一句用 edit_document。

    **换掉一份已经存在的文档，要带上你读到的那一版的指纹。** 指纹在 read_document 交回
    来的正文末尾。没带、或者带的那一串已经过期（你读完之后有人改过），这次写入会被拒，
    一个字都不会写 —— 那时候重新 read_document 读一遍，在新的那一版上改，再带着新指纹
    写回来。不这么做的话，你会把别人刚写下的整段悄悄盖掉，而且两边都不会知道。

    第一次写下一份不需要指纹。写成之后交回来的话里会有这一份的新指纹，接着改它带上
    那一串就行，不用再读一遍。

    路径里的目录不存在会自动建，不用先建目录。

    **写自然的描述，不要写属性表。** 这些内容最终会变成她看到的东西 —— 分级标题
    套着字段名、一行一条属性，读起来会很出戏；同样的内容写成连贯的句子（走进去第一
    眼看到什么、那儿正是什么样），才是一个人真的站在那儿看到的东西。

    一份有字数上限，超了会**一个字都不写**并且告诉你 —— 那时候拆成几份，别硬塞。

    Args:
        path: 写到哪。
        content: 这一份的全部正文。
        fingerprint: 你读到的那一版的指纹；新写一份留空。

    Returns:
        一句确认，末尾带着这一份的新指纹。
    """
    root = documents_root()
    change = await _touch_disk(
        _one_document(root, path), _write, root, path, content, fingerprint
    )
    # 抛在记日志之前：被拒的那几次 ``@tool_error`` 自己会记一条带 traceback 的
    # warning，这儿再记一条"写 ... 12 字"只会让日志看起来像写成了。
    change.raise_if_refused()
    logger.info("world documents 写 root=%s path=%r（%d 字）", root, path, len(content))
    return change.said


@tool
@tool_error("这一处没改成")
async def edit_document(
    path: Annotated[
        str, Field(description="改哪一份，照 list_documents 列出来的逐字抄")
    ],
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

    **这只手不要指纹**：find 逐字唯一命中本身就顶了指纹的用 —— 别人把那一段改过了，
    锚点就找不到了，这次替换照样会被拒。

    改一整份用 write_document；改一句用这只手。

    Args:
        path: 改哪一份。
        find: 要被换掉的那一段原文（逐字、唯一）。
        replace: 换成什么；留空 = 删掉那一段。

    Returns:
        一句确认。
    """
    root = documents_root()
    said = await _touch_disk(
        _one_document(root, path), _edit, root, path, find, replace
    )
    logger.info("world documents 改 root=%s path=%r", root, path)
    return said


@tool
@tool_error("这一份没删成")
async def delete_document(
    path: Annotated[
        str, Field(description="删哪一份，照 list_documents 列出来的逐字抄")
    ],
    fingerprint: Annotated[
        str,
        Field(
            description=(
                "你上一次 read_document（或 write_document）交回来的那串指纹；"
                "删掉一份文档时必填"
            )
        ),
    ] = "",
) -> str:
    """把一份文档删掉。

    一条线走完了就是把它那一份删掉 —— 不用留一个"已结束"的标记，设定集里不该有
    只为了记状态而存在的东西。

    **删掉之前要带上你读到的那一版的指纹**，跟整份换掉是同一条规矩：删掉比换掉更狠，
    换掉至少还留下新的那一版，删掉是把这一份整个带走。指纹在 read_document 交回来的
    正文末尾。没带、或者带的那一串已经过期（你读完之后有人改过），这次删除会被拒，
    一个字都不会动 —— 那时候重新 read_document 读一遍，看看新写进去的那一段是什么，
    确认这条线真的走完了再来。

    **只删单份文档，删不了目录。** 整个目录要清就一份一份来。

    Args:
        path: 删哪一份。
        fingerprint: 你读到的那一版的指纹。

    Returns:
        一句确认。
    """
    root = documents_root()
    change = await _touch_disk(
        _one_document(root, path), _delete, root, path, fingerprint
    )
    change.raise_if_refused()
    logger.info("world documents 删 root=%s path=%r", root, path)
    return change.said


# world 一轮里操作设定集的五只手。**她手里没有这几只**（第六节那条边界），
# ``tests/living/test_documents.py`` 钉着这两组不相交。
DOCUMENT_TOOLS = [
    list_documents,
    read_document,
    write_document,
    edit_document,
    delete_document,
]


# ---------------------------------------------------------------------------
# 从外面进来的那一只手
#
# 见模块 docstring「第三个写者」那一节。下面四个入口是这一层交给进程外面的全部，
# 它们和上面五只手的差别只在呈现：结构化的结果，不是渲染给模型看的那几段中文。
# 走的锁和指纹是同一套 —— 每一个都落在 :func:`_touch_disk` 上。
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TreeListing:
    """一次列目录的结果。

    :func:`list_tree` 交给模型的是一段话：空树是一句、卷没挂上是另一句。程序这一侧要
    的是清单本身，外加"树在不在"这个判断 —— 从那段话里解析出来会在措辞一改的时候静默
    失效，而这两种处境的下一步完全不同：一个是照着写就行，一个是去找运维。
    """

    entries: tuple[str, ...]
    mounted: bool


@dataclass(frozen=True)
class WholeDocument:
    """一份文档此刻的**全文**和它的指纹。

    ``path`` 是解析后相对树根的那条路径，不暴露挂载点。正文不截断，理由见模块
    docstring「第三个写者」那一节最后一段。
    """

    path: str
    content: str
    fingerprint: str


def _listing(root: Path, under: str) -> TreeListing:
    """``under`` 底下的清单。根目录不在的两种处境由 ``mounted`` 分开。"""
    base = root if under.strip() in ("", ".", "./", _SEP) else resolve_within(root, under)
    if not base.exists():
        if base == root.resolve() or base == root:
            return TreeListing(entries=(), mounted=documents_mount().exists())
        raise FileNotFoundError(f"「{under}」这个目录不存在。")
    if base.is_file():
        # ``rglob`` 对一个文件交回空，于是"路径写错了"会长得跟"这个目录是空的"一样。
        raise NotADirectoryError(f"「{under}」是一份文档不是一个目录。")
    return TreeListing(entries=tuple(_entries(base, root)), mounted=True)


def _whole(root: Path, path: str) -> WholeDocument:
    """整篇正文加上它的指纹。两次触盘都在锁里，所以指纹配的一定是这一段正文。"""
    target = resolve_within(root, path)
    if target.is_dir():
        raise IsADirectoryError(f"「{path}」是一个目录不是一份文档。")
    if not target.exists():
        raise FileNotFoundError(f"没有「{path}」这一份。")
    content = target.read_text(encoding="utf-8")
    return WholeDocument(
        path=_relative(root, target),
        content=content,
        fingerprint=fingerprint_of(content),
    )


async def listing(under: str = "") -> TreeListing:
    """列目录。跟 ``list_documents`` 一样不进锁 —— 一把锁按的是一份文档，不是一棵树。"""
    root = documents_root()
    found = await asyncio.to_thread(_listing, root, under)
    logger.info(
        "world documents 外部列目录 root=%s under=%r（%d 项）",
        root,
        under,
        len(found.entries),
    )
    return found


async def read_whole(path: str) -> WholeDocument:
    """读一份文档的全文和指纹。"""
    root = documents_root()
    found = await _touch_disk(_one_document(root, path), _whole, root, path)
    logger.info(
        "world documents 外部读 root=%s path=%r（%d 字）",
        root,
        path,
        len(found.content),
    )
    return found


async def rewrite(path: str, content: str, fingerprint: str = "") -> DocumentChange:
    """整份重写。指纹那道门跟 :func:`_write` 是同一道 —— 用的就是它。"""
    root = documents_root()
    change = await _touch_disk(
        _one_document(root, path), _write, root, path, content, fingerprint
    )
    logger.info(
        "world documents 外部写 root=%s path=%r（%d 字）结果=%s",
        root,
        path,
        len(content),
        change.outcome,
    )
    return change


async def remove(path: str, fingerprint: str = "") -> DocumentChange:
    """删掉一份文档。同样只删单份，不删目录。"""
    root = documents_root()
    change = await _touch_disk(
        _one_document(root, path), _delete, root, path, fingerprint
    )
    logger.info(
        "world documents 外部删 root=%s path=%r 结果=%s", root, path, change.outcome
    )
    return change
