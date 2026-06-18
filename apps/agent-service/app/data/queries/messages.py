"""Chat message queries backed by common_* tables.

agent-service consumes ``common_message`` / ``common_conversation`` /
``common_agent_response`` only. The returned read model keeps the existing
agent-service payload names, where ``message_id`` is the common message id.
"""
from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import func, or_, text, update
from sqlalchemy.future import select

from app.data.message_record import CommonMessageRecord
from app.data.models import (
    CommonAgentResponse,
    CommonConversation,
    CommonMessage,
    CommonUser,
)
from app.infra import cst_time
from app.runtime.db import auto_tx, current_session

__all__ = [
    "find_cross_chat_messages",
    "find_message_content",
    "find_username",
    "find_group_download_permission",
    "find_message_by_id",
    "find_last_bot_reply_time",
    "find_gray_config",
    "find_user_messages_after",
    "find_recent_chat_messages",
    "find_messages_with_user_chat_persona_by_root",
    "find_messages_with_user_chat_persona_in_chat",
    "find_persona_spoken_chats_in_window",
    "update_messages_tos_files",
]


def _bot_config_persona():
    """assistant 行经 ``bot_config(bot_name → persona_id)`` 兜底取发言 persona。

    proactive 出站行真实落库形态是 ``response_id=NULL`` 且**没有**
    ``common_agent_response`` 行（worker 口径：proactive session_id=null → responseId
    不挂、不写 agent_response），所以单靠 ``response_id → common_agent_response.session_id``
    join 必拿 None —— 那会让 proactive 行被判成 persona=None，下游误判为真人输入（串味）。
    bot_name → persona 是它在真实链路里唯一能拿到的归属来源。

    ``bot_config`` 由 channel-server 管理、不在 agent-service 的 SQLAlchemy 模型里
    （见 ``models.py`` 顶注、``resolve_persona_id`` 同样裸表读它），用相关标量子查询读
    裸表：``bot_name = common_message.bot_name`` 与外层 ``common_message`` 关联，只取
    ``is_active`` 的映射。

    **承重红线（codex 必改 1）**：子查询额外 correlate 外层 ``role = 'assistant'``。
    channel-server 给真人 ``role='user'`` 行**也写 bot_name``（storeLarkInboundMessage
    给 inbound user 行落 bot_name=botName、claim 时再写），裸 ``bot_name`` 子查询会对
    user 行也命中、把真人话错归成某 persona——查询合同被串脏。human-chat 路径下游
    ``is_self`` 第一个条件就是 ``role == 'assistant'`` 遮住不炸，但睡前回顾路径
    （``find_persona_spoken_chats_in_window`` → ``review``）**直接用 persona 分"她说的
    vs 用户说的"、没有 role gate**，会把真人话当成她自己说的。加 role 限定让外层非
    assistant 行（user / 其它）→ 子查询无命中 → 返回 NULL。assistant proactive 出站行
    （``response_id=NULL``、无 agent_response）仍经 bot_name → persona 兜底拿到归属。
    """
    return (
        select(text("persona_id"))
        .select_from(text("bot_config"))
        .where(
            text(
                "bot_name = common_message.bot_name AND is_active = true "
                "AND common_message.role = 'assistant'"
            )
        )
        .limit(1)
        .scalar_subquery()
    )


def _uuid(value: str | UUID | None) -> UUID | None:
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except ValueError:
        return None


def _uuid_list(values: list[str] | set[str]) -> list[UUID]:
    out: list[UUID] = []
    for value in values:
        parsed = _uuid(value)
        if parsed is not None:
            out.append(parsed)
    return out


def _content_item_to_v2(item: dict) -> dict:
    if "type" in item:
        return item

    kind = item.get("kind")
    if kind == "text":
        return {"type": "text", "value": item.get("text", "")}
    if kind in {"image", "audio", "file", "sticker"}:
        out = {"type": kind, "value": item.get("key", "")}
        if item.get("meta"):
            out["meta"] = item["meta"]
        return out
    if kind == "unsupported":
        return {
            "type": "unsupported",
            "value": item.get("text", ""),
            "meta": item.get("meta", {}),
        }
    return {"type": "unsupported", "value": str(item)}


