from datetime import datetime
from uuid import UUID as PyUUID

from sqlalchemy import (
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
    content: Mapped[str] = mapped_column(Text, nullable=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("chat_id", "diary_date"),
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
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        # 月/周计划：同类型同周期只有一条
        UniqueConstraint("plan_type", "period_start", "period_end", "time_start"),
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
    content: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("chat_id", "week_start"),)


class PersonImpression(Base):
    """赤尾对群友的人物印象"""

    __tablename__ = "person_impression"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(100), nullable=False)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    impression_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("chat_id", "user_id"),)


class ChatImpression(Base):
    """赤尾对群/聊天的氛围印象

    记录她对每个群的整体感觉：氛围、节奏、她在其中的位置。
    不记录具体话题（防止反馈循环），只记感觉和性格。
    例如："这个群很放飞，大家喜欢互相逗"、"小群，聊天节奏慢，比较温柔"
    """

    __tablename__ = "chat_impression"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    impression_text: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AkaoJournal(Base):
    """赤尾个人日志 — 赤尾级，不绑 chat_id

    与 DiaryEntry（按群/私聊分开的素材）不同，
    个人日志是赤尾作为一个人的统一记忆沉淀。

    daily: 融合当天所有 DiaryEntry + Schedule，模糊化具体话题
    weekly: 合成 7 篇 daily 日志的沉淀

    日志不直接注入聊天上下文。它的作用是：
    1. 喂给下一天的 Schedule 生成（经历→计划的循环）
    2. 作为赤尾内在经历的存档
    """

    __tablename__ = "akao_journal"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # "daily" | "weekly"
    journal_type: Mapped[str] = mapped_column(String(10), nullable=False)
    # daily: "2026-03-17", weekly: week_start "2026-03-10"
    journal_date: Mapped[str] = mapped_column(String(10), nullable=False)
    # daily: same as journal_date, weekly: week_end
    period_end: Mapped[str] = mapped_column(String(10), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # 合成时用了几个聊天的素材
    source_chat_count: Mapped[int] = mapped_column(Integer, default=0)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("journal_type", "journal_date"),
    )
