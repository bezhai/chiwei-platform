"""``common_message`` 的映射得跟公共层那张表对得上。

这张表是三个服务共写的，列的形状是跨服务约定，不是本服务的私事。所以这里钉的是
**语义**，不只是"有这一列"。
"""

from sqlalchemy import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PgUUID

from app.data.models import CommonMessage


def _column(name: str):
    return CommonMessage.__table__.columns[name]


def test_mentioned_common_user_ids_is_a_uuid_array():
    """被 @ 的人存公共层 id 的数组。

    元素是 uuid 而不是 text：这一列里的每个值都要跟 ``common_user.common_user_id``
    比对，两边类型不一致的话比对会在数据库那层静默退化成字符串比较。
    """
    col = _column("mentioned_common_user_ids")
    assert isinstance(col.type, ARRAY)
    assert isinstance(col.type.item_type, (PgUUID, type(_column("common_user_id").type)))


def test_mentioned_common_user_ids_keeps_unknown_apart_from_nobody():
    """**NULL 不能被消掉。**

    NULL = 没人算过这条消息（加列前的存量行、QQ 的行、飞书新写入方上线前的行）；
    ``[]`` = 算过，确实谁都没点。读的一侧把 NULL 当"不知道"，也就是不算被点名。

    这一列一旦 NOT NULL 或者带了默认值，存量行就会凭空变成"确认没人被 @"，
    而且合并之后再也分不开。这条用例就是拦这个的。
    """
    col = _column("mentioned_common_user_ids")
    assert col.nullable is True
    assert col.default is None
    assert col.server_default is None


def test_agent_outbound_id_is_a_uuid_column_not_the_wire_string():
    """这一行是她哪一次主动开口的产物，存的是那个 uuid。

    渠道服务写进来的是 ``proactive:<uuid>`` **去掉前缀之后**那一段，列是 uuid 类型
    （TS 侧 ``@Column({ type: 'uuid', nullable: true })``）。这边映射成 text 的话，
    引擎按 uuid 反查时两边类型不一致，比对在数据库那层静默退化成字符串比较 ——
    表现是永远对不上账，一句报错都没有。
    """
    col = _column("agent_outbound_id")
    assert isinstance(col.type, type(_column("common_message_id").type))


def test_agent_outbound_id_keeps_unrecorded_apart_from_not_proactive():
    """**NULL 不能被消掉。**

    三种行都落在 NULL 上：加列之前的存量行、QQ 渠道写的行、以及所有被动回复的行。
    NOT NULL 或者给了默认值，就把"没记过"和"确实不是主动发的"合并了，而且合并之后
    再也分不开 —— 那正是这一列存在的理由被抹掉。
    """
    col = _column("agent_outbound_id")
    assert col.nullable is True
    assert col.default is None
    assert col.server_default is None