def _content_text(content: list[dict], content_text: str | None) -> str:
    if content_text is not None:
        return content_text
    parts: list[str] = []
    for item in content:
        if item.get("kind") == "text":
            parts.append(str(item.get("text", "")))
        elif item.get("type") == "text":
            parts.append(str(item.get("value", "")))
    return "".join(parts)


def _content_json(row: CommonMessage) -> str:
    content = row.content or []
    text = _content_text(content, row.content_text)
    return json.dumps(
        {
            "v": 2,
            "text": text,
            "items": [_content_item_to_v2(item) for item in content],
        },
        ensure_ascii=False,
    )


def _chat_type(scope: str) -> str:
    return "p2p" if scope == "direct" else scope


def _record(row: CommonMessage) -> CommonMessageRecord:
    return CommonMessageRecord(
        message_id=str(row.common_message_id),
        user_id=str(row.common_user_id) if row.common_user_id else None,
        username=row.sender_display_name,
        content=_content_json(row),
        role=row.role,
        root_message_id=str(row.common_root_message_id or row.common_message_id),
        reply_message_id=(
            str(row.common_reply_message_id) if row.common_reply_message_id else None
        ),
        chat_id=str(row.common_conversation_id),
        chat_type=_chat_type(row.scope),
        create_time=int(row.event_time),
        message_type=row.message_type,
        bot_name=row.bot_name,
        response_id=row.response_id,
    )


async def find_cross_chat_messages(
    user_id: str,
    bot_names: list[str],
    exclude_chat_id: str,
    since_ms: int,
    excluded_chat_ids: list[str] | None = None,
) -> list[CommonMessageRecord]:
    user_uuid = _uuid(user_id)
    exclude_chat_uuid = _uuid(exclude_chat_id)
    if user_uuid is None or exclude_chat_uuid is None:
        return []

    stmt = (
        select(CommonMessage)
        .where(CommonMessage.common_conversation_id != exclude_chat_uuid)
        .where(CommonMessage.event_time >= since_ms)
        .where(CommonMessage.bot_name.in_(bot_names))
        .where(
            or_(
                (CommonMessage.role == "user")
                & (CommonMessage.common_user_id == user_uuid),
                CommonMessage.role == "assistant",
            )
        )
        .order_by(CommonMessage.event_time.asc())
    )
    if excluded_chat_ids:
        excluded = _uuid_list(excluded_chat_ids)
        if excluded:
            stmt = stmt.where(~CommonMessage.common_conversation_id.in_(excluded))
    async with auto_tx():
        result = await current_session().execute(stmt)
        return [_record(row) for row in result.scalars().all()]


async def find_message_content(message_id: str) -> str | None:
    msg_uuid = _uuid(message_id)
    if msg_uuid is None:
        return None
    async with auto_tx():
        row = await current_session().scalar(
            select(CommonMessage).where(CommonMessage.common_message_id == msg_uuid)
        )
        return _content_json(row) if row else None


async def find_username(user_id: str) -> str | None:
    user_uuid = _uuid(user_id)
    if user_uuid is None:
        return None
    async with auto_tx():
        result = await current_session().execute(
            select(CommonUser.display_name).where(CommonUser.common_user_id == user_uuid)
        )
        return result.scalar_one_or_none()


async def find_group_download_permission(chat_id: str) -> str | None:
    chat_uuid = _uuid(chat_id)
    if chat_uuid is None:
        return None
    async with auto_tx():
        result = await current_session().execute(
            select(CommonConversation.attachment_policy).where(
                CommonConversation.common_conversation_id == chat_uuid
            )
        )
        policy = result.scalar_one_or_none() or {}
        if policy.get("download_allowed") is True:
            return "all_messages"
        if policy.get("download_allowed") is False:
            return "not_allow"
        return None


