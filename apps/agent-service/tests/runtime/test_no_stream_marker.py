"""Phase 5a 搭车：删 runtime/stream.py + node.py 里的 is_stream 校验。"""
import pytest


def test_runtime_stream_module_does_not_exist():
    with pytest.raises(ModuleNotFoundError):
        import app.runtime.stream  # noqa: F401


def test_node_decorator_does_not_import_is_stream():
    """node.py should no longer reference is_stream."""
    import inspect

    from app.runtime import node as node_mod

    src = inspect.getsource(node_mod)
    assert "is_stream" not in src, (
        "node.py still references is_stream; should be removed in Phase 5a"
    )
    assert "from app.runtime.stream" not in src
