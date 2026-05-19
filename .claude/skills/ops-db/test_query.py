"""Tests for ops-db skill query runner db-alias normalization.

Focus: the canonical db string sent to the server must be byte-identical to
what paas-engine registers in its opsDbs/writeDbs maps. Server does a bare
map lookup with no normalization, so client normalization is load-bearing.
"""

import importlib.util
import os
import sys

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "ops_db_query", os.path.join(os.path.dirname(__file__), "query.py")
)
query = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(query)


@pytest.fixture
def captured(monkeypatch):
    """Capture the payload that would be POSTed, skip real HTTP."""
    box = {}

    def fake_curl_post(url, payload, token):
        box["url"] = url
        box["payload"] = payload
        return {"columns": [], "rows": []}

    monkeypatch.setattr(query, "curl_post", fake_curl_post)
    monkeypatch.setattr(query, "get_env", lambda: ("http://fake", "tok"))
    return box


@pytest.mark.parametrize("user_input", ["chiwei-test", "chiwei_test"])
def test_query_normalizes_chiwei_test_to_canonical(captured, user_input):
    query.cmd_query([f"@{user_input}", "SELECT", "1"])
    assert captured["payload"]["db"] == "chiwei_test"


@pytest.mark.parametrize("user_input", ["chiwei-test", "chiwei_test"])
def test_submit_normalizes_chiwei_test_to_canonical(captured, user_input):
    query.cmd_submit([f"@{user_input}", "ALTER", "TABLE", "t", "ADD", "c", "INT;"])
    assert captured["payload"]["db"] == "chiwei_test"


def test_existing_aliases_unchanged(captured):
    query.cmd_query(["@chiwei", "SELECT", "1"])
    assert captured["payload"]["db"] == "chiwei"
    query.cmd_query(["@paas-engine", "SELECT", "1"])
    assert captured["payload"]["db"] == "paas_engine"


def test_unknown_db_still_rejected(captured):
    with pytest.raises(SystemExit):
        query.cmd_query(["@chiwei-prod", "SELECT", "1"])


def test_available_list_includes_chiwei_test(capsys):
    with pytest.raises(SystemExit):
        query.cmd_query(["@nope", "SELECT", "1"])
    err = capsys.readouterr().err
    assert "chiwei_test" in err
