"""Unit tests for the shared ``to_snake`` identifier helper.

These cases lock in the algorithm's behavior so migrator-generated table
names and durable-edge queue names never drift apart again.
"""

from __future__ import annotations

from app.runtime.naming import to_snake


def test_single_word():
    assert to_snake("Ping") == "ping"


def test_one_word_message():
    assert to_snake("Message") == "message"


def test_acronym_prefix():
    # The human-readable convention: ``HTTPRequest`` -> ``http_request``,
    # NOT ``h_t_t_p_request``. This is why migrator and durable now share
    # the same algorithm.
    assert to_snake("HTTPRequest") == "http_request"


def test_trailing_version_suffix():
    # Locks in whatever the regex naturally does with a digit-bearing
    # suffix like ``V2`` — documented here so future changes don't
    # silently break table/queue names.
    assert to_snake("MyDataV2") == "my_data_v2"


def test_empty_string():
    assert to_snake("") == ""
