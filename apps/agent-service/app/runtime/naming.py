"""Canonical CamelCase -> snake_case conversion for runtime identifiers.

Used for deriving table names (``migrator``), queue / routing-key names
(``durable``) and any other runtime-owned identifier that needs to be
reproducibly computed from a Data class name. Keeping one implementation
keeps those identifiers aligned — e.g. for a Data class ``HTTPRequest``
the table name and durable queue name both reference ``http_request``
instead of drifting to ``h_t_t_p_request`` in one place and
``http_request`` in another.

The algorithm handles common acronym boundaries the way humans read them:

  * ``Ping``           -> ``ping``
  * ``HTTPRequest``    -> ``http_request``
  * ``MyDataV2``       -> ``my_data_v2``
"""

from __future__ import annotations

import re

_CAMEL_TO_SNAKE_1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_TO_SNAKE_2 = re.compile(r"([a-z0-9])([A-Z])")


def to_snake(name: str) -> str:
    """Convert a ``CamelCase`` identifier to ``snake_case``."""
    if not name:
        return name
    s = _CAMEL_TO_SNAKE_1.sub(r"\1_\2", name)
    return _CAMEL_TO_SNAKE_2.sub(r"\1_\2", s).lower()
