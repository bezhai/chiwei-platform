"""chat — conversation pipeline.

Public surface:
    stream_chat  — main entry point for streaming chat responses
    MessageRouter — decides which personas respond to a message
"""

from app.chat.pipeline import stream_chat
from app.chat.router import MessageRouter

__all__ = ["stream_chat", "MessageRouter"]
