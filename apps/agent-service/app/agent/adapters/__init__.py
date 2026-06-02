"""Adapter package — importing it registers every ModelClient adapter.

Each adapter module calls ``register_adapter`` at import time (side effect). The
resolution seam (``app.agent.client.build_model_client``) only finds an adapter
if its module has been imported, so importing this package wires up the full
``client_type → adapter`` table. T2 lands OpenAI; T3 adds Gemini.
"""

from __future__ import annotations

from app.agent.adapters import gemini as _gemini  # noqa: F401  (registers adapters)
from app.agent.adapters import openai as _openai  # noqa: F401  (registers adapters)
