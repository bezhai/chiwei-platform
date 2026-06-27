#!/usr/bin/env python3
"""ops-db skill query runner — read-only queries and write mutation submission."""

import json
import os
import re
import subprocess
import sys

WRITE_KEYWORDS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)

SCHEMA_SQL = (
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema='public' ORDER BY table_name"
)

DB_ALIASES = {
    "paas-engine": "paas_engine",
    "paas_engine": "paas_engine",
    "chiwei": "chiwei",
    "chiwei-test": "chiwei_test",
    "chiwei_test": "chiwei_test",
}
DEFAULT_DB = "paas_engine"


def get_env():
    paas_api = os.environ.get("PAAS_API", "")
    cc_token = os.environ.get("DASHBOARD_CC_TOKEN", "")
    if not paas_api:
        print("ERROR: PAAS_API 环境变量未设置", file=sys.stderr)
        sys.exit(1)
    return paas_api, cc_token


def curl_post(url, payload, token):
    result = subprocess.run(
        [
            "curl", "-sfS", "-X", "POST", url,
            "-H", "Content-Type: application/json",
            "-H", f"X-API-Key: {token}",
            "-d", json.dumps(payload),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: API 调用失败: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def curl_get(url, token):
    result = subprocess.run(
        [
            "curl", "-sfS", url,
            "-H", f"X-API-Key: {token}",
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: API 调用失败: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    return json.loads(result.stdout)


def cmd_query(args):
    """只读 SQL 查询，必须指定 @数据库。"""
    if not args or not args[0].startswith("@"):
        avail = ", ".join(sorted(set(DB_ALIASES.values())))
        print(f"ERROR: 必须指定数据库，如 @chiwei 或 @paas_engine（可用: {avail}）", file=sys.stderr)
        sys.exit(1)
    alias = args.pop(0)[1:]
    if alias not in DB_ALIASES:
        print(f"ERROR: 未知数据库 '{alias}'，可用: {', '.join(sorted(set(DB_ALIASES.values())))}", file=sys.stderr)
        sys.exit(1)
    dbname = DB_ALIASES[alias]
    if not args:
        print("ERROR: 缺少 SQL 查询", file=sys.stderr)
        sys.exit(1)

    sql = " ".join(args).strip()
    if sql.lower() == "schema":
        sql = SCHEMA_SQL

    if WRITE_KEYWORDS.search(sql):
        print(f"ERROR: 拒绝执行写操作（请用 submit 命令提交审批）: {sql}", file=sys.stderr)
        sys.exit(1)

    paas_api, cc_token = get_env()
    resp = curl_post(
        f"{paas_api}/dashboard/api/ops/db-query",
        {"db": dbname, "sql": sql},
        cc_token,
    )
    if "error" in resp and resp["error"]:
        print(f"ERROR: {resp['error']}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(
        {"columns": resp.get("columns", []), "rows": resp.get("rows", [])},
        default=str, ensure_ascii=False,
    ))


def _args_text(args):
    if isinstance(args, str):
        return args.strip()
    return " ".join(args).strip()


def _split_first_word(raw):
    m = re.match(r"^(\S+)(?:\s+([\s\S]*))?$", raw.strip())
    if not m:
        return "", ""
    return m.group(1), m.group(2) or ""


def cmd_submit(args):
    """submit @<db> <SQL> [-- reason: <说明>]
    提交 DDL/DML 申请，等待人工在 Dashboard 审批。
    """
    raw = _args_text(args)
    if not raw:
        print("用法: submit @<db> <SQL>", file=sys.stderr)
        sys.exit(1)

    dbname = DEFAULT_DB
    first_word, rest = _split_first_word(raw)
    if first_word.startswith("@"):
        alias = first_word[1:]
        if alias not in DB_ALIASES:
            print(f"ERROR: 未知数据库 '{alias}'", file=sys.stderr)
            sys.exit(1)
        dbname = DB_ALIASES[alias]
        raw = rest.strip()

    # 提取 reason：支持 "-- reason: xxx" 出现在任意位置
    reason = ""
    reason_match = re.search(r"--\s*reason:\s*(.+)$", raw, re.IGNORECASE)
    if reason_match:
        reason = reason_match.group(1).strip()
        sql = raw[:reason_match.start()].strip()
    else:
        sql = raw

    if not sql:
        print("ERROR: SQL 为空", file=sys.stderr)
        sys.exit(1)

    paas_api, cc_token = get_env()
    resp = curl_post(
        f"{paas_api}/dashboard/api/ops/db-mutations",
        {"db": dbname, "sql": sql, "reason": reason, "submitted_by": "claude-code"},
        cc_token,
    )
    if "error" in resp and resp.get("error"):
        print(f"ERROR: {resp['error']}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(resp, default=str, ensure_ascii=False))


def cmd_submit_file(dbname, file_path, reason):
    """submit @<db> --file <path> --reason <text>

    Shell-free SQL submission. The SQL is read verbatim from file_path as
    UTF-8 and POSTed byte-for-byte (no -- reason: regex, no join/split, no
    shell). Used for payloads that break the argv path: PL/pgSQL $$ blocks,
    $vars, quotes, %, newlines, literal '-- reason:' substrings.
    """
    if dbname not in DB_ALIASES:
        print(f"ERROR: 未知数据库 '{dbname}'", file=sys.stderr)
        sys.exit(1)
    dbname = DB_ALIASES[dbname]

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            sql = f.read()
    except OSError as e:
        print(f"ERROR: 无法读取 SQL 文件 '{file_path}': {e}", file=sys.stderr)
        sys.exit(1)

    if not sql.strip():
        print("ERROR: SQL 文件为空", file=sys.stderr)
        sys.exit(1)

    paas_api, cc_token = get_env()
    resp = curl_post(
        f"{paas_api}/dashboard/api/ops/db-mutations",
        {"db": dbname, "sql": sql, "reason": reason, "submitted_by": "claude-code"},
        cc_token,
    )
    if "error" in resp and resp.get("error"):
        print(f"ERROR: {resp['error']}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(resp, default=str, ensure_ascii=False))


def parse_submit_file_argv(argv):
    """Parse `submit [@db] --file <path> [--reason <text>]` from a raw argv
    list (discrete elements, never joined/split). Returns
    (dbname, file_path, reason) or None if this is not a --file submit.
    """
    if not argv or argv[0].lower() != "submit" or "--file" not in argv:
        return None

    rest = argv[1:]
    dbname = DEFAULT_DB
    file_path = None
    reason = ""
    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok.startswith("@"):
            dbname = tok[1:]
            i += 1
        elif tok == "--file":
            if i + 1 >= len(rest):
                print("ERROR: --file 缺少路径参数", file=sys.stderr)
                sys.exit(1)
            file_path = rest[i + 1]
            i += 2
        elif tok == "--reason":
            if i + 1 >= len(rest):
                print("ERROR: --reason 缺少说明参数", file=sys.stderr)
                sys.exit(1)
            reason = rest[i + 1]
            i += 2
        else:
            print(f"ERROR: --file 模式下无法识别的参数 '{tok}'", file=sys.stderr)
            sys.exit(1)

    if file_path is None:
        return None
    return dbname, file_path, reason


def cmd_status(args):
    """status <id> — 查询 mutation 审批状态。"""
    if not args:
        print("用法: status <mutation_id>", file=sys.stderr)
        sys.exit(1)

    mutation_id = args[0].strip()
    paas_api, cc_token = get_env()
    resp = curl_get(
        f"{paas_api}/dashboard/api/ops/db-mutations/{mutation_id}",
        cc_token,
    )
    print(json.dumps(resp, default=str, ensure_ascii=False))


def main():
    if len(sys.argv) < 2:
        print("用法: query.py <command> [args...]", file=sys.stderr)
        print("命令: [@db] <SQL|schema>  |  submit @<db> <SQL>  |  status <id>", file=sys.stderr)
        sys.exit(1)

    # --file 模式：从原始 argv（离散元素，不 join/不 split、不经 shell）解析。
    # 必须先于下面的 join/split，否则 SQL 文件内容与 reason 会被破坏。
    file_submit = parse_submit_file_argv(sys.argv[1:])
    if file_submit is not None:
        dbname, file_path, reason = file_submit
        cmd_submit_file(dbname, file_path, reason)
        return

    # skill 预处理以 "$ARGUMENTS" 传入，所有参数合为单个字符串。
    # 先拼回完整文本，再按首词分派。
    raw = " ".join(sys.argv[1:]).strip()
    first_word, rest = _split_first_word(raw)
    first_word = first_word.lower()

    if first_word == "submit":
        cmd_submit(rest)
    elif first_word == "status":
        cmd_status(rest.split())
    else:
        cmd_query(raw.split())


if __name__ == "__main__":
    main()