async def find_message_by_id(message_id: str) -> CommonMessageRecord | None:
    msg_uuid = _uuid(message_id)
    if msg_uuid is None:
        return None
    async with auto_tx():
        result = await current_session().execute(
            select(CommonMessage).where(CommonMessage.common_message_id == msg_uuid)
        )
        row = result.scalar_one_or_none()
        return _record(row) if row else None


async def find_last_bot_reply_time(chat_id: str) -> int:
    chat_uuid = _uuid(chat_id)
    if chat_uuid is None:
        return 0
    async with auto_tx():
        result = await current_session().execute(
            select(func.max(CommonMessage.event_time)).where(
                CommonMessage.common_conversation_id == chat_uuid,
                CommonMessage.role == "assistant",
            )
        )
        return result.scalar_one_or_none() or 0


async def find_gray_config(message_id: str) -> dict | None:
    msg_uuid = _uuid(message_id)
    if msg_uuid is None:
        return None
    async with auto_tx():
        row = await current_session().scalar(
            select(CommonMessage).where(CommonMessage.common_message_id == msg_uuid)
        )
        if not row:
            return None
        conversation = await current_session().scalar(
            select(CommonConversation).where(
                CommonConversation.common_conversation_id
                == row.common_conversation_id
            )
        )
        policy = conversation.attachment_policy if conversation else None
        gray = (policy or {}).get("gray_config")
        return gray if isinstance(gray, dict) else None


async def find_user_messages_after(
    chat_id: str,
    *,
    after: int,
    limit: int,
    exclude_user_id: str,
) -> list[CommonMessageRecord]:
    chat_uuid = _uuid(chat_id)
    exclude_user_uuid = _uuid(exclude_user_id)
    if chat_uuid is None:
        return []

    stmt = (
        select(CommonMessage)
        .where(
            CommonMessage.common_conversation_id == chat_uuid,
            CommonMessage.role == "user",
            CommonMessage.message_type != "proactive_trigger",
            CommonMessage.event_time > after,
        )
        .order_by(CommonMessage.event_time.desc())
        .limit(limit)
    )
    if exclude_user_uuid is not None:
        stmt = stmt.where(CommonMessage.common_user_id != exclude_user_uuid)

    async with auto_tx():
        result = await current_session().execute(stmt)
        return [_record(row) for row in result.scalars().all()]


