"""Core SQLAlchemy ORM models.

Note: bot_config and common_bot_presence are managed by channel-server.

Tables:
  - common_user, common_conversation, common_message, common_agent_response
  - common_bot_presence (raw SQL, managed by channel-server)
  - model_provider, model_mappings
  - bot_persona
  - akao_schedule
  - reply_style_log

（v4 记忆的 model 已随旧记忆机器整体删除：fragment / abstract_memory /
memory_edge / notes / memory_entity / schedule_revision。它们都是
SQLAlchemy Base（create_all 语义），删 model 不动库表；表的 DROP 走运维。）
"""

from datetime import datetime
from uuid import UUID as PyUUID

from sqlalchemy import (
    JSON,
    UUID,
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Common channel facts
# ---------------------------------------------------------------------------


class CommonUser(Base):
    __tablename__ = "common_user"

    common_user_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class CommonConversation(Base):
    __tablename__ = "common_conversation"

    common_conversation_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    channel: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    display_name: Mapped[str | None] = mapped_column(String(256), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    member_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    attachment_policy: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class CommonMessage(Base):
    __tablename__ = "common_message"

    common_message_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    channel: Mapped[str] = mapped_column(String(64), nullable=False)
    common_conversation_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    common_user_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    sender_display_name: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    content_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    common_root_message_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    common_reply_message_id: Mapped[PyUUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    scope: Mapped[str] = mapped_column(String(16), nullable=False)
    message_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    bot_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    response_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True
    )
    event_time: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )


class CommonAgentResponse(Base):
    __tablename__ = "common_agent_response"

    response_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    trigger_common_message_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    common_conversation_id: Mapped[PyUUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    bot_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    persona_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    response_type: Mapped[str] = mapped_column(
        String(30), nullable=False, default="reply"
    )
    replies: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    agent_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    safety_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending"
    )
    safety_result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# Model provider
# ---------------------------------------------------------------------------


class ModelProvider(Base):
    __tablename__ = "model_provider"

    provider_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    api_key: Mapped[str] = mapped_column(Text)
    base_url: Mapped[str] = mapped_column(Text)
    client_type: Mapped[str] = mapped_column(String(50), default="openai")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    use_proxy: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class ModelMapping(Base):
    __tablename__ = "model_mappings"

    id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    alias: Mapped[str] = mapped_column(String(100), unique=True)
    provider_name: Mapped[str] = mapped_column(String(100))
    real_model_name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# Bot persona
# ---------------------------------------------------------------------------


class BotPersona(Base):
    __tablename__ = "bot_persona"

    persona_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(50), nullable=False)
    persona_core: Mapped[str] = mapped_column(Text, nullable=False)
    persona_lite: Mapped[str] = mapped_column(Text, nullable=False)
    default_reply_style: Mapped[str] = mapped_column(Text, nullable=False)
    error_messages: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    appearance_detail: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=""
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------


class AkaoSchedule(Base):
    """赤尾日程条目 — 支持 monthly / weekly / daily 三层时间维度"""

    __tablename__ = "akao_schedule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    plan_type: Mapped[str] = mapped_column(String(20), nullable=False)
    period_start: Mapped[str] = mapped_column(String(10), nullable=False)
    period_end: Mapped[str] = mapped_column(String(10), nullable=False)
    time_start: Mapped[str | None] = mapped_column(String(5), nullable=True)
    time_end: Mapped[str | None] = mapped_column(String(5), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    mood: Mapped[str | None] = mapped_column(String(50), nullable=True)
    energy_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_style_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    proactive_action: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    target_chats: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    persona_id: Mapped[str] = mapped_column(String(50), nullable=False)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "persona_id",
            "plan_type",
            "period_start",
            "period_end",
            "time_start",
        ),
    )




# ---------------------------------------------------------------------------
# Identity & relationship
# ---------------------------------------------------------------------------


class ReplyStyleLog(Base):
    """Reply Style 审计日志 — append-only"""

    __tablename__ = "reply_style_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    persona_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    style_text: Mapped[str] = mapped_column(Text, nullable=False)
    observation: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
