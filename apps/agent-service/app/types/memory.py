"""
智能记忆管理相关数据类型
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class MessageType(str, Enum):
    """消息类型枚举"""

    QUESTION = "question"
    ANSWER = "answer"
    DISCUSSION = "discussion"
    NOTIFICATION = "notification"
    CASUAL = "casual"


class ReplyInfo(BaseModel):
    """回复信息"""

    reply_to_message_id: str
    reply_to_user_id: str
    reply_type: str  # "quote", "mention", "thread"


class MessageFeatures(BaseModel):
    """消息特征"""

    message_id: str
    user_id: str
    chat_id: str
    content: str
    timestamp: int
    reply_to: ReplyInfo | None = None
    mentions: list[str] = []
    message_type: MessageType = MessageType.CASUAL
    keywords: list[str] = []

    # 特征评分
    has_question: bool = False
    has_at_mention: bool = False
    is_continuous_from_same_user: bool = False


class RelevanceScore(BaseModel):
    """相关性评分详情"""

    total_score: float
    reply_chain_score: float
    user_continuity_score: float
    time_decay_score: float
    mention_relation_score: float
    keyword_overlap_score: float


class ContextUsageRecord(BaseModel):
    """上下文使用记录"""

    context_id: str
    chat_id: str
    user_id: str
    trigger_message_id: str
    context_message_ids: list[str]
    context_message_count: int
    relevance_scores: dict[str, float] | None = None
    processing_time_ms: int | None = None
    fallback_used: bool = False
    created_at: datetime = datetime.now()


class MemoryMetrics(BaseModel):
    """记忆管理指标"""

    avg_context_build_time_ms: float
    avg_relevance_score: float
    avg_context_message_count: float
    fallback_rate: float
    total_contexts_built: int
    unique_chats_served: int


class ChatRecord(BaseModel):
    """聊天记录模型"""

    user_name: str = Field(..., description="用户名")
    content: str = Field(..., description="聊天内容")
    create_time: str = Field(..., description="创建时间")
    message_id: str = Field(..., description="消息ID")
    reply_message_id: str | None = Field(None, description="回复消息ID")


class HistoryMessagesResponse(BaseModel):
    """历史消息列表响应模型"""

    messages: list[ChatRecord] = Field(..., description="按顺序排列的消息内容列表")
    total_count: int = Field(..., description="消息总数")
