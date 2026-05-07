"""Admin / public-API request Data classes — for HTTP source RPC endpoints.

Phase 6 v4 Gap 1 closure. Each Data wraps one HTTP endpoint's input;
all transient (no DB row); wired via Source.http(...) with response=True.
"""
from __future__ import annotations

from typing import Annotated

from app.runtime import Data, Key


class AdminLifeTickRequest(Data):
    persona_id: Annotated[str, Key]
    dry_run: bool = True
    force: bool = False

    class Meta:
        transient = True


class AdminGlimpseRequest(Data):
    persona_id: Annotated[str, Key]

    class Meta:
        transient = True


class DebugGlimpseRequest(Data):
    persona_id: Annotated[str, Key]

    class Meta:
        transient = True


class AdminVoiceRequest(Data):
    persona_id: Annotated[str, Key]

    class Meta:
        transient = True


class AdminScheduleRequest(Data):
    persona_id: Annotated[str, Key]
    plan_type: str = "daily"
    target_date: str | None = None

    class Meta:
        transient = True


class AdminSearchRequest(Data):
    # Data 要求至少一个 Key，AdminSearch 实际不去重（transient）；选 num 仅为
    # 满足约束（int 可序列化进 dedup hash）。queries 单独保留为 list 字段。
    queries: list[str]
    num: Annotated[int, Key] = 5

    class Meta:
        transient = True


class ScheduleListRequest(Data):
    # persona_id 可选——不传等价于 list all。
    persona_id: Annotated[str | None, Key] = None
    plan_type: str | None = None
    active_only: bool = True
    limit: int = 50

    class Meta:
        transient = True


class ScheduleCurrentRequest(Data):
    persona_id: Annotated[str, Key]

    class Meta:
        transient = True


class ScheduleDailyRequest(Data):
    target_date: Annotated[str, Key]
    persona_id: str = ""

    class Meta:
        transient = True


class ScheduleCreateRequest(Data):
    persona_id: Annotated[str, Key]
    plan_type: str
    period_start: str
    period_end: str
    time_start: str | None = None
    time_end: str | None = None
    content: str = ""
    mood: str | None = None
    energy_level: int | None = None
    response_style_hint: str | None = None
    proactive_action: dict | None = None
    target_chats: list | None = None
    model: str | None = None
    is_active: bool = True

    class Meta:
        transient = True


class ScheduleDeleteRequest(Data):
    schedule_id: Annotated[int, Key]

    class Meta:
        transient = True
