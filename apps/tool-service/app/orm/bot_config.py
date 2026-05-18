from sqlalchemy import JSON, Boolean, Column, String
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class BotConfig(Base):
    __tablename__ = "bot_config"

    # bot_config 多 channel 化后，飞书凭据不再是 app_id/app_secret 裸列，而是
    # 统一存进 credentials JSONB（各 channel 自己的形状由 adapter 解释）。
    # 旧裸列已删，tool-service 只认 channel='lark' 并从 credentials 取凭据。
    bot_name = Column(String, primary_key=True)
    channel = Column(String, nullable=False, default="lark")
    credentials = Column(JSON, nullable=True)
    is_active = Column(Boolean, default=True)
