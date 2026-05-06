"""chat — conversation pipeline.

Public surface:
    MessageRouter — decides which personas respond to a message
"""

from app.chat.router import MessageRouter

__all__ = ["MessageRouter"]