async def find_recent_chat_messages(
    *,
    chat_id: str,
    limit: int,
    since: str | None = None,
) -> list[tuple[CommonMessageRecord, str | None]]:
    """按 chat_id 捞这个会话的消息（proactive 渲染的历史上下文）。

    proactive（赤尾主动给真人发消息）**没有源消息**，渲染历史不能走
    ``quick_search``（它从 message_id 反查），只能靠 chat_id 取。这里给一个
    ``chat_id``，捞这个会话的消息：

      * **``since`` 增量水位（治她对着旧话反复主动开口）**：``since`` 非空时只取
        ``event_time`` **严格大于** ``since`` 的消息——即「上一次 life 轮之后真人新发
        的」增量，她这次主动发不再把早就说过的旧话拉进来。``since`` 是 ISO8601 串
        （life 写的 ``LifeState.observed_at`` 形态）、DB ``event_time`` 是毫秒整数，
        过滤前经 ``cst_time.parse`` 把 ISO 折成毫秒时刻再比。``since=None``（默认、也是
        冷启兜底）时**行为完全不变**：退回全量最近 ``limit`` 条。``since`` 解析不出真实
        时刻（脏串）时同样退回全量（不静默把这次主动发的历史吞成空，由水位语义兜底）。
      * **user + assistant 都取**（区别于 ``find_user_messages_after`` 只取 user）——
        proactive context 要把赤尾自己发过的（含上一条 proactive）认作她自己说的，
        所以 assistant 行也得在历史里。
      * assistant 行的发言 persona 经
        ``COALESCE(common_agent_response.persona_id, bot_config.persona_id)`` 取：
          - 普通回复行带 ``response_id`` → join ``common_agent_response.session_id``
            拿到 persona（**优先**，同 ``find_persona_spoken_chats_in_window`` 的 join）。
          - **proactive 出站行 ``response_id=NULL`` 且没有 agent_response 行**（worker
            真实落库口径：proactive session_id=null → responseId 不挂、不写
            agent_response），此 join 必拿 None；改经 ``bot_config(bot_name →
            persona_id)`` 兜底（channel-server 落 proactive 时写了 bot_name）。
            **承重红线（codex 必改 1）**：只靠 response_id 会把 proactive 行判成
            persona=None → proactive context 误判为真人输入（串味）；bot_name → persona
            是它在真实链路里唯一能拿到的归属来源。
        user 行两路都拿 None（无 persona）。``bot_config`` 由 channel-server 管理、不在
        agent-service 的 SQLAlchemy 模型里，用相关标量子查询读裸表（同
        ``resolve_persona_id`` 用裸表名读它），只取 ``is_active`` 的映射。
      * 超 ``limit`` 只保**最近 N 条**、仍按发生先后升序（条目数量控制、不字符截断）：
        SQL 先按 event_time 降序取最近 N 条，再在 Python 反转回升序。``since`` 过滤后
        仍保这个上限（水位后消息很多时取最近 limit 条防爆）。
      * ``proactive_trigger`` 伪消息剔除（NULL-safe，同 ``_by_root``）。

    返回 ``[(record, 发言 persona), ...]``。``chat_id`` 解析不出 uuid → 返回 ``[]``。
    """
    chat_uuid = _uuid(chat_id)
    if chat_uuid is None:
        return []

    stmt = (
        select(
            CommonMessage,
            func.coalesce(
                CommonAgentResponse.persona_id, _bot_config_persona()
            ).label("persona_id"),
        )
        .outerjoin(
            CommonAgentResponse,
            CommonMessage.response_id == CommonAgentResponse.session_id,
        )
        .where(
            CommonMessage.common_conversation_id == chat_uuid,
            or_(
                CommonMessage.message_type.is_(None),
                CommonMessage.message_type != "proactive_trigger",
            ),
        )
        .order_by(CommonMessage.event_time.desc())
        .limit(limit)
    )

    # ``since`` 增量水位：把 ISO8601 串折成毫秒时刻（DB event_time 口径），只取严格大于
    # 它的消息。脏串（cst_time.parse 解析不出真实时刻）退回全量——不加这个过滤即可，
    # 不静默把历史吞成空（向后兼容 + 冷启兜底语义一致）。
    if since is not None:
        since_dt = cst_time.parse(since)
        if since_dt is not None:
            since_ms = int(since_dt.timestamp() * 1000)
            stmt = stmt.where(CommonMessage.event_time > since_ms)

    async with auto_tx():
        result = await current_session().execute(stmt)
        rows = [(_record(msg), msg_persona) for msg, msg_persona in result.all()]
    rows.reverse()
    return rows


