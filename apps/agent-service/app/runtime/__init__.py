"""Public API of the dataflow runtime.

Business code that writes Data classes, @node functions, wire() declarations,
or deployment bind() rules should import from `app.runtime` only. The
submodules (data, node, wire, source, sink, emit, placement, query, stream, …)
are internal implementation; the names re-exported here are the stable
surface promised by `docs/guides/dataflow-framework.md`.

Engine internals (compile_graph, registries, Runtime, durable plumbing,
migrator, http_source) intentionally stay submodule-only — they are not
needed to write a node and may change without notice.
"""

from app.runtime.data import AdminOnly, Data, DedupKey, Key, Version
from app.runtime.emit import emit
from app.runtime.node import node
from app.runtime.placement import bind
from app.runtime.query import query
from app.runtime.sink import Sink
from app.runtime.source import Source
from app.runtime.stream import Stream
from app.runtime.wire import wire

__all__ = [
    "AdminOnly",
    "Data",
    "DedupKey",
    "Key",
    "Version",
    "Sink",
    "Source",
    "Stream",
    "bind",
    "emit",
    "node",
    "query",
    "wire",
]
