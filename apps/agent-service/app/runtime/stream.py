from typing import Generic, TypeVar, get_args, get_origin

T = TypeVar("T")


class Stream(Generic[T]):
    """Type-only marker for 'stream of T'. Never instantiated directly;
    runtime sees a @node returning Stream[X] and wires it as async iterable of X."""


def is_stream(annotation) -> bool:
    return get_origin(annotation) is Stream


def element_type(annotation):
    args = get_args(annotation)
    return args[0] if args else None
