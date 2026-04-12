"""Core SQLAlchemy ORM models.

Note: bot_config, bot_chat_presence, agent_responses 等表由 lark-server 管理，
此处未定义 ORM model，queries.py 中通过 raw SQL 访问。

Tables:
  - lark_user, lark_group_chat_info, lark_base_chat_info, lark_group_member
  - model_provider, model_mappings
  - conversation_messages
  - bot_persona
  - akao_schedule
  - experience_fragment, life_engine_state, glimpse_state
  - memory_entity, reply_style_log, relationship_memory_v2
"""

from datetime import datetime
from uuid import UUID as PyUUID

from sqlalchemy import (
    JSON,
    UUID,
    BigInteger,
    Boolean,
    DateTime,
    Index,
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
# Lark / Feishu
# ---------------------------------------------------------------------------


class LarkUser(Base):
    __tablename__ = "lark_user"

    id: Mapped[int] = mapped_column(BigInteger, autoincrement=True, unique=True)
    union_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    avatar_origin: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_admin: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class LarkGroupChatInfo(Base):
    __tablename__ = "lark_group_chat_info"

    chat_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String)
    avatar: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_count: Mapped[int] = mapped_column(BigInteger)
    chat_status: Mapped[str] = mapped_column(String(20))
    is_leave: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    download_has_permission_setting: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )


class LarkBaseChatInfo(Base):
    __tablename__ = "lark_base_chat_info"

    chat_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    chat_mode: Mapped[str] = mapped_column(String(10))
    permission_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    gray_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class LarkGroupMember(Base):
    __tablename__ = "lark_group_member"

    chat_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    union_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    is_manager: Mapped[bool] = mapped_column(Boolean, default=False)
    is_leave: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
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
# Conversation messages
# ---------------------------------------------------------------------------


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

    id: Mapped[int] = mapped_column(BigInteger, autoincrement=True, unique=True)
    message_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(100))
    content: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(20))
    root_message_id: Mapped[str] = mapped_column(String(100))
    reply_message_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    chat_id: Mapped[str] = mapped_column(String(100))
    chat_type: Mapped[str] = mapped_column(String(10))
    create_time: Mapped[int] = mapped_column(BigInteger)
    message_type: Mapped[str | None] = mapped_column(
        String(30), nullable=True, default="text"
    )
    vector_status: Mapped[str] = mapped_column(String(20), default="pending")
    bot_name: Mapped[str | None] = mapped_column(String(50), nullable=True)
    response_id: Mapped[str | None] = mapped_column(String(100), nullable=True)


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


class ExperienceFragment(Base):
    """经历碎片 — 赤尾记忆的唯一存储单元"""

    __tablename__ = "experience_fragment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_id: Mapped[str] = mapped_column(String(50), nullable=False)
    grain: Mapped[str] = mapped_column(String(20), nullable=False)
    source_chat_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_type: Mapped[str | None] = mapped_column(String(10), nullable=True)
    time_start: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    time_end: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    mentioned_entity_ids: Mapped[list] = mapped_column(JSONB, default=list)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LifeEngineState(Base):
    """Life Engine 状态 — append-only"""

    __tablename__ = "life_engine_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_id: Mapped[str] = mapped_column(String(50), nullable=False)
    current_state: Mapped[str] = mapped_column(Text, nullable=False)
    activity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    response_mood: Mapped[str] = mapped_column(Text, nullable=False)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    skip_until: Mapped[datetime | None] = mapped_column(
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


class RelationshipMemoryV2(Base):
    """关系记忆 v2 — 两阶段管线产出, append-only"""

    __tablename__ = "relationship_memory_v2"

    id: Mapped[int] = mapped_column(primary_key=True)
    persona_id: Mapped[str] = mapped_column(String(50), nullable=False)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    core_facts: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    impression: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        Index(
            "idx_rel_mem_v2_persona_user_created",
            "persona_id",
            "user_id",
            created_at.desc(),
        ),
    )
