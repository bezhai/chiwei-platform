from typing import Generic, TypeVar, get_args, get_origin

T = TypeVar("T")


class Stream(Generic[T]):
    """Internal type marker for 'stream of T'. Never instantiated.

    Not part of the public ``app.runtime`` surface today: the ``@node``
    decorator rejects any ``Stream[X]`` parameter or return at decorate
    time because the runtime wrapper has no async-iteration dispatch
    (it only auto-emits a single ``Data`` instance). The marker stays
    here so the decorator's check has something to match on; when
    async-iteration support lands, both this module and the public API
    will be re-promoted together.
    """


def is_stream(annotation) -> bool:
    return get_origin(annotation) is Stream


def element_type(annotation):
    args = get_args(annotation)
    return args[0] if args else None
