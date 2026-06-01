"""Core SQLAlchemy ORM models.

Note: bot_config and bot_chat_presence are managed by channel-server.

Tables:
  - common_user, common_conversation, common_message, common_agent_response
  - model_provider, model_mappings
  - bot_persona
  - akao_schedule
  - life_engine_state, glimpse_state
  - memory_entity, reply_style_log
  # Memory v4
  - fragment, abstract_memory, memory_edge, notes, schedule_revision
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
# Memory — experience fragments, glimpse, life engine
# ---------------------------------------------------------------------------


class LifeEngineState(Base):
    """Life Engine 状态 — append-only"""

    __tablename__ = "life_engine_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_id: Mapped[str] = mapped_column(String(50), nullable=False)
    current_state: Mapped[str] = mapped_column(Text, nullable=False)
    activity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    response_mood: Mapped[str] = mapped_column(Text, nullable=False)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    skip_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    state_end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class GlimpseState(Base):
    """Glimpse 观察状态 — append-only"""

    __tablename__ = "glimpse_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_id: Mapped[str] = mapped_column(String(50), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(100), nullable=False)
    last_seen_msg_time: Mapped[int] = mapped_column(BigInteger, nullable=False)
    observation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MemoryEntity(Base):
    """飞书长 ID -> 短自增 ID 映射，用于碎片内容中消歧"""

    __tablename__ = "memory_entity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(10), nullable=False)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    __table_args__ = (UniqueConstraint("entity_type", "external_id"),)


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


# ---------------------------------------------------------------------------
# Memory v4
# ---------------------------------------------------------------------------


class Fragment(Base):
    """事实碎片 — v4 短期/长期记忆中的原子事实。"""

    __tablename__ = "fragment"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    persona_id: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    chat_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    clarity: Mapped[str] = mapped_column(Text, nullable=False, server_default="clear")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_touched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class AbstractMemory(Base):
    """抽象记忆 — v4 subject + content 模型（不分类型）。"""

    __tablename__ = "abstract_memory"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    persona_id: Mapped[str] = mapped_column(Text, nullable=False)
    subject: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    clarity: Mapped[str] = mapped_column(Text, nullable=False, server_default="clear")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_touched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class MemoryEdge(Base):
    """统一边表 — 连接 fragment / abstract_memory 节点。"""

    __tablename__ = "memory_edge"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    persona_id: Mapped[str] = mapped_column(Text, nullable=False)
    from_id: Mapped[str] = mapped_column(Text, nullable=False)
    from_type: Mapped[str] = mapped_column(Text, nullable=False)
    to_id: Mapped[str] = mapped_column(Text, nullable=False)
    to_type: Mapped[str] = mapped_column(Text, nullable=False)
    edge_type: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Note(Base):
    """赤尾主动清单 — 她自己决定记下来的事。"""

    __tablename__ = "notes"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    persona_id: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    when_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    delete_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class ScheduleRevision(Base):
    """today_schedule 的 append-only 历史版本。"""

    __tablename__ = "schedule_revision"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    persona_id: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
