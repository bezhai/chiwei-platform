"""Public API of the dataflow runtime.

Business code that writes Data classes, @node functions or wire()
declarations should import from `app.runtime` only. The submodules (data,
node, wire, sink, emit, …) are internal implementation; the names
re-exported here are the stable surface promised by
`docs/guides/dataflow-framework.md`.

Internals (compile_graph, registries, durable plumbing, migrator)
intentionally stay submodule-only — they are not needed to write a node and
may change without notice. Starting an app is the plugin host's
(:mod:`app.host`).
"""

from app.runtime.data import AdminOnly, Data, DedupKey, Key, Version
from app.runtime.emit import emit
from app.runtime.node import node
from app.runtime.sink import Sink
from app.runtime.wire import wire

__all__ = [
    "AdminOnly",
    "Data",
    "DedupKey",
    "Key",
    "Version",
    "Sink",
    "emit",
    "node",
    "wire",
]