async def find_messages_with_user_chat_persona_by_root(
    *,
    root_message_id: str,
    until_create_time: int,
) -> list[tuple[CommonMessageRecord, str | None, str | None, str | None]]:
    root_uuid = _uuid(root_message_id)
    if root_uuid is None:
        return []

    stmt = (
        select(
            CommonMessage,
            CommonConversation.display_name.label("chat_name"),
            func.coalesce(
                CommonAgentResponse.persona_id, _bot_config_persona()
            ).label("persona_id"),
        )
        .outerjoin(
            CommonConversation,
            CommonMessage.common_conversation_id
            == CommonConversation.common_conversation_id,
        )
        .outerjoin(
            CommonAgentResponse,
            CommonMessage.response_id == CommonAgentResponse.session_id,
        )
        .where(CommonMessage.common_root_message_id == root_uuid)
        .where(CommonMessage.event_time <= until_create_time)
        # 历史 proactive_trigger 伪消息（旧外部判断器旁路遗留，已删）剔除：它是
        # 触发器记录、不是真实对话，绝不能混进可见聊天上下文。NULL-safe（正常
        # 消息 message_type 多为 NULL，裸 != 会把 NULL 行一并丢掉）。
        .where(
            or_(
                CommonMessage.message_type.is_(None),
                CommonMessage.message_type != "proactive_trigger",
            )
        )
        .order_by(CommonMessage.event_time.asc())
    )
    async with auto_tx():
        result = await current_session().execute(stmt)
        rows = []
        for msg, chat_name, persona_id in result.all():
            record = _record(msg)
            rows.append((record, record.username, chat_name, persona_id))
        return rows


async def find_messages_with_user_chat_persona_in_chat(
    *,
    chat_id: str,
    exclude_root_message_id: str,
    after_create_time: int,
    before_create_time: int,
    exclude_user_id: str,
    limit: int,
) -> list[tuple[CommonMessageRecord, str | None, str | None, str | None]]:
    chat_uuid = _uuid(chat_id)
    root_uuid = _uuid(exclude_root_message_id)
    exclude_user_uuid = _uuid(exclude_user_id)
    if chat_uuid is None or root_uuid is None:
        return []

    stmt = (
        select(
            CommonMessage,
            CommonConversation.display_name.label("chat_name"),
            func.coalesce(
                CommonAgentResponse.persona_id, _bot_config_persona()
            ).label("persona_id"),
        )
        .outerjoin(
            CommonConversation,
            CommonMessage.common_conversation_id
            == CommonConversation.common_conversation_id,
        )
        .outerjoin(
            CommonAgentResponse,
            CommonMessage.response_id == CommonAgentResponse.session_id,
        )
        .where(
            CommonMessage.common_conversation_id == chat_uuid,
            CommonMessage.common_root_message_id != root_uuid,
            CommonMessage.event_time >= after_create_time,
            CommonMessage.event_time < before_create_time,
            # 历史 proactive_trigger 伪消息剔除（NULL-safe，同 _by_root）。
            or_(
                CommonMessage.message_type.is_(None),
                CommonMessage.message_type != "proactive_trigger",
            ),
        )
        .order_by(CommonMessage.event_time.desc())
        .limit(limit)
    )
    if exclude_user_uuid is not None:
        stmt = stmt.where(CommonMessage.common_user_id != exclude_user_uuid)

    async with auto_tx():
        result = await current_session().execute(stmt)
        rows = []
        for msg, chat_name, persona_id in result.all():
            record = _record(msg)
            rows.append((record, record.username, chat_name, persona_id))
        return rows


