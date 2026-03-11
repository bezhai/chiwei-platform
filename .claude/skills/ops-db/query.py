#!/usr/bin/env python3
"""Safe read-only query against PostgreSQL databases."""

import json
import re
import subprocess
import sys
from urllib.parse import urlparse

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


def get_secret_value(key: str) -> str:
    raw = subprocess.check_output(
        ["kubectl", "get", "secret", "paas-engine-secret", "-n", "prod",
         "-o", f"jsonpath={{.data.{key}}}"],
        text=True,
    )
    import base64
    return base64.b64decode(raw).decode()


def get_endpoint_ip() -> str:
    return subprocess.check_output(
        ["kubectl", "get", "endpoints", "postgres", "-n", "prod",
         "-o", "jsonpath={.subsets[0].addresses[0].ip}"],
        text=True, stderr=subprocess.DEVNULL,
    ).strip()


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

    # Safety check
    if WRITE_KEYWORDS.search(sql):
        print(f"ERROR: 拒绝执行写操作: {sql}", file=sys.stderr)
        sys.exit(1)

    # Get connection info
    db_url = get_secret_value("DATABASE_URL")
    endpoint_ip = get_endpoint_ip()
    parsed = urlparse(db_url)

    import psycopg2
    conn = psycopg2.connect(
        host=endpoint_ip,
        port=parsed.port or 5432,
        user=parsed.username,
        password=parsed.password,
        dbname=dbname,
    )
    conn.set_session(readonly=True)

    try:
        cur = conn.cursor()
        cur.execute(sql)
        columns = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
        # Output as JSON for easy parsing
        print(json.dumps({"columns": columns, "rows": [list(r) for r in rows]},
                         default=str, ensure_ascii=False))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
