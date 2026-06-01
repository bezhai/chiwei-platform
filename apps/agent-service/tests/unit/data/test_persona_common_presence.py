"""Persona routing must stay on common-layer identity.

Lark mention app_id resolution is owned by channel-server. agent-service only
receives persona_ids and resolves bot presence by common_conversation_id.
"""

from pathlib import Path


def test_persona_queries_do_not_read_lark_credentials_for_mentions():
    src = Path("app/data/queries/persona.py").read_text()
    assert "credentials->>'app_id'" not in src
    assert "resolve_mentioned_personas" not in src
    assert "common_bot_presence" in src
    assert "bot_chat_presence" not in src
