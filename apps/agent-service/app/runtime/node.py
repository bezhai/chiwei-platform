"""@node decorator: marks a business async function as a dataflow node.

Reflects on the function's type hints to:
  * validate all inputs are ``Data`` subclasses or ``Stream[Data]``;
  * validate the return is ``Data | Stream[Data] | None``;
  * reject any ``AdminOnly`` Data in the return position;
  * store reflection metadata accessible via ``inputs_of`` / ``output_of``;
  * register the function in ``NODE_REGISTRY``.
"""

from __future__ import annotations

import inspect
from typing import Callable, get_type_hints

from app.runtime.data import Data, is_admin_only
from app.runtime.stream import element_type, is_stream

NODE_REGISTRY: set[Callable] = set()
_NODE_META: dict[Callable, dict] = {}


def node(fn: Callable) -> Callable:
    hints = get_type_hints(fn)
    ret = hints.pop("return", None)
    sig = inspect.signature(fn)
    expected_annotated = {
        name
        for name, p in sig.parameters.items()
        if p.kind
        in (p.POSITIONAL_OR_KEYWORD, p.KEYWORD_ONLY, p.POSITIONAL_ONLY)
        and name != "self"
    }
    missing = expected_annotated - hints.keys()
    if missing:
        raise TypeError(
            f"{fn.__name__} missing type annotations for parameter(s): {sorted(missing)}"
        )
    inputs: dict[str, type] = {}
    for name, t in hints.items():
        if is_stream(t):
            et = element_type(t)
            if not (isinstance(et, type) and issubclass(et, Data)):
                raise TypeError(
                    f"{fn.__name__}.{name}: Stream[X] requires X be a Data subclass"
                )
        elif not (isinstance(t, type) and issubclass(t, Data)):
            raise TypeError(
                f"{fn.__name__}.{name} must be a Data subclass or Stream[Data]"
            )
        inputs[name] = t
    if ret is not None and ret is not type(None):
        tgt = element_type(ret) if is_stream(ret) else ret
        if not (isinstance(tgt, type) and issubclass(tgt, Data)):
            raise TypeError(
                f"{fn.__name__} return must be Data | Stream[Data] | None"
            )
        if is_admin_only(tgt):
            raise TypeError(
                f"{fn.__name__} returns AdminOnly Data {tgt.__name__}: forbidden"
            )
    _NODE_META[fn] = {"inputs": inputs, "output": ret}
    NODE_REGISTRY.add(fn)
    return fn


def inputs_of(fn: Callable) -> dict[str, type]:
    return _NODE_META[fn]["inputs"]


def output_of(fn: Callable):
    return _NODE_META[fn]["output"]
