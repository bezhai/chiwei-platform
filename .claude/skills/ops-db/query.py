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
    """默认模式：只读 SQL 查询。"""
    dbname = DEFAULT_DB
    if args[0].startswith("@"):
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


def cmd_submit(args):
    """submit @<db> <SQL> [-- reason: <说明>]
    提交 DDL/DML 申请，等待人工在 Dashboard 审批。
    """
    if not args:
        print("用法: submit @<db> <SQL>", file=sys.stderr)
        sys.exit(1)

    dbname = DEFAULT_DB
    if args[0].startswith("@"):
        alias = args.pop(0)[1:]
        if alias not in DB_ALIASES:
            print(f"ERROR: 未知数据库 '{alias}'", file=sys.stderr)
            sys.exit(1)
        dbname = DB_ALIASES[alias]

    raw = " ".join(args).strip()

    # 从注释中提取 reason
    reason = ""
    sql_lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("-- reason:"):
            reason = stripped[len("-- reason:"):].strip()
        else:
            sql_lines.append(line)
    sql = "\n".join(sql_lines).strip()

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

    args = sys.argv[1:]
    first = args[0].lower()

    if first == "submit":
        cmd_submit(args[1:])
    elif first == "status":
        cmd_status(args[1:])
    else:
        cmd_query(args)


if __name__ == "__main__":
    main()
