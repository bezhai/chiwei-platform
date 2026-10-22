"""给她的一轮加一只手、加一段输入，只改那个插件和清单。

``tests/living/radio`` 是一个只在测试里存在的插件，登记一只手（``turn_on_the_radio``）和一段输入段
（``radio.playing``）。这里的清单是 agent-service 那份加上它：插件列表末尾多它一个，``resident`` 的工具和
输入段末尾各多一个名字。别的插件、宿主、固定循环一行没改：组好的那一轮里有这只手和这一段，排在清单
写的位置上，别的插件登记的东西跟线上的 agent-service 一模一样。插件和清单两处缺一处，宿主都起不来。
"""
from __future__ import annotations

import datetime as dt
import importlib
from dataclasses import replace

import pytest

from app.agent.round import Round
from app.host import Plugin
from tests.living.conftest import HER_LANE
from tests.living.radio import PLAYING

_NOW = dt.datetime(2026, 7, 25, 14, 10, tzinfo=dt.timezone(dt.timedelta(hours=8)))


def _with_the_radio():
    """agent-service 的清单加上 radio 插件：插件列表、``resident`` 的工具和输入段末尾各多一个。"""
    from app.manifests.agent_service import MANIFEST, RESIDENT

    resident = replace(
        RESIDENT,
        tools=(*RESIDENT.tools, "turn_on_the_radio"),
        input=(*RESIDENT.input, "radio.playing"),
    )
    return replace(
        MANIFEST,
        plugins=(*MANIFEST.plugins, "tests.living.radio"),
        profiles={**MANIFEST.profiles, "resident": resident},
    )


def _what_the_others_register(host) -> list[tuple[str, str, str]]:
    return sorted(
        (r.plugin, r.kind, r.name)
        for r in host.registered()
        if r.plugin not in ("radio", "her-round")
    )


async def test_a_tool_and_an_input_section_take_only_their_plugin_and_the_manifest(
    app_host, monkeypatch, tmp_path
):
    monkeypatch.setenv("LANE", HER_LANE)
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path / "skills"))
    manifest = _with_the_radio()
    resident = manifest.profiles["resident"]
    assembled = []
    her_round = Plugin(
        name="her-round", setup=lambda ctx: assembled.append(ctx.profile("resident"))
    )
    plugins = [importlib.import_module(m).PLUGIN for m in manifest.plugins]

    with_the_radio = await app_host("agent-service", [*plugins, her_round], manifest=manifest)
    (profile,) = assembled

    assert [t.name for t in profile.tools] == list(resident.tools)
    radio_tool = profile.tools[-1]
    assert (radio_tool.plugin, radio_tool.value.trim) == ("radio", "material")
    assert await radio_tool.value.tool.invoke({}) == PLAYING
    assert [s.name for s in profile.input] == list(resident.input)
    radio_section = profile.input[-1]
    assert radio_section.plugin == "radio"
    her_turn = Round(
        app="agent-service",
        lane=HER_LANE,
        participant="akao",
        profile="resident",
        round_id="2026-07-25T14:10+08:00",
        now=_NOW,
        reason="test",
        due=None,
    )
    assert await radio_section.value(her_turn) == PLAYING

    # 线上那份清单起的宿主跟它在一个进程里登记不了两遍（收件箱、路由按名字全进程唯一），先停掉这一个。
    others = _what_the_others_register(with_the_radio)
    await with_the_radio.stop()
    as_it_runs = await app_host("agent-service")
    assert others == _what_the_others_register(as_it_runs)


async def test_the_plugin_without_the_manifest_or_the_manifest_without_the_plugin_does_not_start(
    app_host, monkeypatch, tmp_path
):
    """两处缺一处都起不来：插件登记了、清单没列，宿主说它没有位置；清单列了、插件没起，宿主说没人登记。"""
    from app.host.errors import UnknownContribution, UnplacedContribution
    from app.manifests.agent_service import MANIFEST

    monkeypatch.setenv("LANE", HER_LANE)
    monkeypatch.setenv("SKILLS_DIR", str(tmp_path / "skills"))
    as_it_runs = [importlib.import_module(m).PLUGIN for m in MANIFEST.plugins]
    radio = importlib.import_module("tests.living.radio").PLUGIN

    with pytest.raises(UnplacedContribution, match="turn_on_the_radio"):
        await app_host("agent-service", [*as_it_runs, radio])
    with pytest.raises(UnknownContribution, match="turn_on_the_radio"):
        await app_host("agent-service", as_it_runs, manifest=_with_the_radio())
