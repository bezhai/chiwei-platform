"""Common message read model for agent-service.

The database source is ``common_message``. Field names here match the
agent-service domain payloads where ``message_id`` means ``common_message_id``
and ``chat_id`` means ``common_conversation_id``.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class CommonMessageRecord:
    message_id: str
    user_id: str | None
    username: str | None
    content: str
    role: str
    root_message_id: str
    reply_message_id: str | None
    chat_id: str
    chat_type: str
    create_time: int
    message_type: str | None = "text"
    bot_name: str | None = None
    response_id: str | None = None


@dataclass(slots=True)
class LifeChatMessage:
    """life 醒来读对话时的一条消息 —— 已经判明「谁说的」的可读形态。

    ``message_id`` = ``common_message_id``，供 life 纯聊天唤醒时派生本轮幂等种子；
    ``is_self`` = 这条是不是赤尾自己说的（role=assistant 且发言 persona == 当前
    persona）；``speaker_display_name`` 是发言者展示名（真人私聊用昵称兜底、不暴露
    raw user_id）；``cst_time`` 是 CST 显示串（``HH:MM CST``），不是裸毫秒。
    """

    message_id: str
    speaker_display_name: str
    is_self: bool
    text: str
    cst_time: str


@dataclass(slots=True)
class ReadableFile:
    """她可见上下文里一个可读的文件项 —— read_book 在她看得见的文件里按名字认（读小说 Task 2）。

    ``attachment_id`` 是这个附件实例的身份（收到该文件那次派生 = common_message_id + file_key，
    见 reading_source.derive_attachment_id），印象按它 key（决策 3）。``file_name`` 原始文件名
    （read_book 按它匹配 + 解码分流）。``tos_file`` 对象存储引用（``files/<file_key>``）——为空
    表示字节还没缓存进对象存储（read_book 据此回问"文件还没准备好"、不开读）。
    """

    attachment_id: str
    file_name: str
    tos_file: str


@dataclass(slots=True)
class LifeChatCounterpart:
    """私聊里对面那个真人是谁 —— 让渲染层能把私聊段具名（主动私聊具名化 Task 1）。

    ``user_id`` = 对方的 ``common_user_id``（str 形态，渲染层内联拼 ``user:<uuid>``
    句柄用）；``display_name`` 是对方展示名（数据层已做兜底，永远非空——全历史都
    没写过 sender_display_name 时落「（不知名）」，同 ``LifeChatMessage`` 的展示名
    口径：不暴露 raw user_id、不把 None 漏给渲染层）。
    """

    user_id: str
    display_name: str


@dataclass(slots=True)
class LifeChatConversation:
    """life 醒来读对话时的一个会话分组 —— 一个会话 + 它最近一段消息 + 可读文件候选。

    ``scope`` = ``"direct"``（私聊）/ ``"group"``（群）；``display_name`` 是群名，
    私聊为 ``None``；``messages`` 按发生先后升序。``file_candidates`` 是这批同一段消息里
    解析出的可读文件项（读小说 Task 2：read_book 在**她这一轮看得见的同一批消息**里认文件，
    零额外查询、真同一边界——不重跑 recent 查询避免边界漂移）。``counterparts`` 是私聊
    对面的真人（主动私聊具名化 Task 1：按会话**全历史**的 role='user' 行解析、与 since
    窗口解耦，正常 p2p 恰好 1 个、脏数据多人如实全列）；群会话恒为空。
    """

    chat_id: str
    scope: str
    display_name: str | None
    messages: list[LifeChatMessage]
    file_candidates: list[ReadableFile] = field(default_factory=list)
    counterparts: list[LifeChatCounterpart] = field(default_factory=list)
