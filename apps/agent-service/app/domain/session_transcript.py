"""SessionTranscript — 一条 agent 续接对话流的 PG durable Data（替代 Redis）.

``Agent.run(..., session_id=...)`` 是有状态续接：读 ``session_id`` 下存的 transcript
拼到 system prompt 之后让模型从上次断点继续，跑完把本轮新消息追加回去。这条 transcript
是整段 Message 序列的可回放快照（含 tool call / result + 各 provider 私有 blob 如
gemini ``thought_signature``）。

为什么从 Redis 换成 PG durable Data：
  * 开发机不能直连 Redis，旧 session **清不掉**，没法做干净冷启验证；
  * pod 重启 / 部署 Redis key 没了，她的**意识流就丢**；
  * Redis 黑盒**不可查**——没法 SQL 直查她这一天怎么想过来的。
换成 PG：ops-db 可清、durable 不丢、可直接 SQL 查这一天的 transcript。

设计上钉死的两条：

  * **transcript 是 str 字段（JSON 文本），不是 list 字段。** 这是形态选择、不是
    framework 限制（persist 层已支持 list / dict → JSONB）：整条 transcript 序列化成
    ``json.dumps([m.to_replay_dict() for m in combined], ensure_ascii=False)`` 这个
    字符串，1:1 平移落进一个 TEXT 列，正是 Redis 版现在写的那个字符串——保持与 Redis
    格式逐字节一致，换存储不改序列化语义。

  * **as_latest + Version，Key = session_id（不额外加 lane Key）。** 每轮 append 一版，
    对外读永远 ``select_latest`` 取最新那版全文（旧版留作历史，可 SQL 查、不删）。
    ``session_id`` 格式是 ``lane:actor:date``（见 ``app.agent.trace.make_session_id``），
    **lane 已经在 key 里**——不同泳道天然是不同 session_id、不同行，所以不像
    WorldState / LifeState 那样需要额外显式 lane Key（它们的 key 是 (lane, persona)，
    persona 单独会跨泳道撞）。这里单 session_id key 已带 lane，泳道隔离由 key 本身保证。

字段：``session_id``（Key）/ ``ver``（Version）/ ``transcript_json``（TEXT，整条
transcript 的 JSON 文本）。``transcript_json`` / ``session_id`` 均不撞 runtime 保留列
（id / created_at / updated_at / dedup_hash）。
"""

from __future__ import annotations

from typing import Annotated

from app.runtime.data import Data, Key, Version


class SessionTranscript(Data):
    """一条 agent 续接对话流的最新全文（as_latest，带 Version）。

    自然键 ``session_id``（``lane:actor:date``，已含 lane → 泳道天然隔离）。
    ``transcript_json`` 是整条 transcript 的 JSON 文本（``to_replay_dict`` + json.dumps），
    lossless 可回放。每轮 append 一版，读最新一版即她此刻完整的续接上下文。
    """

    session_id: Annotated[str, Key]
    ver: Annotated[int, Version] = 0
    transcript_json: str  # 整条 transcript 的 JSON 文本（lossless replay）
