"""主动发 message_id 前缀的跨语言线格式契约（生产方这一侧）。

``proactive:<uuid>`` 这个形状由本服务产出（app/living/mouth.py 拼出来），由
lark-service（TS）的出站投递剥掉前缀取 uuid 落进 ``common_message.agent_outbound_id``。
前缀是两边共同的约定，但两边各写各的字面量 —— 只改一边不会有任何测试变红：
投递方静默认不出主动消息，那次开口在库里永久失联，全程零报错。

所以两侧测试读同一份向量：``contracts/proactive-message-id.json``。要骗过测试就得改
共享的那一份，而改了共享那一份，两侧一起转红。

读它的是测试、不是生产代码：两个镜像的 Dockerfile 都不 COPY ``contracts/``，
lark-service 还是 ``bun build --compile`` 出来的独立二进制，运行时读不到这份文件。
跟 ``contracts/mq-channel-routes.json`` 是同一套做法。

TS 侧：apps/lark-service/src/lark/outbound/proactive-message-id.test.ts
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.domain.chat_dataflow import PROACTIVE_MESSAGE_ID_PREFIX

# 两侧读的是同一份文件。
CONTRACT_PATH = (
    Path(__file__).resolve().parents[4] / "contracts" / "proactive-message-id.json"
)
CONTRACT: dict[str, Any] = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_prefix_matches_contract():
    assert PROACTIVE_MESSAGE_ID_PREFIX == CONTRACT["message_id_prefix"]
