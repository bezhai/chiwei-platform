#!/usr/bin/env python3
"""Safe read-only query via PaaS Engine ops/query API."""

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


def main():
    if len(sys.argv) < 2:
        print("用法: query.py [@数据库] <SQL | schema>", file=sys.stderr)
        print(f"可用数据库: {', '.join(sorted(set(DB_ALIASES.values())))}", file=sys.stderr)
        sys.exit(1)

    args = sys.argv[1:]
    dbname = DEFAULT_DB

    # Parse @db prefix
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

    # Safety check (client-side, server also enforces)
    if WRITE_KEYWORDS.search(sql):
        print(f"ERROR: 拒绝执行写操作: {sql}", file=sys.stderr)
        sys.exit(1)

    # Call Dashboard API for audited db-query
    paas_api = os.environ.get("PAAS_API", "")
    cc_token = os.environ.get("DASHBOARD_CC_TOKEN", "")

    if not paas_api:
        print("ERROR: PAAS_API 环境变量未设置", file=sys.stderr)
        sys.exit(1)

    result = subprocess.run(
        [
            "curl", "-sfS", "-X", "POST",
            f"{paas_api}/dashboard/api/ops/db-query",
            "-H", "Content-Type: application/json",
            "-H", f"X-API-Key: {cc_token}",
            "-d", json.dumps({"db": dbname, "sql": sql}),
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        print(f"ERROR: API 调用失败: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    resp = json.loads(result.stdout)
    if "error" in resp and resp["error"]:
        print(f"ERROR: {resp['error']}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(
        {"columns": resp.get("columns", []), "rows": resp.get("rows", [])},
        default=str,
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    main()
