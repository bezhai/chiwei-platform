#!/usr/bin/env python3
"""Bangumi ACG 数据库查询脚本

通过 SANDBOX_BANGUMI_TOKEN 环境变量访问 https://api.bgm.tv。

用法:
    python3 bangumi.py <子命令> [选项]
    python3 bangumi.py --help

子命令:
    search-subjects     搜索条目（动画、书籍、游戏等）
    search-characters   搜索角色
    search-persons      搜索现实人物（声优、漫画家等）
    get-subject         获取条目详情
    get-character       获取角色详情
    get-person          获取人物详情
    get-related         获取关联数据（角色、人物、关联条目）
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE_URL = "https://api.bgm.tv"
TOKEN = os.environ.get("SANDBOX_BANGUMI_TOKEN", "")

# 类型映射
SUBJECT_TYPE_MAP = {"书籍": 1, "动画": 2, "音乐": 3, "游戏": 4, "三次元": 6}
SUBJECT_TYPE_REVERSE = {1: "书籍", 2: "动画", 3: "音乐", 4: "游戏", 6: "三次元"}
CAREER_MAP = {
    "制作人员": "producer", "漫画家": "mangaka", "音乐人": "artist",
    "声优": "seiyu", "作家": "writer", "绘师": "illustrator", "演员": "actor",
}
CHARACTER_TYPE_REVERSE = {1: "角色", 2: "机体", 3: "舰船", 4: "组织"}
PERSON_TYPE_REVERSE = {1: "个人", 2: "公司", 3: "组合"}
CAREER_REVERSE = {
    "producer": "制作人员", "mangaka": "漫画家", "artist": "音乐人",
    "seiyu": "声优", "writer": "作家", "illustrator": "绘师", "actor": "演员",
}


def _request(path, method="GET", params=None, data=None):
    """发送 Bangumi API 请求"""
    url = f"{BASE_URL}{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
        if qs:
            url = f"{url}?{qs}"

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "panda1234/search",
    }
    if TOKEN:
        headers["Authorization"] = f"Bearer {TOKEN}"

    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        resp = urllib.request.urlopen(req, timeout=15)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"API 错误 {e.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"网络错误: {e}", file=sys.stderr)
        sys.exit(1)


# === 数据简化函数 ===

def _simplify_subject(s):
    rating = s.get("rating") or {}
    tags = s.get("tags") or []
    return {
        "id": s["id"],
        "type": SUBJECT_TYPE_REVERSE.get(s.get("type"), str(s.get("type"))),
        "name": s.get("name"),
        "name_cn": s.get("name_cn"),
        "summary": s.get("summary"),
        "date": s.get("date"),
        "platform": s.get("platform"),
        "score": rating.get("score"),
        "tags": [t["name"] for t in tags[:10]] if tags else None,
    }


def _simplify_character(c):
    return {
        "id": c["id"],
        "name": c.get("name"),
        "type": CHARACTER_TYPE_REVERSE.get(c.get("type"), str(c.get("type"))),
        "summary": c.get("summary"),
        "gender": c.get("gender"),
    }


def _simplify_person(p):
    careers = p.get("career") or []
    return {
        "id": p["id"],
        "name": p.get("name"),
        "type": PERSON_TYPE_REVERSE.get(p.get("type"), str(p.get("type"))),
        "career": [CAREER_REVERSE.get(c, c) for c in careers],
    }


# === 子命令实现 ===

def cmd_search_subjects(args):
    body = {"keyword": args.keyword, "sort": args.sort}
    filt = {}
    if args.types:
        filt["type"] = [SUBJECT_TYPE_MAP[t] for t in args.types if t in SUBJECT_TYPE_MAP]
    if args.tags:
        filt["tag"] = args.tags
    if args.start_date:
        filt.setdefault("air_date", []).append(f">={args.start_date}")
    if args.end_date:
        filt.setdefault("air_date", []).append(f"<{args.end_date}")
    if args.min_rating:
        filt.setdefault("rating", []).append(f">={args.min_rating}")
    if args.max_rating:
        filt.setdefault("rating", []).append(f"<={args.max_rating}")
    if filt:
        body["filter"] = filt

    params = {}
    if args.limit != 10:
        params["limit"] = args.limit
    if args.offset:
        params["offset"] = args.offset

    resp = _request("/v0/search/subjects", method="POST", params=params, data=body)
    result = {
        "total": resp.get("total", 0),
        "data": [_simplify_subject(s) for s in resp.get("data", [])],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_search_characters(args):
    body = {"keyword": args.keyword}
    params = {}
    if args.limit != 10:
        params["limit"] = args.limit
    if args.offset:
        params["offset"] = args.offset

    resp = _request("/v0/search/characters", method="POST", params=params, data=body)
    result = {
        "total": resp.get("total", 0),
        "data": [_simplify_character(c) for c in resp.get("data", [])],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_search_persons(args):
    body = {"keyword": args.keyword}
    if args.careers:
        body["career"] = [CAREER_MAP[c] for c in args.careers if c in CAREER_MAP]
    params = {}
    if args.limit != 10:
        params["limit"] = args.limit
    if args.offset:
        params["offset"] = args.offset

    resp = _request("/v0/search/persons", method="POST", params=params, data=body)
    result = {
        "total": resp.get("total", 0),
        "data": [_simplify_person(p) for p in resp.get("data", [])],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_get_subject(args):
    resp = _request(f"/v0/subjects/{args.id}")
    print(json.dumps(_simplify_subject(resp), ensure_ascii=False, indent=2))


def cmd_get_character(args):
    resp = _request(f"/v0/characters/{args.id}")
    print(json.dumps(_simplify_character(resp), ensure_ascii=False, indent=2))


def cmd_get_person(args):
    resp = _request(f"/v0/persons/{args.id}")
    print(json.dumps(_simplify_person(resp), ensure_ascii=False, indent=2))


def cmd_get_related(args):
    """获取关联数据: subject→characters/persons/relations, character→subjects/persons, person→characters/subjects"""
    entity = args.entity  # subject/character/person
    rel = args.relation   # characters/persons/relations/subjects
    resp = _request(f"/v0/{entity}s/{args.id}/{rel}")

    # 根据关联类型简化
    simplifiers = {
        "characters": lambda item: {
            "id": item["id"], "name": item.get("name"),
            "type": CHARACTER_TYPE_REVERSE.get(item.get("type"), ""),
            "relation": item.get("relation", ""),
        },
        "persons": lambda item: {
            "id": item["id"], "name": item.get("name"),
            "type": PERSON_TYPE_REVERSE.get(item.get("type"), ""),
            "relation": item.get("relation", ""),
            "career": [CAREER_REVERSE.get(c, c) for c in (item.get("career") or [])],
        },
        "relations": lambda item: {
            "id": item["id"], "name": item.get("name"), "name_cn": item.get("name_cn"),
            "type": SUBJECT_TYPE_REVERSE.get(item.get("type"), ""),
            "relation": item.get("relation", ""),
        },
        "subjects": lambda item: {
            "id": item["id"], "name": item.get("name"), "name_cn": item.get("name_cn"),
            "type": SUBJECT_TYPE_REVERSE.get(item.get("type"), ""),
            "staff": item.get("staff", ""),
        },
    }
    simplify = simplifiers.get(rel, lambda x: x)
    result = [simplify(item) for item in resp]
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(
        description="Bangumi ACG 数据库查询",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", help="可用子命令")

    # search-subjects
    p = sub.add_parser("search-subjects", help="搜索条目（动画、书籍、游戏等）")
    p.add_argument("--keyword", "-k", help="搜索关键词")
    p.add_argument("--types", nargs="+", choices=["书籍", "动画", "音乐", "游戏", "三次元"], help="条目类型")
    p.add_argument("--sort", default="match", choices=["match", "heat", "score"], help="排序方式")
    p.add_argument("--tags", nargs="+", help="标签筛选（多个为且关系）")
    p.add_argument("--start-date", help="开始日期 YYYY-MM-DD（包含）")
    p.add_argument("--end-date", help="结束日期 YYYY-MM-DD（不包含）")
    p.add_argument("--min-rating", type=int, help="最小评分 1-10")
    p.add_argument("--max-rating", type=int, help="最大评分 1-10")
    p.add_argument("--limit", type=int, default=10, help="返回数量（默认10）")
    p.add_argument("--offset", type=int, default=0, help="分页偏移")

    # search-characters
    p = sub.add_parser("search-characters", help="搜索角色")
    p.add_argument("--keyword", "-k", required=True, help="搜索关键词")
    p.add_argument("--limit", type=int, default=10, help="返回数量")
    p.add_argument("--offset", type=int, default=0, help="分页偏移")

    # search-persons
    p = sub.add_parser("search-persons", help="搜索现实人物（声优、漫画家等）")
    p.add_argument("--keyword", "-k", required=True, help="搜索关键词")
    p.add_argument("--careers", nargs="+", choices=["制作人员", "漫画家", "音乐人", "声优", "作家", "绘师", "演员"], help="职业筛选")
    p.add_argument("--limit", type=int, default=10, help="返回数量")
    p.add_argument("--offset", type=int, default=0, help="分页偏移")

    # get-subject
    p = sub.add_parser("get-subject", help="获取条目详情")
    p.add_argument("--id", type=int, required=True, help="条目 ID")

    # get-character
    p = sub.add_parser("get-character", help="获取角色详情")
    p.add_argument("--id", type=int, required=True, help="角色 ID")

    # get-person
    p = sub.add_parser("get-person", help="获取人物详情")
    p.add_argument("--id", type=int, required=True, help="人物 ID")

    # get-related
    p = sub.add_parser("get-related", help="获取关联数据")
    p.add_argument("--entity", required=True, choices=["subject", "character", "person"], help="实体类型")
    p.add_argument("--id", type=int, required=True, help="实体 ID")
    p.add_argument("--relation", required=True, choices=["characters", "persons", "relations", "subjects"], help="关联类型")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    dispatch = {
        "search-subjects": cmd_search_subjects,
        "search-characters": cmd_search_characters,
        "search-persons": cmd_search_persons,
        "get-subject": cmd_get_subject,
        "get-character": cmd_get_character,
        "get-person": cmd_get_person,
        "get-related": cmd_get_related,
    }
    dispatch[args.command](args)


if __name__ == "__main__":
    main()