async def find_persona_spoken_chats_in_window(
    *,
    persona_id: str,
    since_ms: int,
    until_ms: int,
    per_chat_limit: int,
) -> list[tuple[str, str | None, list[tuple[CommonMessageRecord, str | None]]]]:
    """她在窗口内**发过言**的 chat → 这些 chat 在窗口内的消息（睡前回顾的聊天证据）。

    参与边界合同（spec 决策 2b）：她在 ``[since_ms, until_ms]`` 闭区间内发过言的
    chat 才算她的经历——被动在场没吭声的群不算（chat 是被动唤起模型，她没被唤起
    就没看见）。「她发过言」按 common 口径判：assistant 消息的发言 persona ==
    ``persona_id``，发言 persona 经 ``COALESCE(common_agent_response.persona_id,
    bot_config(bot_name→persona_id))`` 取——**普通回复**经 ``response_id →
    agent_response`` join 拿，**proactive 出站行**（``response_id=NULL``、无
    agent_response）经 ``bot_config`` 兜底拿（承重 2：只认 response join 会把她只发过
    proactive 的 chat 整个漏出回顾）。

    每个够格的 chat 取窗口内消息（user + assistant，剔除 ``proactive_trigger``
    伪消息），**条目数量控制不截断**：超 ``per_chat_limit`` 只保最近 N 条、仍按
    发生先后升序。每条消息带发言 persona（None = 用户消息 / 无归属），发言 persona
    同样经 ``COALESCE(agent_response.persona_id, bot_config 兜底)`` 取（proactive 出站
    行 persona 归属不丢；``_bot_config_persona`` 已加 role 限定，真人 user 行仍是
    None、归属正确），让回顾分得清"她说的"和"别的 bot 说的"；身份字段（user_id /
    username / chat_type）在 ``CommonMessageRecord`` 里。返回按 chat 维度分组：
    ``[(chat_id, chat 显示名, [(record, 发言 persona), ...]), ...]``。
    """
    spoke_stmt = (
        select(CommonMessage.common_conversation_id)
        .outerjoin(
            CommonAgentResponse,
            CommonMessage.response_id == CommonAgentResponse.session_id,
        )
        .where(
            func.coalesce(CommonAgentResponse.persona_id, _bot_config_persona())
            == persona_id,
            CommonMessage.role == "assistant",
            CommonMessage.event_time >= since_ms,
            CommonMessage.event_time <= until_ms,
        )
        .distinct()
    )

    out: list[tuple[str, str | None, list[tuple[CommonMessageRecord, str | None]]]] = []
    async with auto_tx():
        chat_ids = [row[0] for row in (await current_session().execute(spoke_stmt)).all()]
        for chat_uuid in chat_ids:
            name_row = await current_session().execute(
                select(CommonConversation.display_name).where(
                    CommonConversation.common_conversation_id == chat_uuid
                )
            )
            chat_name = name_row.scalar_one_or_none()

            # 窗口内消息按时间**降序取最近 N 条**（条目上限是"保最近"的语义），
            # 再反转回升序——回顾按发生先后读一段对话。
            msg_stmt = (
                select(
                    CommonMessage,
                    func.coalesce(
                        CommonAgentResponse.persona_id, _bot_config_persona()
                    ).label("persona_id"),
                )
                .outerjoin(
                    CommonAgentResponse,
                    CommonMessage.response_id == CommonAgentResponse.session_id,
                )
                .where(
                    CommonMessage.common_conversation_id == chat_uuid,
                    CommonMessage.event_time >= since_ms,
                    CommonMessage.event_time <= until_ms,
                    or_(
                        CommonMessage.message_type.is_(None),
                        CommonMessage.message_type != "proactive_trigger",
                    ),
                )
                .order_by(CommonMessage.event_time.desc())
                .limit(per_chat_limit)
            )
            rows = (await current_session().execute(msg_stmt)).all()
            entries = [(_record(msg), msg_persona) for msg, msg_persona in rows]
            entries.reverse()
            out.append((str(chat_uuid), chat_name, entries))
    return out


async def update_messages_tos_files(
    updates: dict[str, dict[str, str]],
) -> int:
    if not updates:
        return 0

    from app.chat.content_parser import update_tos_files

    updated_count = 0
    async with auto_tx():
        s = current_session()
        for mid, mapping in updates.items():
            msg_uuid = _uuid(mid)
            if msg_uuid is None:
                continue
            row = await s.scalar(
                select(CommonMessage).where(CommonMessage.common_message_id == msg_uuid)
            )
            if row is None:
                continue
            new_content = update_tos_files(_content_json(row), mapping)
            if not new_content:
                continue
            data = json.loads(new_content)
            row.content = data.get("items", [])
            row.content_text = data.get("text")
            await s.execute(
                update(CommonMessage)
                .where(CommonMessage.common_message_id == msg_uuid)
                .values(content=row.content, content_text=row.content_text)
            )
            updated_count += 1
    return updated_count
