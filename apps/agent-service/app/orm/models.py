from datetime import datetime
from uuid import UUID as PyUUID

from sqlalchemy import (
    UUID,
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class LarkUser(Base):
    __tablename__ = "lark_user"

    union_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    avatar_origin: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_admin: Mapped[bool | None] = mapped_column(Boolean, nullable=True)


class ModelProvider(Base):
    __tablename__ = "model_provider"

    provider_id: Mapped[PyUUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    api_key: Mapped[str] = mapped_column(Text)
    base_url: Mapped[str] = mapped_column(Text)
    # 用于区分底层客户端类型，例如 "openai"、"ark" 等
    client_type: Mapped[str] = mapped_column(String(50), default="openai")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
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


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"

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
    # 向量化状态: pending(待处理) | completed(已完成) | failed(失败)
    vector_status: Mapped[str] = mapped_column(String(20), default="pending")
    # 机器人名称（用于多 bot 场景下载图片等）
    bot_name: Mapped[str | None] = mapped_column(String(50), nullable=True)


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
    """基础聊天信息"""

    __tablename__ = "lark_base_chat_info"

    chat_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    chat_mode: Mapped[str] = mapped_column(String(10))
    permission_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    gray_config: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


class DiaryEntry(Base):
    """赤尾日记"""

    __tablename__ = "diary_entry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(100), nullable=False)
    diary_date: Mapped[str] = mapped_column(String(10), nullable=False)  # "2026-03-10"
    persona_id: Mapped[str] = mapped_column(String(50), nullable=False, default="akao")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("chat_id", "diary_date", "persona_id"),
    )


class AkaoSchedule(Base):
    """赤尾日程条目

    支持三层时间维度的计划：
    - monthly: 月度方向（兴趣倾向、生活基调）
    - weekly: 周计划（本周大致安排）
    - daily: 日计划（逐时段活动、心情、精力）

    月/周计划由 LLM 离线生成，给出方向而非限制。
    日计划继承月/周计划，填充具体时段。
    event 类型覆盖同时段的 routine。
    """

    __tablename__ = "akao_schedule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 计划层级: "monthly" | "weekly" | "daily"
    plan_type: Mapped[str] = mapped_column(String(20), nullable=False)

    # 时间周期
    period_start: Mapped[str] = mapped_column(String(10), nullable=False)  # "2026-03-01"
    period_end: Mapped[str] = mapped_column(String(10), nullable=False)  # "2026-03-31"

    # 日计划专用：当天内的时间段
    time_start: Mapped[str | None] = mapped_column(String(5), nullable=True)  # "07:00"
    time_end: Mapped[str | None] = mapped_column(String(5), nullable=True)  # "08:30"

    # 内容：叙事性描述（所有层级都有）
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # 结构化元数据（主要用于日计划时段）
    mood: Mapped[str | None] = mapped_column(String(50), nullable=True)
    energy_level: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 1-5
    response_style_hint: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Phase 2: 主动行为配置
    proactive_action: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    target_chats: Mapped[list | None] = mapped_column(JSONB, nullable=True)

    # 生成元信息
    persona_id: Mapped[str] = mapped_column(String(50), nullable=False, default="akao")

    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("persona_id", "plan_type", "period_start", "period_end", "time_start"),
    )


class LarkGroupMember(Base):
    """群成员信息"""

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


class WeeklyReview(Base):
    """赤尾周记"""

    __tablename__ = "weekly_review"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(100), nullable=False)
    week_start: Mapped[str] = mapped_column(String(10), nullable=False)  # "2026-03-10" (周一)
    week_end: Mapped[str] = mapped_column(String(10), nullable=False)  # "2026-03-16" (周日)
    persona_id: Mapped[str] = mapped_column(String(50), nullable=False, default="akao")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("chat_id", "week_start", "persona_id"),)


class PersonImpression(Base):
    """Bot 对群友的人物印象"""

    __tablename__ = "person_impression"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(100), nullable=False)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    persona_id: Mapped[str] = mapped_column(String(50), nullable=False, default="akao")
    impression_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("chat_id", "user_id", "persona_id"),)


class GroupCultureGestalt(Base):
    """Bot 对一个群的整体感觉，一句话"""

    __tablename__ = "group_culture_gestalt"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(100), nullable=False)
    persona_id: Mapped[str] = mapped_column(String(50), nullable=False, default="akao")
    gestalt_text: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("chat_id", "persona_id"),)


class BotPersona(Base):
    """Bot 人设配置 — 每个 persona bot 的人设数据"""

    __tablename__ = "bot_persona"

    persona_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(50), nullable=False)
    persona_core: Mapped[str] = mapped_column(Text, nullable=False)
    persona_lite: Mapped[str] = mapped_column(Text, nullable=False)
    default_reply_style: Mapped[str] = mapped_column(Text, nullable=False)
    error_messages: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AkaoJournal(Base):
    """赤尾个人日志 — 跨群合成的一天感受

    从当天所有 DiaryEntry 模糊化合成，保留情感和氛围，隐去具体话题。
    daily: 每天一篇
    weekly: 每周一篇（从 7 篇 daily 合成）
    """

    __tablename__ = "akao_journal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    journal_type: Mapped[str] = mapped_column(String(10), nullable=False)  # "daily" | "weekly"
    journal_date: Mapped[str] = mapped_column(String(10), nullable=False)  # "2026-03-26" or week monday
    period_end: Mapped[str] = mapped_column(String(10), nullable=False)  # daily 同 journal_date, weekly 为周日
    persona_id: Mapped[str] = mapped_column(String(50), nullable=False, default="akao")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source_chat_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("persona_id", "journal_type", "journal_date"),
    )
