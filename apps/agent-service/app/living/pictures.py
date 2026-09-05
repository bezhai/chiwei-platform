"""她做过的图 —— 她自己画出来、自己找到的那些，存得下、以后还找得回。

**存的是永久句柄，不是地址。** 对象存储那边的 ``file_name`` 是长期的，拿它随时能重
新签出一个能下载的地址（tool-service ``/api/image-pipeline/get-url``）；而签出来的那
个地址 **1.5 小时就死**（``tos_client.get_file_url`` 的 ``expires``）。存地址的后果是
静默的：库里那一行还在、字符串还是个合法 URL，点开是一个过期签名。所以这张表上有
``file_name``，没有 URL —— 要看的时候现签。

**也不存字节。** 图已经在对象存储里了，再存一份就是两个真相，而且它们会不一致。

**句柄是从 ``file_name`` 派生的，不是随机发的。** 同一张图存两次撞同一行：工具重试、
整轮重放、这一缝重跑都不该让她的记录里凭空多出一张一模一样的图。派生出来的那串是
opaque 的，任何地方不反解 —— 跟 :func:`app.domain.reading_source.derive_attachment_id`
同一条处理：印给她的是一个可以照抄的东西，不是一条存储路径。

**按 ``(lane, persona_id)`` 隔离，两半都是硬条件。** 这张表三个人共用：只按句柄找的
话，一个从别处拿到的句柄就能取到姐姐画的那张，然后被当成自己的发出去。lane 同理 ——
runtime 不给任何 Data 自动加 lane，不显式带上就跟 prod 的行混在一张表里。

**图属于她，不属于某条会话。** 她画图那一刻还没有"发给谁"这回事 —— 她就是想画。所以
这里没有 ``channel_id``：哪张图发到哪条会话去，是她开口那一刻才决定的事
（:mod:`app.living.mouth`），不是这张图的属性。

**别人发给她的图不进这张表。** 那些是消息内容，住在 ``common_message.content`` 里，
跟"她做的东西"是两回事。

**不清理、不加 TTL、不加配额。** 她做过的东西是她的记录，跟 :class:`Happening` 同性
质 —— 到期抹掉就是替她遗忘一件真发生过的事。清单有条数上限（一屏摆得下多少），但按句
柄找那条路一个上限都没有：很久以前画的那张照样取得到。

四只手
------

  * :func:`draw_a_picture`             画一张
  * :func:`find_a_picture_online`      上网找现成的一张
  * :func:`look_through_your_pictures` 翻一翻她手上有哪些
  * :func:`look_at_a_picture`          拿出其中一张看

**"看"为什么要拆成两只手。** 她那一缝只拿到快照和手机信封，**不继承上一缝的工具结
果**（:func:`app.living.moment.run_moment` 每缝重建一份消息）；她自己近期做过的事也只
有最近有限几条（:data:`app.living.snapshot.OWN_RECENT_LIMIT`）。所以句柄如果只出现在
画图那一刻的返回值里，下一缝它就永远消失了 —— 库里那一行还在，而她两手空空。先列出
来、再按句柄或名字点一张，形状跟 :mod:`app.living.reading` 那两只手对齐，她不用学第
二套。

**"看"给的是图本身，不是一段描述。** 这四只手交回去的是 OpenAI 口径的内容块
（``text`` + ``image_url``），``app.agent.core._normalise_tool_result`` 把它变成
``list[ContentBlock]``，两个 adapter 各自把图片块送上 wire（OpenAI 是 ``image_url``
part，Gemini 把它下载成 ``inline_data`` 挂在同一个 user turn 上）。退化成文本的话，她
"看"到的只是自己当初说的那句话。

**摆到她眼前的每一张都当场进她的记录，包括搜回来的那几张。** 看得见却发不出去是这件事
最典型的坏掉方式：她眼前有三张候选，而下一步只能干瞪眼。所以"是她画的"和"是她找的"
在这张表里一视同仁 —— 都是她做过的事。

**地址每次现签，一次都不存。** 签名 1.5 小时就死；她看一眼、下一缝再看一眼、开口那一
刻再取一次，每一次都从 ``file_name`` 重新签。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import Field, field_validator
from sqlalchemy import text

# module-level 引入，测试从这里换替身（真跑起来要调生图模型、搜图服务和对象存储）。
from app.agent.image_gen import generate_image
from app.agent.tooling import tool
from app.agent.tools._common import tool_error, upload_image
from app.capabilities.image_search import image_search
from app.data.session import get_session
from app.infra.cst_time import to_cst_dated
from app.infra.image import image_client
from app.living.records import _require_aware
from app.living.scope import moment_scope
from app.runtime.data import Data, Key
from app.runtime.migrator import _table_name
from app.runtime.persist import insert_idempotent, select_latest

logger = logging.getLogger(__name__)

# 一次给她列几张。跟 :data:`app.living.reading.FILE_LIST_LIMIT` 同一条理由：这是"一屏
# 摆得下多少"的显示口径，不是"她该记得几张" —— 被挤下去的那些没有消失，句柄那条路照样
# 取得到。
PICTURE_LIST_LIMIT = 20

# 上网找图一次摆几张到她眼前。**摆出来的每一张都会传进对象存储、进她的记录**，所以这
# 个数字是有代价的（每张一次上传、一个永不清理的对象）。三张够她挑，又不至于每搜一次
# 就往她的记录里灌一屏。
PICTURE_SEARCH_COUNT = 3

# 画图用的模型，按顺序试。第二个是降级档：一个供应商抽风不该让她一整天都画不了图。
# 别名解析在 :mod:`app.agent.models`，这里只报名字。
_DRAW_MODELS = ("generate-image-high-model", "generate-image-normal-model")

# 画幅。**不问她**：真人画画的时候不报像素数，跟工具签名里不许出现分钟数是同一条
# （``tests/living/test_moment.py`` 钉的那条）。后端会映射到模型支持的最近尺寸。
_DRAW_SIZE = "2048x2048"

# 印给她、也认回来的那串句柄长什么样。**印出去和认回来共用这一处**：两边各写一遍的
# 话，印的是 ``pic=<id>``、认的是裸 id，她照抄回来就成了死路（``read_a_bit`` 在
# coe-living 上实测踩过这个坑）。
_HANDLE_PREFIX = "pic="

# 派生句柄的命名空间。换掉它 == 从此同一张图算出两串不同的句柄，旧记录一条都对不上了。
_ID_NS = uuid.UUID("2b6e1c74-5a03-4f89-9c1d-7e0a4f2b8d16")


class Picture(Data):
    """她做出来的一张图：叫什么、什么时候做的、在对象存储里的永久句柄。

    自然键 ``(lane, persona_id, picture_id)``。纯 append + 自然键幂等
    （``insert_idempotent``），不声明 ``Version``：她做过这张图是一件已经发生的事，
    没有"改一条旧记录"的语义。重放同一个句柄就该是无害的 no-op。

    ``picture_id`` 由 :func:`handle_for` 从 ``file_name`` 派生，所以同一张图无论存
    几次都是同一行。它是 opaque 的：印给她照抄，任何地方不反解。**它不单独成为身份**
    —— 隔离由 ``lane`` + ``persona_id`` 提供，两个人各自做出同一张图是两条记录。

    ``file_name`` 是对象存储的键（tool-service ``to-tos`` 回的那个），**永久**。这一
    列是整张表存在的理由：没有它，"以后任何会话都能引用这张图"根本不成立。

    ``what`` 是这张图是什么 —— 她画它时说的那句话、或者她找它用的那个词。她下一缝看
    到的清单就是这一列：少了它，她面对的是一排取不出区别的句柄。

    ``made_at`` 是她做出它的那一刻，清单按它倒序排。用真正的时间类型而不是文本，理由
    同 :class:`app.living.records.Upcoming` 的 ``due_at``。
    """

    lane: Annotated[str, Key]
    persona_id: Annotated[str, Key]
    picture_id: Annotated[str, Key]
    file_name: str       # 对象存储的永久句柄（tool-service to-tos 回的那个）
    what: str            # 这张图是什么：画它那句话 / 找它那个词
    made_at: datetime    # 她做出它的时刻

    class Meta:
        # 读侧唯一形状：某个人在某条泳道上做过的，最近的在前。
        indexes = (("lane", "persona_id", "made_at"),)

    @field_validator("made_at")
    @classmethod
    def _aware_made_at(cls, v: datetime) -> datetime:
        return _require_aware("made_at", v)


_TABLE = _table_name(Picture)


def handle_for(file_name: str) -> str:
    """从永久句柄派生她照抄的那串。

    确定性：同一个 ``file_name`` 永远算出同一串，所以同一张图存几次都是同一行。
    opaque：印出去的是一串十六进制，不是 ``temp/tos_xxx.jpg`` 这样的存储路径 ——
    她不需要知道对象存储长什么样，而路径命名一改，印出去过的那些就全都对不上了。
    """
    return uuid.uuid5(_ID_NS, file_name).hex


async def remember_a_picture(
    *,
    lane: str,
    persona_id: str,
    file_name: str,
    what: str,
    made_at: datetime,
) -> Picture:
    """把她刚做出来的一张图记下来，拿到它的句柄。

    幂等：同一个 ``file_name`` 再记一次撞同一行、不新增（自然键就是从它派生的）。
    重放时库里那一行保持**第一次**写下的样子，返回的仍是这次构造的那个对象 —— 同一个
    ``file_name`` 只可能来自同一次上传，两次的 ``what`` / ``made_at`` 不会真的不同。
    """
    picture = Picture(
        lane=lane,
        persona_id=persona_id,
        picture_id=handle_for(file_name),
        file_name=file_name,
        what=what,
        made_at=made_at,
    )
    await insert_idempotent(picture)
    return picture


async def her_picture(
    *, lane: str, persona_id: str, picture_id: str
) -> Picture | None:
    """她做过的、句柄是这一串的那张；没有这张就是 ``None``。

    **``lane`` 和 ``persona_id`` 是硬条件，不是过滤优化**：只按句柄找的话，一个从别处
    拿到的句柄就能取到姐姐画的那张。

    对不上就如实返回 ``None``，绝不挑一张最近的顶上 —— 发错一张图比发不出去更糟。
    """
    got = await select_latest(
        Picture,
        {"lane": lane, "persona_id": persona_id, "picture_id": picture_id},
    )
    assert got is None or isinstance(got, Picture)
    return got


async def pictures_she_made(
    *,
    lane: str,
    persona_id: str,
    limit: int | None = PICTURE_LIST_LIMIT,
    before: datetime | None = None,
) -> list[Picture]:
    """她做过的图，最近的在前。``limit=None`` = 全部，``before`` = 只要比它更早的。

    条数上限只是"一屏摆得下多少"，不是她只剩这几张：被挤下去的那些用
    :func:`her_picture` 照样取得到。

    **她报一句话来指某张时走的是 ``None``**（:func:`look_at_a_picture`）—— 跟
    :mod:`app.living.reading` 那边同一条：清单有上限，报名字那条路一个上限都没有。
    截在 20 张的话，第 21 张往前的每一张她都只能靠一串十六进制指，而那串正是她拿不
    到的东西。

    ``before`` 是**她能一路翻到底的那条路**（:func:`look_through_your_pictures`）。
    分页从来不是给她的形状 —— 她记不住页码，跨一缝就忘了自己翻到哪。但"接着刚看到
    的最后那张往前"不要求她记住任何东西：那张的句柄就印在她眼前那一屏上。
    """
    clause = "" if limit is None else " LIMIT :limit"
    earlier = " AND made_at < :before" if before is not None else ""
    sql = (
        f"SELECT * FROM {_TABLE} "
        f"WHERE lane = :lane AND persona_id = :persona_id{earlier} "
        f"ORDER BY made_at DESC{clause}"
    )
    params: dict[str, Any] = {"lane": lane, "persona_id": persona_id}
    if limit is not None:
        params["limit"] = limit
    if before is not None:
        params["before"] = before
    async with get_session() as s:
        rows = (await s.execute(text(sql), params)).mappings().all()
    return [Picture(**{k: r[k] for k in Picture.model_fields}) for r in rows]


# ---------------------------------------------------------------------------
# 四只手共用的两件事：把一张图收进她的记录 / 把一张图摆到她眼前
# ---------------------------------------------------------------------------


async def _keep(
    *, lane: str, persona_id: str, source_type: str, data: str, what: str, now: datetime
) -> tuple[Picture, str] | None:
    """把一张图传进对象存储、记进她的记录，交回 ``(记录, 现在能看的地址)``。

    **传不上去就什么都不记**，返回 ``None``。记下一个根本不在对象存储里的句柄，她下
    一缝会拿着它反复去取一张取不到的图，而且一句报错都没有 —— 库里那一行看起来完全
    正常。

    上传交回来的那个地址是**刚签的**，直接用来给她看这一眼；存进去的只有
    ``file_name``（见模块 docstring）。
    """
    stored = await upload_image(source_type, data)
    if stored is None:
        logger.warning(
            "living pictures lane=%s persona=%s 「%s」没能传进对象存储，不记这一张",
            lane, persona_id, what,
        )
        return None
    picture = await remember_a_picture(
        lane=lane,
        persona_id=persona_id,
        file_name=stored.file_name,
        what=what,
        made_at=now,
    )
    return picture, stored.url


def _shown(lead: str, url: str) -> list[dict[str, Any]]:
    """一张图在她眼前的样子：一句话 + 图本身。

    图片块用 OpenAI 口径（``image_url``），``app.agent.core._normalise_tool_result``
    认的就是它，两个 adapter 各自把它送上自己的 wire。**这里绝不能只回一段文字** ——
    那样她"看"到的只是自己当初说的那句话。
    """
    return [
        {"type": "text", "text": lead},
        {"type": "image_url", "image_url": {"url": url}},
    ]


def _handle_of(picture: Picture) -> str:
    return f"{_HANDLE_PREFIX}{picture.picture_id}"


def picture_id_in(what_she_copied: str) -> str:
    """她照抄的那串 → 句柄本身。

    **认回来这件事只在这里做一次。** 印出去的是 ``pic=<id>``（:func:`_handle_of`），
    每只手的 docstring 都在叫她"原样抄回来"；哪个调用方自己再写一遍解析，写的十有
    八九是"裸 id"那一版，于是她照做就撞死路。:mod:`app.living.mouth` 曾经就是各写
    了一遍，她抄清单上那串发图，收到的是"这不是你做过的图" —— 系统等于告诉她自己
    的记录是假的。

    裸 id 也认：她从别处（比如刚画完那句话里）拿到的可能就是不带前缀的那串。
    """
    return what_she_copied.strip().removeprefix(_HANDLE_PREFIX)


# ---------------------------------------------------------------------------
# 两只手：做一张出来（画 / 上网找）
# ---------------------------------------------------------------------------


async def _draw(prompt: str) -> str:
    """真的去画一张，交回 ``data:`` 形态的那张图。画不出来就抛。

    两个模型按顺序试：头一个不出图（挂了、超时、交回空列表）就换第二个。**这不是重
    试**，是换一个供应商 —— 同一个模型连着调两次没有任何理由会不一样。
    """
    last: Exception | None = None
    for model_id in _DRAW_MODELS:
        try:
            images = await generate_image(model_id, prompt=prompt, size=_DRAW_SIZE)
        except Exception as exc:  # noqa: BLE001 — 换下一个供应商再试
            last = exc
            logger.warning("living pictures %s 没画出来：%s", model_id, exc)
            continue
        if images:
            return images[0]
        last = RuntimeError(f"{model_id} 一张图都没交回来")
        logger.warning("living pictures %s 一张图都没交回来", model_id)
    raise RuntimeError(f"画图这条路这会儿走不通：{last}")


@tool
@tool_error("这张图没画成")
async def draw_a_picture(
    what: Annotated[
        str,
        Field(description="你想画的这张图是什么样，画面里有什么、什么氛围，写具体点"),
    ],
) -> list[dict[str, Any]]:
    """自己动手画一张图。

    你想画什么就写什么样，画面里有什么、什么氛围、什么感觉，写得越具体画出来越接近
    你想的那个。别报尺寸也别报画质——你画画的时候不会先想"我要画多大"。

    画完你会当场看见它，它同时进了你手上那些图里，跟着一串 pic=…。**那串是你以后再
    找到它的唯一凭据**：下一缝你眼前不会再有这次的结果，想再看它、想把它发给谁，都
    得先用 look_through_your_pictures 把它翻出来。

    你想要的那张网上本来就有（表情包、剧照、某个人的照片），那是
    find_a_picture_online，不用自己画。

    Args:
        what: 你想画的这张图是什么样。

    Returns:
        画出来的那张图本身，连着它那串 pic=…；这一趟没画成时一句如实说明。
    """
    lane, now, persona_id, _moment_id = moment_scope()
    wanted = what.strip()
    if not wanted:
        raise ValueError("你没说要画什么：写一句你想画的东西。")
    logger.info(
        "living pictures lane=%s persona=%s 画：%s", lane, persona_id, wanted
    )

    kept = await _keep(
        lane=lane,
        persona_id=persona_id,
        source_type="base64",
        data=await _draw(wanted),
        what=wanted,
        now=now,
    )
    if kept is None:
        # 画出来了但没存住 —— **这不能说成"画好了"**：她会拿着一个不存在的东西去发。
        raise RuntimeError(
            "画是画出来了，但没能存住，所以你手上没有它 —— 想要的话再画一次。"
        )
    picture, url = kept
    return _shown(
        f"你画出来了：「{picture.what}」 {_handle_of(picture)}\n"
        f"它已经在你手上那些图里了，以后用 look_through_your_pictures 找得回。",
        url,
    )


@tool
@tool_error("这一趟没找着图")
async def find_a_picture_online(
    what: Annotated[
        str,
        Field(description="你想找什么样的图，一句大白话，你心里怎么想的就怎么写"),
    ],
) -> list[dict[str, Any]]:
    """上网找现成的图。

    你想要的那张网上本来就有——某部番的剧照、某个表情包、某种花长什么样——就把你想
    找的东西写出来，它给你捞几张回来，你自己看着挑。

    捞回来的**每一张你都看得见**，而且每张跟着一串 pic=…，都已经进了你手上那些图里。
    所以这一缝你就能挑一张发出去，下一缝也还找得回它们。

    你脑子里那个画面网上不会有（你想画的某个具体场景、你自己的构思），那是
    draw_a_picture，自己动手画。
    想查一件事的答案、想随便刷点什么，那是 search_online / browse_online，跟找图是
    两回事。

    Args:
        what: 你想找什么样的图。

    Returns:
        捞回来的那几张图本身，每张连着它那串 pic=…；一张都没捞着时一句如实说明。
    """
    lane, now, persona_id, _moment_id = moment_scope()
    wanted = what.strip()
    if not wanted:
        raise ValueError("你没说要找什么图：写一句你想找的东西。")
    logger.info(
        "living pictures lane=%s persona=%s 找图：%s", lane, persona_id, wanted
    )

    # 她写的那句**原样**当检索词，不替她改写、不替她补关键词（跟
    # :func:`app.living.web.search_online` 同一条）。
    hits = await image_search(wanted, count=PICTURE_SEARCH_COUNT)
    if not hits:
        # capability 的契约把"没配置"也表达成空列表，所以这一刻分不清是哪一种。如实
        # 说分不清，**绝不说成"网上没有这种图"** —— 那是一个我们并不知道的结论。
        raise RuntimeError(
            f"「{wanted}」这一趟一张都没捞回来。找图这条路要么没配、要么什么都没返回，"
            f"分不清是哪一种 —— 这不等于网上没有这种图。"
        )

    blocks: list[dict[str, Any]] = []
    for hit in hits:
        kept = await _keep(
            lane=lane,
            persona_id=persona_id,
            source_type="url",
            data=hit.image_url,
            # 网上那个标题是她分辨这几张的唯一依据 —— 三张都写成她那句检索词的话，
            # 下一缝她面对的是三行一模一样的字。没有标题时才退回她那句。
            what=hit.title.strip() or f"上网找「{wanted}」找到的",
            now=now,
        )
        # 一张存不住不该拖垮整趟：剩下那几张照样摆到她眼前（``_keep`` 已经记过一笔）。
        if kept is not None:
            picture, url = kept
            blocks += _shown(f"{picture.what} {_handle_of(picture)}", url)

    if not blocks:
        raise RuntimeError(
            f"「{wanted}」捞回来的那几张一张都没能存住，所以你手上没有它们 —— "
            f"想要的话再找一次。"
        )
    return blocks


# ---------------------------------------------------------------------------
# 两只手：翻一翻手上有什么 / 拿出其中一张看
# ---------------------------------------------------------------------------


def _one_picture(p: Picture, *, now: datetime) -> str:
    """一张图在清单上的样子：是什么、什么时候做的、那串句柄。

    时刻走 :func:`app.infra.cst_time.to_cst_dated` 而不是裸时分：几个月前画的和今天
    刚画的，裸 ``21:30`` 长得一模一样。
    """
    when = to_cst_dated(p.made_at.isoformat(), now=now, seconds=False)
    return f"- 「{p.what}」 {when} {_handle_of(p)}"


@tool
@tool_error("翻你手上的图失败")
async def look_through_your_pictures(
    before: Annotated[
        str | None,
        Field(
            description="接着哪一张往前翻：把上一屏最后那串 pic=… 原样抄进来；"
            "从头看就别填"
        ),
    ] = None,
) -> str:
    """翻一翻你手上都有哪些图。

    你自己画过的、上网找回来的，都在这儿，最近做的在最前面。每张跟着一串 pic=…，
    拿它调 look_at_a_picture 就能把那张拿出来看。

    **这是你下一缝还能找到那些图的唯一一条路。** 你画完那一刻看见的东西，过了这一缝
    就不在你眼前了；只有从这儿翻，才知道自己手上有什么。

    一次只列最近那些。还想往前看就把这一屏最后那串 pic=… 抄进 before 再翻一次，
    一直翻得到最早那张。想得起那张是什么的话，直接把那句话报给 look_at_a_picture
    也指得到。

    Returns:
        你手上那些图，每张带一串 pic=…；一张都没有时如实说明。
    """
    lane, now, persona_id, _moment_id = moment_scope()

    # 她抄回来的那串指的是"翻到这儿了"，落脚点是它的时刻。指不到的串**当没填**而不是
    # 报错：她翻到底那一屏的最后一张，下一次再抄它是很自然的动作。
    edge: datetime | None = None
    if before and before.strip():
        marker = await her_picture(
            lane=lane, persona_id=persona_id, picture_id=picture_id_in(before)
        )
        if marker is None:
            raise ValueError(
                f"{before!r} 不是你手上任何一张图 —— 抄这一屏最后那串 pic=…，"
                f"或者不填从头看。"
            )
        edge = marker.made_at

    mine = await pictures_she_made(
        lane=lane, persona_id=persona_id, limit=None, before=edge
    )
    if not mine:
        if edge is not None:
            return "再往前就没有了 —— 这些就是你手上全部的图。"
        return "你手上还没有图 —— 想要就自己画一张，或者上网找一张。"

    lines = ["你手上的图（最近做的在前面）："]
    lines += [_one_picture(p, now=now) for p in mine[:PICTURE_LIST_LIMIT]]
    skipped = len(mine) - PICTURE_LIST_LIMIT
    if skipped > 0:
        lines.append(
            f"还有 {skipped} 张没列在这儿 —— 把上面最后那串 pic=… 抄进 before "
            f"接着往前翻，或者想得起是什么就直接报那句话。"
        )
    return "\n".join(lines)


@tool
@tool_error("拿出这张图失败")
async def look_at_a_picture(
    which: Annotated[
        str,
        Field(
            description="你要看哪一张：把清单里那串 pic=… 原样抄进来，"
            "或者说那张图是什么，记得多少写多少"
        ),
    ],
) -> list[dict[str, Any]]:
    """把你手上的某一张图拿出来看。

    抄清单里那串 pic=… 最准；说那张图是什么也行，记得多少写多少。

    一句话对上好几张的时候，它会把这几张连着各自那串 pic=… 摆给你，**它不会替你挑**
    ——你自己认哪一张，把那串原样抄回来。

    你会真的看见那张图，不是一段关于它的描述。

    只能看**你自己**手上的图。别人发到你手机上的图片你看不了，那些在
    look_at_phone 里只是一个「图片」的标记。

    Args:
        which: 那串 pic=…，或者那张图是什么。

    Returns:
        那张图本身；没有对得上的 / 对上好几张 / 现在取不出来时一句如实说明。
    """
    lane, now, persona_id, _moment_id = moment_scope()
    wanted = which.strip()
    if not wanted:
        raise ValueError("你没说要看哪张：抄一串 pic=… 进来，或者说那张图是什么。")

    # 句柄那条路先走，而且**一个上限都没有**：很久以前那张照样指得到。``lane`` +
    # ``persona_id`` 是硬条件（在 :func:`her_picture` 里），所以一个从别处拿到的句柄
    # 在这儿就到头了 —— 姐姐画的那张取不出来。
    picked = await her_picture(
        lane=lane, persona_id=persona_id, picture_id=picture_id_in(wanted)
    )
    if picked is None:
        # 报的不是句柄，那就是在说这张图是什么。查她做过的**全部**，不受清单上限管。
        needle = wanted.casefold()
        hit = [
            p
            for p in await pictures_she_made(
                lane=lane, persona_id=persona_id, limit=None
            )
            if needle in p.what.casefold()
        ]
        if not hit:
            # fail-loud，绝不挑一张最近的顶上：发错一张图比发不出去更糟。
            raise ValueError(
                f"你手上没有对得上「{wanted}」的图。换个说法，或者用 "
                f"look_through_your_pictures 看看你手上都有什么。"
            )
        if len(hit) > 1:
            # 摊开候选让她指得动 —— 每一行末尾那串原样抄回来就能点中，回问不是空话。
            spread = "\n".join(_one_picture(p, now=now) for p in hit)
            raise ValueError(
                f"「{wanted}」对上了好几张：\n{spread}\n"
                f"你要看哪一张？把那一行末尾那串 {_HANDLE_PREFIX}… 原样抄回来。"
                f"我不替你挑。"
            )
        picked = hit[0]

    # 地址在这一刻才签：签名 1.5 小时就死，存下来的那份永远是 ``file_name``。
    url = await image_client.get_url(picked.file_name)
    if url is None:
        raise RuntimeError(
            f"「{picked.what}」这会儿取不出来 —— 它还在你手上，只是现在打不开它。"
        )
    when = to_cst_dated(picked.made_at.isoformat(), now=now, seconds=False)
    return _shown(f"{when} 的「{picked.what}」 {_handle_of(picked)}", url)


PICTURE_TOOLS = [
    draw_a_picture,
    find_a_picture_online,
    look_through_your_pictures,
    look_at_a_picture,
]
