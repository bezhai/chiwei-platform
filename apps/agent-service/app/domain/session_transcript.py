"""SessionTranscript — 一整条可回放对话流的 durable PG Data。

存的是一段 ``Message`` 序列的可回放快照（含 tool call / result + 各 provider 私有
blob 如 gemini ``thought_signature``），读写在 :mod:`app.agent.session`，跨轮连续的
上下文（:mod:`app.agent.continuity`）就落在这张表上。

存 PG 不存 Redis 的三条理由都还成立：开发机连不上 Redis 就没法做干净的冷启验证、
pod 重启 key 就没了、黑盒查不了。PG 这边 ops-db 可清、重启不丢、可以直接 SQL 查她这
一天怎么想过来的。

设计上钉死的两条：

  * **transcript 是 str 字段（JSON 文本），不是 list 字段。** 这是形态选择、不是
    framework 限制（persist 层已支持 list / dict → JSONB）：整条 transcript 序列化成
    ``json.dumps([m.to_replay_dict() for m in messages], ensure_ascii=False)`` 落进一
    个 TEXT 列。一条 transcript 天然是一个不透明的整体，不按元素查、也不按元素改。

  * **as_latest + Version，Key = session_id（不额外加 lane Key）。** 每次写一版，对外
    读永远 ``select_latest`` 取最新那版全文（旧版留作历史，可 SQL 查、不删）。
    ``session_id`` 由调用方拼，**泳道放在 key 里**（比如 ``lane:persona_id``）——不同
    泳道天然是不同 session_id、不同行，泳道隔离由 key 本身保证，不需要额外显式的
    lane Key。

``ver`` 同时是乐观并发的令牌：写入方带着读到的那一版做 CAS，别人在中间写过就一行都不
落（见 :func:`app.agent.session.replace_session`）。

字段：``session_id``（Key）/ ``ver``（Version）/ ``transcript_json``（TEXT，整条
transcript 的 JSON 文本）。``transcript_json`` / ``session_id`` 均不撞 runtime 保留列
（id / created_at / updated_at / dedup_hash）。
"""

from __future__ import annotations

from typing import Annotated

from app.runtime.data import Data, Key, Version


class SessionTranscript(Data):
    """一条可回放对话流的最新全文（as_latest，带 Version）。

    自然键 ``session_id``（调用方拼，已含 lane → 泳道天然隔离）。
    ``transcript_json`` 是整条 transcript 的 JSON 文本（``to_replay_dict`` + json.dumps），
    lossless 可回放。每次写一版，读最新一版即她此刻完整的上下文。
    """

    session_id: Annotated[str, Key]
    ver: Annotated[int, Version] = 0
    transcript_json: str  # 整条 transcript 的 JSON 文本（lossless replay）
