from typing import Annotated

from app.runtime.data import Data, Key
from app.runtime.stream import Stream, element_type, is_stream


class Chunk(Data):
    sid: Annotated[str, Key]
    seq: Annotated[int, Key]
    text: str
    is_final: bool = False


def test_stream_is_generic_alias():
    anno = Stream[Chunk]
    assert is_stream(anno)
    assert element_type(anno) is Chunk


def test_non_stream_detected():
    assert is_stream(Chunk) is False
    assert is_stream(int) is False


def test_final_marker_default_false():
    c = Chunk(sid="s1", seq=0, text="hi")
    assert c.is_final is False
