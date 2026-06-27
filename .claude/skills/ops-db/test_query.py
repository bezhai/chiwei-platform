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


def test_curl_post_non_2xx_surfaces_server_error(monkeypatch, capsys):
    def fake_run(cmd, capture_output, text):
        return query.subprocess.CompletedProcess(
            cmd,
            0,
            stdout='{"error":"pq: relation \\"missing\\" does not exist"}\n500',
            stderr="",
        )

    monkeypatch.setattr(query.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc:
        query.curl_post("http://fake", {"sql": "SELECT * FROM missing"}, "tok")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "HTTP 500" in err
    assert 'pq: relation "missing" does not exist' in err


def test_curl_get_non_2xx_surfaces_server_error(monkeypatch, capsys):
    def fake_run(cmd, capture_output, text):
        return query.subprocess.CompletedProcess(
            cmd,
            0,
            stdout='{"error":"mutation not found"}\n404',
            stderr="",
        )

    monkeypatch.setattr(query.subprocess, "run", fake_run)

    with pytest.raises(SystemExit) as exc:
        query.curl_get("http://fake/mutations/999", "tok")

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "HTTP 404" in err
    assert "mutation not found" in err


def test_available_list_includes_chiwei_test(capsys):
    with pytest.raises(SystemExit):
        query.cmd_query(["@nope", "SELECT", "1"])
    err = capsys.readouterr().err
    assert "chiwei_test" in err


# --- --file / --reason input path (shell-free SQL submission) ---

# Deliberately hostile payload: PL/pgSQL $$ dollar-quoting, shell $var,
# single + double quotes, %s, multi-line newlines, a -- comment, and a
# literal "-- reason: xxx" substring that MUST NOT be stripped as a reason.
HOSTILE_SQL = (
    "DO $$\n"
    "DECLARE v_id INT;\n"
    "BEGIN\n"
    "  INSERT INTO bot_persona (name, persona_core)\n"
    "  VALUES ('赤尾', 'she said \"hi\" -- reason: not a real reason\\n100% $HOME');\n"
    "  -- a trailing sql comment with $var and 'quotes'\n"
    "END $$;\n"
)


def test_submit_file_sends_bytes_verbatim(captured, tmp_path):
    """SQL read via --file must reach HTTP layer byte-identical: $$ preserved,
    no -- reason: stripping, reason taken from --reason not from SQL."""
    sql_file = tmp_path / "seed.sql"
    sql_file.write_text(HOSTILE_SQL, encoding="utf-8")

    query.cmd_submit_file(
        dbname="chiwei_test",
        file_path=str(sql_file),
        reason="复刻 prod bot_persona 到 chiwei-test",
    )

    payload = captured["payload"]
    assert payload["sql"] == HOSTILE_SQL  # byte-identical, $$ intact
    assert "$$" in payload["sql"]
    assert payload["sql"].count("\n") == HOSTILE_SQL.count("\n")
    assert payload["reason"] == "复刻 prod bot_persona 到 chiwei-test"
    assert payload["db"] == "chiwei_test"
    assert payload["submitted_by"] == "claude-code"


def test_submit_file_resolves_db_alias(captured, tmp_path):
    sql_file = tmp_path / "x.sql"
    sql_file.write_text("INSERT INTO t VALUES (1);", encoding="utf-8")
    query.cmd_submit_file(
        dbname=query.DB_ALIASES["chiwei-test"],
        file_path=str(sql_file),
        reason="r",
    )
    assert captured["payload"]["db"] == "chiwei_test"


def test_submit_file_via_main_argv(captured, tmp_path, monkeypatch):
    """End-to-end through main(): `submit @chiwei-test --file P --reason R`.
    The path/reason are discrete argv elements and must not be word-split."""
    sql_file = tmp_path / "seed.sql"
    sql_file.write_text(HOSTILE_SQL, encoding="utf-8")
    monkeypatch.setattr(
        sys, "argv",
        ["query.py", "submit", "@chiwei-test",
         "--file", str(sql_file), "--reason", "seed persona data"],
    )
    query.main()
    assert captured["payload"]["sql"] == HOSTILE_SQL
    assert captured["payload"]["db"] == "chiwei_test"
    assert captured["payload"]["reason"] == "seed persona data"


def test_submit_file_missing_file_errors(captured, tmp_path):
    with pytest.raises(SystemExit):
        query.cmd_submit_file(
            dbname="chiwei",
            file_path=str(tmp_path / "nope.sql"),
            reason="r",
        )
