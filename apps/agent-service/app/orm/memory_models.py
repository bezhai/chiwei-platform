"""记忆系统 v3 数据模型

experience_fragment: 唯一的记忆存储，所有粒度在同一张表
memory_entity: 飞书长 ID → 短自增 ID 映射，用于碎片内容消歧
"""

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.orm.models import Base


class ExperienceFragment(Base):
    """经历碎片 — 赤尾记忆的唯一存储单元"""

    __tablename__ = "experience_fragment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    persona_id: Mapped[str] = mapped_column(String(50), nullable=False)

    # 粒度: conversation / glimpse / daily / weekly
    grain: Mapped[str] = mapped_column(String(20), nullable=False)

    # 来源（conversation/glimpse 有值，daily/weekly 为 NULL）
    source_chat_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_type: Mapped[str | None] = mapped_column(String(10), nullable=True)  # p2p / group

    # 原始消息的时间范围（毫秒时间戳）
    time_start: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    time_end: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    # 核心：赤尾的主观经历叙事
    content: Mapped[str] = mapped_column(Text, nullable=False)

    # 涉及的人/群（entity ID 列表，用于按人/群检索）
    mentioned_entity_ids: Mapped[list] = mapped_column(JSONB, default=list)

    # 元信息
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class LifeEngineState(Base):
    """Life Engine 状态 — 每次 tick INSERT 一行，保留完整历史"""

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
    """Glimpse 观察状态 — append-only，每次观察 INSERT 一行"""

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
    """ID 映射 — 飞书长 ID → 短自增 ID，用于碎片内容中消歧

    碎片内容用 `名字(#id)` 格式引用：
    "今天和阿儒(#3)在番剧群(#7)聊了新番"
    """

    __tablename__ = "memory_entity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(10), nullable=False)  # user / group / p2p
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)

    __table_args__ = (UniqueConstraint("entity_type", "external_id"),)


class ReplyStyleLog(Base):
    """Reply Style 审计日志 — 每次漂移 INSERT 一行，append-only"""

    __tablename__ = "reply_style_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    persona_id: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    style_text: Mapped[str] = mapped_column(Text, nullable=False)
    observation: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # 'base' / 'drift' / 'manual'
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RelationshipMemoryV2(Base):
    """关系记忆 v2 — 两阶段管线产出，去掉冗余 user_name/memory_text"""

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
        Index("idx_rel_mem_v2_persona_user_created", "persona_id", "user_id", created_at.desc()),
    )


