"""Data queries — split per domain. 调用方 import 不变 (`from app.data.queries import X`).

8 个 domain 文件：
  - model_provider / persona / messages / agent_response / life
  - memory（fragments + abstracts CRUD）/ memory_edges / memory_search

Each domain file owns ``__all__``; this package aggregates via ``from X import *``.
``test_queries_split.py`` 验证 __all__ 完整 + 无重名（``from-import-*`` 重名后者
覆盖不报错，必须有测试兜底）。
"""
from app.data.queries.agent_response import *  # noqa: F401,F403

# Aggregate __all__ for downstream introspection (test_queries_split asserts on this).
from app.data.queries.agent_response import __all__ as _ar
from app.data.queries.life import *  # noqa: F401,F403
from app.data.queries.life import __all__ as _life
from app.data.queries.memory import *  # noqa: F401,F403
from app.data.queries.memory import __all__ as _memory
from app.data.queries.memory_edges import *  # noqa: F401,F403
from app.data.queries.memory_edges import __all__ as _memory_edges
from app.data.queries.memory_search import *  # noqa: F401,F403
from app.data.queries.memory_search import __all__ as _memory_search
from app.data.queries.messages import *  # noqa: F401,F403
from app.data.queries.messages import __all__ as _messages
from app.data.queries.model_provider import *  # noqa: F401,F403
from app.data.queries.model_provider import __all__ as _mp
from app.data.queries.persona import *  # noqa: F401,F403
from app.data.queries.persona import __all__ as _persona

__all__ = [
    *_ar,
    *_life,
    *_memory,
    *_memory_edges,
    *_memory_search,
    *_messages,
    *_mp,
    *_persona,
]
