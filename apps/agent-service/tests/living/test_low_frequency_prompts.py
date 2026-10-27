"""一周一次、一天一次的那两条 prompt 每次都从 Langfuse 现取，不读上一次留下的缓存。

Langfuse SDK（3.14.1，``Langfuse.get_prompt``）的缓存过期之后**不是**重新去取：它先把过期的那份交回来，
另起一个后台线程去刷新。我们的缓存时长是 10 秒，一条一周才取一次的 prompt 因此每次拿到的都是上一次
（一周前）留下的那份：2026-10-24 coe-review 跑每周回看，Langfuse 上那个泳道的 label 已经指向 v4，
跑的却是 v3。日记一天一次，同一个问题。

这里的 Langfuse 是照 SDK 缓存行为写的替身：缓存过期时先交回旧的、再在"后台"换成新的；
``cache_ttl_seconds=0`` 时每次都现取。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langfuse.api import Prompt_Text
from langfuse.model import TextPromptClient

from app.agent.prompts import PromptSet
from app.infra.propagation import Context
from app.living.day_page.pages import (
    DAY_PAGE_AGENT,
    DAY_PAGE_PROMPT_ID,
    DAY_PAGE_SEGMENT,
)
from app.living.persona_review.review import (
    PERSONA_REVIEW_AGENT,
    PERSONA_REVIEW_PROMPT_ID,
    PERSONA_REVIEW_SEGMENT,
)

# SDK 不传 ``cache_ttl_seconds`` 时的缓存时长（秒）。
_SDK_DEFAULT_TTL = 60


class _SdkCachingLangfuse:
    """Langfuse 客户端的 ``get_prompt``，缓存的行为照 SDK 3.14.1：

      * 缓存里没有，或者 ``cache_ttl_seconds=0``：现取，放进缓存；
      * 缓存里有、没过期：交回缓存里的；
      * 缓存里有、过期了：**交回过期的那份**，后台去取新的（这里取完立刻换进缓存，下一次才看得到）。
    """

    def __init__(self) -> None:
        self.server: dict[str, TextPromptClient] = {}
        self._cache: dict[str, tuple[TextPromptClient, float]] = {}
        self.clock = 0.0

    def release(self, prompt_id: str, version: int) -> None:
        """``production`` 这个 label 挪到 ``prompt_id`` 的 ``version`` 上。"""
        variables = {
            PERSONA_REVIEW_PROMPT_ID: "{{persona_name}}{{persona_core}}",
            DAY_PAGE_PROMPT_ID: "{{persona_name}}{{persona_core}}",
        }[prompt_id]
        self.server[prompt_id] = TextPromptClient(
            Prompt_Text(
                name=prompt_id,
                version=version,
                prompt=f"v{version}：{variables}",
                config={},
                labels=["production"],
                tags=[],
            )
        )

    def get_prompt(
        self, name: str, *, label: str | None = None, cache_ttl_seconds: int | None = None, **_: Any
    ) -> TextPromptClient:
        ttl = _SDK_DEFAULT_TTL if cache_ttl_seconds is None else cache_ttl_seconds
        cached = self._cache.get(name)
        if cached is None or ttl == 0:
            fresh = self.server[name]
            self._cache[name] = (fresh, self.clock + ttl)
            return fresh
        prompt, expires_at = cached
        if self.clock >= expires_at:
            self._cache[name] = (self.server[name], self.clock + ttl)
        return prompt


@pytest.fixture
def langfuse(monkeypatch) -> _SdkCachingLangfuse:
    client = _SdkCachingLangfuse()
    monkeypatch.setattr("app.agent.prompts._get_client", lambda: client)
    monkeypatch.setattr(
        "app.agent.prompts.current_context", lambda: Context(trace_id=None, lane=None)
    )
    monkeypatch.setattr("app.agent.prompts.settings", SimpleNamespace(lane="prod"))
    return client


@pytest.mark.parametrize(
    ("prompt_id", "agent", "segment"),
    [
        (PERSONA_REVIEW_PROMPT_ID, PERSONA_REVIEW_AGENT, PERSONA_REVIEW_SEGMENT),
        (DAY_PAGE_PROMPT_ID, DAY_PAGE_AGENT, DAY_PAGE_SEGMENT),
    ],
)
async def test_a_prompt_released_since_the_last_run_is_the_one_rendered(
    langfuse, prompt_id, agent, segment
):
    """上一次跑完之后 Langfuse 上发了新版本，下一次跑渲染的就是新版本，不是上一次缓存下来的那份。"""

    async def version_rendered() -> int:
        prompts = await PromptSet.resolve(
            {prompt_id: agent}, {prompt_id: segment}, None, participant="akao"
        )
        [(_, _, version)] = prompts.versions(prompt_id)
        return version

    langfuse.release(prompt_id, 3)
    assert await version_rendered() == 3

    langfuse.release(prompt_id, 4)
    langfuse.clock += 7 * 24 * 3600

    assert await version_rendered() == 4, "渲染的是上一次缓存下来的旧版本"
