from pydantic import BaseModel


class ChatMessage(BaseModel):
    """
    聊天消息
    Chat message request
    """

    user_id: str  # 用户id / User ID
    user_open_id: str | None = (
        None  # 用户open_id, 仅当用户为真人时存在 / User open ID, only exists when user is real person
    )
    user_name: str  # 用户名 / User name
    content: str  # 转义成markdown的消息内容，包括图片等 / Markdown content (may include images)
    is_mention_bot: bool  # 是否@机器人 / Mention bot
    role: str  # 角色: 'user' | 'assistant' / Role
    root_message_id: str | None = None  # 根消息id / Root message ID
    reply_message_id: str | None = None  # 回复消息的id / Reply message ID
    message_id: str  # 消息id / Message ID
    chat_id: str  # 聊天id / Chat ID
    chat_type: str  # 聊天类型: 'p2p' | 'group' / Chat type
    create_time: str  # 创建时间 / Creation time


class ChatSimpleMessage(BaseModel):
    """
    聊天简单消息
    Chat simple message
    """

    user_name: str  # 用户名 / User name
    content: str  # 转义成markdown的消息内容，包括图片等 / Markdown content (may include images)
    role: str  # 角色: 'user' | 'assistant' | 'system' / Role


class ChatRequest(BaseModel):
    """
    聊天请求
    Chat request
    """

    message_id: str  # 消息id / Message ID
    session_id: str | None = None  # 会话追踪 ID / Session tracking ID
    is_canary: bool | None = False  # 是否开启灰度
