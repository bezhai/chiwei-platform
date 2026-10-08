"""Replay: pictures reaching her model, in her regular round (``run_moment``).

Both ways a picture gets in front of her end as an OpenAI-style ``image_url`` block in a tool
result, carrying an address tool-service signed a moment ago (the picture itself is never stored
as a link): ``look_at_phone`` shows the pictures someone sent her, after checking each one can
really be fetched, and ``look_at_a_picture`` shows one of her own. In the recorded request each
block also says what fetching that address at the time of the call returns (``"fetched"``),
since that is what the provider adapter downloads and sends.

* ``pictures_reach_her`` — bezhai sends a message with two pictures, one of them never made it
  into the store. Her first moment opens the chat (the stored picture is shown, the missing one is
  written as unopenable) and takes out one of her own pictures. Ten minutes later her history
  sends both pictures again, fetched again. After the hourly cleanup her next moment has them
  replaced by a line saying they are no longer in front of her.
"""

from __future__ import annotations

import struct
import zlib
from datetime import datetime

import pytest

from app.infra.cst_time import CST, now_cst
from app.living.moment import run_moment
from app.living.pictures import remember_a_picture
from tests.replay import seeds
from tests.replay.harness import Reply, ToolUse

pytestmark = pytest.mark.integration

MOMENT = "living_life_moment"


def _png(red: int, green: int, blue: int) -> bytes:
    """A real one-pixel PNG of that colour, so each picture has its own bytes."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(bytes([0, red, green, blue]))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", pixels)
        + chunk(b"IEND", b"")
    )


def _at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 25, hour, minute, tzinfo=CST)


def _moment(replay):
    return lambda: run_moment(lane=replay.lane, persona_id="akao", clock=now_cst)


async def test_pictures_reach_her(replay):
    await seeds.seed_household()
    await seeds.seed_akaos_phone()
    replay.broker.declare_inbox("world")

    replay.objects.put("pictures/akao-sakura.png", _png(250, 200, 220), "image/png")
    await remember_a_picture(
        lane=replay.lane,
        persona_id="akao",
        file_name="pictures/akao-sakura.png",
        what="窗外的樱花",
        made_at=datetime(2026, 7, 24, 20, 0, tzinfo=CST),
    )
    replay.objects.put("images/bezhai-cat.png", _png(120, 90, 40), "image/png")
    # The second picture's object was never stored (the inbound cache missed it): it signs, and
    # the fetch answers 404.
    await seeds.bezhai_sends(
        [
            {"kind": "text", "text": "看我家猫"},
            {"kind": "image", "key": "img_v3_cat", "object": "images/bezhai-cat.png"},
            {"kind": "image", "key": "img_v3_gone", "object": "images/bezhai-gone.png"},
        ],
        summary="看我家猫[image][image]",
        at=_at(13, 55),
        name="cat",
    )
    await replay.start("agent-service")

    replay.model.script(
        MOMENT,
        Reply(
            tools=(ToolUse("look_at_phone", {"channel_id": str(seeds.DM_WITH_BEZHAI)}),)
        ),
        Reply(tools=(ToolUse("look_at_a_picture", {"which": "樱花"}),)),
        Reply(tools=(ToolUse("stop_for_now", {}),)),
    )
    await replay.step(
        "her moment opens the chat and takes out her own picture",
        _moment(replay),
        at=_at(14, 0),
    )

    replay.model.script(MOMENT, Reply(text="那只猫跟樱花一样，都是春天的颜色。"))
    await replay.step(
        "ten minutes later her history sends the pictures again",
        _moment(replay),
        at=_at(14, 10),
    )

    replay.model.script(MOMENT, Reply(text="接着整理胶片。"))
    await replay.step(
        "after the hourly cleanup the pictures are out of her history",
        _moment(replay),
        at=_at(15, 10),
    )

    replay.check("multimodal/pictures_reach_her")
