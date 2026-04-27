from typing import Annotated

import pytest

from app.runtime.data import AdminOnly, Data, Key
from app.runtime.node import NODE_REGISTRY, inputs_of, node, output_of


class Msg(Data):
    mid: Annotated[str, Key]
    text: str


class Frag(Data):
    fid: Annotated[str, Key]
    vec: list[float]


class Cfg(Data, AdminOnly):
    cid: Annotated[str, Key]
    v: dict


@node
async def vectorize(msg: Msg) -> Frag:
    return Frag(fid="f1", vec=[0.0])


def test_registered():
    assert vectorize in NODE_REGISTRY


def test_inputs_reflection():
    assert inputs_of(vectorize) == {"msg": Msg}


def test_output_reflection():
    assert output_of(vectorize) is Frag


def test_admin_only_output_rejected():
    with pytest.raises(TypeError, match="AdminOnly"):

        @node
        async def bad() -> Cfg:
            return Cfg(cid="c1", v={})


def test_non_data_input_rejected():
    with pytest.raises(TypeError, match="must be a Data subclass"):

        @node
        async def bad2(x: int) -> Frag: ...


def test_missing_annotation_rejected():
    with pytest.raises(TypeError, match="missing type annotations"):

        @node
        async def bad3(msg, other: Msg) -> Frag: ...


def test_returns_none_allowed():
    @node
    async def sink_node(msg: Msg) -> None: ...

    assert sink_node in NODE_REGISTRY
    assert output_of(sink_node) is type(None)


def test_returns_optional_data_allowed():
    @node
    async def maybe_emit(msg: Msg) -> Frag | None:
        return None

    assert maybe_emit in NODE_REGISTRY
    # Optional is unwrapped to the inner Data type for downstream wiring.
    assert output_of(maybe_emit) is Frag


def test_returns_optional_admin_only_rejected():
    with pytest.raises(TypeError, match="AdminOnly"):

        @node
        async def bad_optional(msg: Msg) -> Cfg | None: ...


def test_sync_function_rejected_at_decorate_time():
    """A plain ``def`` (not ``async def``) must fail at @node decoration,
    not later inside ``await fn(...)``. Surface the error where the source
    code is.
    """
    with pytest.raises(TypeError, match="async def"):

        @node
        def sync_node(msg: Msg) -> Frag:  # type: ignore[misc]
            return Frag(fid="f1", vec=[0.0])


def test_stream_input_rejected():
    """Stream[X] is a type marker but the runtime has no async-iteration
    dispatch, so a @node accepting Stream[X] would never run. Reject it
    at decorate time.
    """
    from app.runtime.stream import Stream

    with pytest.raises(TypeError, match="Stream"):

        @node
        async def bad_stream(chunks: Stream[Msg]) -> None: ...


def test_stream_return_rejected():
    from app.runtime.stream import Stream

    with pytest.raises(TypeError, match="Stream"):

        @node
        async def bad_stream_ret(msg: Msg) -> Stream[Frag]: ...  # type: ignore[misc]
