#!/usr/bin/env python3
"""同人展搜索脚本 — 调用 tool-service 内部 API

通过 SANDBOX_TOOL_SERVICE_URL 和 SANDBOX_TOOL_SERVICE_TOKEN 环境变量
访问 tool-service 的 /api/sandbox/donjin-search 端点。

用法:
    python3 search.py [选项]
    python3 search.py --help    显示可用参数

示例:
    python3 search.py --query "东方" --activity-type "ONLY"
    python3 search.py --activity-status ongoing
    python3 search.py --query "漫展" --recent-days 30
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error

TOOL_URL = os.environ.get("SANDBOX_TOOL_SERVICE_URL", "http://tool-service:8000")
TOOL_TOKEN = os.environ.get("SANDBOX_TOOL_SERVICE_TOKEN", "")


def main():
    parser = argparse.ArgumentParser(
        description="搜索同人展/漫展/ONLY展活动",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
可用活动类型 (--activity-type):
  茶会, 综合同人展, ONLY, 线上活动, 官方活动, 综合展, 同好包场

活动状态 (--activity-status):
  ongoing  未结束的活动
  ended    已结束的活动

售票状态 (--ticket-status):
  1=暂未开票  2=即将开票  3=正在售票  4=售票结束  5=站外售票
""",
    )
    parser.add_argument("--query", "-q", help="搜索关键词")
    parser.add_argument("--is-online", action="store_true", default=None, help="仅线上活动")
    parser.add_argument("--recent-days", type=int, help="最近 N 天内的活动")
    parser.add_argument("--activity-status", choices=["ongoing", "ended"], help="活动状态")
    parser.add_argument("--activity-type", help="活动类型")
    parser.add_argument("--ticket-status", type=int, choices=[1, 2, 3, 4, 5], help="售票状态")

    args = parser.parse_args()

    # 构建请求体（跳过 None 值）
    payload = {}
    if args.query:
        payload["query"] = args.query
    if args.is_online:
        payload["is_online"] = True
    if args.recent_days is not None:
        payload["recent_days"] = args.recent_days
    if args.activity_status:
        payload["activity_status"] = args.activity_status
    if args.activity_type:
        payload["activity_type"] = args.activity_type
    if args.ticket_status is not None:
        payload["ticket_status"] = args.ticket_status

    # 调用 tool-service API
    url = f"{TOOL_URL}/api/sandbox/donjin-search"
    data = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
    }
    if TOOL_TOKEN:
        headers["Authorization"] = f"Bearer {TOOL_TOKEN}"

    try:
        req = urllib.request.Request(url, data=data, headers=headers)
        resp = urllib.request.urlopen(req, timeout=15)
        result = json.loads(resp.read())
    except urllib.error.URLError as e:
        print(f"请求失败: {e}", file=sys.stderr)
        sys.exit(1)

    if not result.get("success"):
        print(f"搜索失败: {result.get('error', '未知错误')}", file=sys.stderr)
        sys.exit(1)

    # 输出结构化结果
    print(json.dumps(result["data"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
