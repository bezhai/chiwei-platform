"""chat — conversation pipeline.

Public surface:
    MessageRouter — decides which personas respond to a message
"""

from app.chat.persona_filter import MessageRouter

__all__ = ["MessageRouter"]
