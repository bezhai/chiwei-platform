from sqlalchemy import Boolean, Column, String
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class BotConfig(Base):
    __tablename__ = "bot_config"

    bot_name = Column(String, primary_key=True)
    app_id = Column(String, nullable=False)
    app_secret = Column(String, nullable=False)
    is_active = Column(Boolean, default=True)
