"""时间归一到 CST —— 喂给 agent 的时间统一一个口径（阶段 0 Task 1）。

赤尾这套系统里时间出口曾经三种格式混着喂给同一个 agent：world 写 CST aware
ISO（``...+08:00``）、life 写 UTC aware ISO（``...+00:00``）、历史 chat 数据写 Unix
毫秒字符串。模型在一条 prompt 里看到两个"现在"、时间窗口比较差 8 小时。这个
模块把这三种**当前代码实际产生的**格式归一到 CST 一个口径：

  * :func:`parse` —— 三种历史格式 → aware ``datetime``（供按真实时刻比较）。
  * :func:`to_cst_hm` / :func:`to_cst_hms` —— aware ``datetime`` 或原始 raw →
    CST 的 ``HH:MM`` / ``HH:MM:SS`` 显示串（带 ``CST`` 标识，让模型看得出是
    北京时间）。
  * :func:`to_cst_full` —— 多给「年月日 + 星期」的完整口径
    （``YYYY-MM-DD 周X HH:MM CST``），喂 agent 当「现在是几点」的时间锚用：她算
    ``remind_at`` 这类绝对时间要知道今天是几号、星期几，只给时分会瞎填日期。
  * :func:`now_cst_iso` —— 当前 CST aware ISO（``...+08:00``）供新写。

刻意**只认这三种实际格式**，不做"任意表示"的万能解析（spec 决策 1 / non-goal）：
万能解析是为想象的输入过度设计、且更容易把脏数据静默解析错。无法解析的脏串在
显示侧原样回显（向后兼容兜底、不静默吞），在比较侧由调用方按各自语义兜底。

``CST`` 是这一处权威的 CST 偏移常量。项目里别处还散着同样的
``timezone(timedelta(hours=8))``，那些不在阶段 0 范围内、本次不动；阶段 0 的
那几个时间出口统一引这里。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

# 权威 CST 偏移常量（北京时间 UTC+8），一处定义。
CST = timezone(timedelta(hours=8))


def parse(raw: str | None) -> datetime | None:
    """把当前代码实际产生的三种历史格式解析成 aware ``datetime``，供比较归一。

    认得三种格式（都对应一个明确的真实时刻）：
      * CST aware ISO（``2026-06-03T20:30:00+08:00``，world 写）
      * UTC aware ISO（``...+00:00`` 或 ``...Z``，life 写）
      * Unix 毫秒字符串（``"1717..."``，历史 chat 数据写）

    解析失败 / 空 / naive（无时区，老脏数据）一律返回 ``None``——调用方据此走
    各自的兜底语义（比较侧退回 now-based fallback、显示侧原样回显）。不在这里
    猜时区：把 naive 当 CST 还是 UTC 都是猜，宁可返回 None 让调用方显式兜底。
    """
    if not raw:
        return None
    s = raw.strip()
    if not s:
        return None
    # Unix 毫秒：纯数字串，一律按**毫秒**解释——与 mailbox SQL 的
    # ``to_timestamp(col / 1000)`` 同口径（codex T3：helper 按位数兼容秒级、SQL 固定
    # 毫秒，两边口径会分裂;生产只写 13 位毫秒、没有秒级数据，删掉 speculative 的秒级
    # 兼容，让显示/解析与排序口径一致）。
    if s.isdigit():
        try:
            val = int(s)
        except ValueError:
            return None
        try:
            return datetime.fromtimestamp(val / 1000, tz=UTC)
        except (ValueError, OverflowError, OSError):
            return None
    # aware ISO（含 +08:00 / +00:00 / Z）。
    iso = s.replace("Z", "+00:00") if s.endswith("Z") else s
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # naive：无法确定它是哪个时区写的，不猜，交回调用方兜底。
        return None
    return dt


def to_cst(raw: str | None) -> datetime | None:
    """解析 + 转成 CST 时区的 aware ``datetime``；无法解析返回 ``None``。"""
    dt = parse(raw)
    return dt.astimezone(CST) if dt is not None else None


def to_cst_hm(raw: str | None) -> str:
    """把原始时间串显示成 CST 的 ``HH:MM CST``；无法解析则原样回显。"""
    dt = to_cst(raw)
    if dt is None:
        return f"{raw}" if raw else ""
    return f"{dt.strftime('%H:%M')} CST"


def to_cst_hms(raw: str | None) -> str:
    """把原始时间串显示成 CST 的 ``HH:MM:SS CST``；无法解析则原样回显。"""
    dt = to_cst(raw)
    if dt is None:
        return f"{raw}" if raw else ""
    return f"{dt.strftime('%H:%M:%S')} CST"


# 中文星期（周一=0 … 周日=6，对齐 ``datetime.weekday()``），给完整时间口径用。
_WEEKDAYS_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def to_cst_full(raw: str | None) -> str:
    """显示成 CST 的完整口径 ``YYYY-MM-DD 周X HH:MM CST``；无法解析则原样回显。

    比 :func:`to_cst_hm` 多给「年月日 + 星期」。喂给 agent 当「现在是几点」的时间锚
    要用它，而不是只给时分的 ``to_cst_hm`` —— 她记日程 / 算 ``remind_at`` 时要把
    「5 分钟后」「周五」这类相对时间换算成绝对 ISO，必须知道今天是几号、星期几；只给
    时分她只能瞎填日期分量，提醒会被排到错误（甚至已过去）的日期、永远不在该响的那
    一刻触发。
    """
    dt = to_cst(raw)
    if dt is None:
        return f"{raw}" if raw else ""
    return (
        f"{dt.strftime('%Y-%m-%d')} {_WEEKDAYS_CN[dt.weekday()]} "
        f"{dt.strftime('%H:%M')} CST"
    )


def now_cst() -> datetime:
    """当前时刻的 CST aware ``datetime``。"""
    return datetime.now(CST)


def now_cst_iso() -> str:
    """当前 CST aware ISO（``...+08:00``）—— 所有新写时间用它。"""
    return now_cst().isoformat()
