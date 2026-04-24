"""@node decorator: marks a business async function as a dataflow node.

Reflects on the function's type hints to:
  * validate all inputs are ``Data`` subclasses or ``Stream[Data]``;
  * validate the return is ``Data``, ``Data | None``, ``Stream[Data]`` or ``None``;
  * reject any ``AdminOnly`` Data in the return position;
  * store reflection metadata accessible via ``inputs_of`` / ``output_of``;
  * register the function in ``NODE_REGISTRY``.

Behavior: the decorator wraps ``fn`` so that a returned ``Data`` is
automatically emitted into the graph via ``runtime.emit.emit`` — spec
forbids business code from calling ``emit`` / ``mq.publish`` to the next
hop manually. ``None`` returns are skipped. The wrapper still returns
the value to its caller so unit tests can assert on it directly.
"""

from __future__ import annotations

import functools
import inspect
from types import UnionType
from typing import Callable, Union, get_args, get_origin, get_type_hints

from app.runtime.data import Data, is_admin_only
from app.runtime.stream import element_type, is_stream

NODE_REGISTRY: set[Callable] = set()
_NODE_META: dict[Callable, dict] = {}


def _unwrap_optional(annotation):
    """If ``annotation`` is ``T | None`` / ``Optional[T]``, return ``T``.

    Returns the annotation unchanged when it isn't a two-arm union that
    contains ``None``. Used so ``@node`` can accept ``Data | None`` return
    types: the node emits ``None`` to skip, runtime drops it before edges.
    """
    origin = get_origin(annotation)
    if origin is Union or origin is UnionType:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1 and len(get_args(annotation)) == 2:
            return args[0]
    return annotation


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
        # ``Data | None`` returns are allowed — the @node may emit None to
        # skip emission. Validation + metadata use the inner type.
        unwrapped = _unwrap_optional(ret)
        tgt = element_type(unwrapped) if is_stream(unwrapped) else unwrapped
        if not (isinstance(tgt, type) and issubclass(tgt, Data)):
            raise TypeError(
                f"{fn.__name__} return must be Data | Stream[Data] | None"
            )
        if is_admin_only(tgt):
            raise TypeError(
                f"{fn.__name__} returns AdminOnly Data {tgt.__name__}: forbidden"
            )
        ret = unwrapped
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        result = await fn(*args, **kwargs)
        if isinstance(result, Data):
            from app.runtime.emit import emit as _emit

            await _emit(result)
        return result

    _NODE_META[wrapper] = {"inputs": inputs, "output": ret}
    NODE_REGISTRY.add(wrapper)
    return wrapper


def inputs_of(fn: Callable) -> dict[str, type]:
    return _NODE_META[fn]["inputs"]


def output_of(fn: Callable):
    return _NODE_META[fn]["output"]
