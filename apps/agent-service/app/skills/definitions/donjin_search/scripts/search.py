#!/usr/bin/env python3
"""同人展搜索脚本 — 直接调用 AllCpp.cn API

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
import sys
import time
import urllib.error
import urllib.request

_ALLCPP_URL = "https://www.allcpp.cn/allcpp/event/eventMainListV2.do"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}
_TYPE_MAP = {
    "茶会": 1,
    "综合同人展": 2,
    "ONLY": 3,
    "线上活动": 6,
    "官方活动": 7,
    "综合展": 8,
    "同好包场": 10,
}
_MAX_RETRIES = 3


def _format_time(ts):
    if not ts:
        return ""
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ts / 1000))
    except Exception:
        return ""


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
    parser.add_argument(
        "--is-online", action="store_true", default=None, help="仅线上活动"
    )
    parser.add_argument("--recent-days", type=int, help="最近 N 天内的活动")
    parser.add_argument(
        "--activity-status", choices=["ongoing", "ended"], help="活动状态"
    )
    parser.add_argument("--activity-type", help="活动类型")
    parser.add_argument(
        "--ticket-status", type=int, choices=[1, 2, 3, 4, 5], help="售票状态"
    )

    args = parser.parse_args()

    # 构建 AllCpp API 参数
    recent_days = args.recent_days
    if args.activity_status == "ongoing":
        recent_days = -1
    elif args.activity_status == "ended":
        recent_days = -2

    params = {
        "keyword": args.query or "",
        "sort": 1,
        "page": 1,
        "page_size": 100,
    }
    if args.is_online:
        params["is_online"] = "true"
    if recent_days is not None:
        params["day"] = recent_days
    if args.activity_type and args.activity_type in _TYPE_MAP:
        params["type"] = _TYPE_MAP[args.activity_type]
    if args.ticket_status is not None:
        params["ticketStatus"] = args.ticket_status

    # 带重试调用 AllCpp API
    qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None and v != "")
    url = f"{_ALLCPP_URL}?{qs}"

    data = None
    for attempt in range(_MAX_RETRIES):
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            resp = urllib.request.urlopen(req, timeout=15)
            data = json.loads(resp.read())
            break
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            if attempt == _MAX_RETRIES - 1:
                print(f"AllCpp API 请求失败: {e}", file=sys.stderr)
                sys.exit(1)
            time.sleep(1)

    result = (data or {}).get("result", {})
    raw_events = result.get("list", [])

    # 结构化输出
    events = []
    for item in raw_events:
        events.append(
            {
                "name": item.get("name", ""),
                "type": item.get("type", ""),
                "tag": item.get("tag", ""),
                "enter_time": _format_time(item.get("enterTime")),
                "end_time": _format_time(item.get("endTime")),
                "wanna_go_count": item.get("wannaGoCount", 0),
                "city_name": item.get("cityName", "") or "",
                "enter_address": item.get("enterAddress", ""),
                "ended": item.get("ended", False) or False,
                "is_online": item.get("isOnline", 0) == 1,
                "event_url": f"https://www.allcpp.cn/allcpp/event/event.do?event={item['id']}",
            }
        )

    output = {"total": result.get("total", len(events)), "events": events}
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
