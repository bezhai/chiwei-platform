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
import uuid
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


def test_the_two_spellings_of_one_outbound_id_are_the_same_value():
    """32 位 hex 和标准 uuid 是**同一个值的两种写法**，不是两个编号。

    她眼前只会出现 hex 那一种（``app/living/snapshot.py`` 印「你刚做过、说过」，
    ``app/living/phone.py`` 印她打开的会话里自己那几行），而库里
    ``common_message.agent_outbound_id`` 是 uuid 列、存的是带短横那一种。中间那步换算
    错了不会报错：她照抄的编号查不到任何行，撤回只会说"没有这条"，看不出是写法错了。
    """
    v = CONTRACT["outbound_id_vector"]

    assert uuid.UUID(hex=v["hex"]) == uuid.UUID(v["uuid"])
    assert uuid.UUID(v["uuid"]).hex == v["hex"], "库里那一行换回她见过的写法"
    assert str(uuid.UUID(hex=v["hex"])) == v["uuid"], "她见过的写法换成库里那一种"


def test_both_spellings_come_off_the_same_derived_uuid():
    """线上那一串是怎么从同一个 uuid 派生出两种写法的 —— 照 ``mouth.send_message`` 走。

    那边一次派生、两处取值：``outbound_id = derived.hex`` 记进她自己的台账，
    ``message_id = f"{前缀}{derived}"`` 走线格式发给投递方，投递方剥掉前缀落进
    ``agent_outbound_id``。两处取值不同源的话，台账和公共层从此对不上账。
    """
    v = CONTRACT["outbound_id_vector"]
    derived = uuid.UUID(hex=v["hex"])

    assert derived.hex == v["hex"]
    assert f"{PROACTIVE_MESSAGE_ID_PREFIX}{derived}" == v["message_id"]
