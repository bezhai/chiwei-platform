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
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from pydantic import field_validator
from sqlalchemy import text

from app.data.session import get_session
from app.living.records import _require_aware
from app.runtime.data import Data, Key
from app.runtime.migrator import _table_name
from app.runtime.persist import insert_idempotent, select_latest

# 一次给她列几张。跟 :data:`app.living.reading.FILE_LIST_LIMIT` 同一条理由：这是"一屏
# 摆得下多少"的显示口径，不是"她该记得几张" —— 被挤下去的那些没有消失，句柄那条路照样
# 取得到。
PICTURE_LIST_LIMIT = 20

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
    *, lane: str, persona_id: str, limit: int = PICTURE_LIST_LIMIT
) -> list[Picture]:
    """她做过的图，最近的在前。

    只是"一屏摆得下多少"，不是她只剩这几张：被挤下去的那些用 :func:`her_picture`
    照样取得到。
    """
    sql = (
        f"SELECT * FROM {_TABLE} "
        f"WHERE lane = :lane AND persona_id = :persona_id "
        f"ORDER BY made_at DESC LIMIT :limit"
    )
    async with get_session() as s:
        rows = (
            await s.execute(
                text(sql),
                {"lane": lane, "persona_id": persona_id, "limit": limit},
            )
        ).mappings().all()
    return [Picture(**{k: r[k] for k in Picture.model_fields}) for r in rows]
