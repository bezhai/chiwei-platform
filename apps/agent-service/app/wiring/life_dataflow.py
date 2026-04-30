"""Phase 4 wiring: cron / event source -> fan-out / business node.

Graph topology (see docs/superpowers/specs/2026-04-30-dataflow-phase-4-...):

  cron */1   -> MinuteTick -> fan_out_life_tick + fan_out_voice
  cron 0,30 8-21 -> LightDayTick -> fan_out_light_day
  cron 0 22-7 except 3 -> LightNightTick -> fan_out_light_night
  cron 0 3 -> HeavyReviewTick -> fan_out_heavy
  cron 0 5 -> DailyPlanTick -> run_shared_daily_pipeline_node -> SharedDailyContext
                                                                -> fan_out_daily_plan
  cron */5 -> GlimpseTick -> fan_out_glimpse -> GlimpseTickRequest -> glimpse_tick_node
  LifeStateChanged -> glimpse_event_node
  GlimpseRequest .durable() -> run_glimpse_node
"""
from __future__ import annotations

from app.domain.life_dataflow import (
    DailyPlanRequest,
    DailyPlanTick,
    GlimpseRequest,
    GlimpseTick,
    GlimpseTickRequest,
    HeavyReviewRequest,
    HeavyReviewTick,
    LifeStateChanged,
    LifeTickRequest,
    LightDayTick,
    LightNightTick,
    LightReviewRequest,
    MinuteTick,
    SharedDailyContext,
    VoiceRequest,
)
from app.nodes.life_dataflow import (
    daily_plan_node,
    fan_out_daily_plan,
    fan_out_glimpse,
    fan_out_heavy,
    fan_out_life_tick,
    fan_out_light_day,
    fan_out_light_night,
    fan_out_voice,
    glimpse_event_node,
    glimpse_tick_node,
    heavy_review_node,
    life_tick_node,
    light_review_node,
    run_glimpse_node,
    run_shared_daily_pipeline_node,
    voice_node,
)
from app.runtime import Source, wire

TZ = "Asia/Shanghai"

# Cron tick entry points
wire(MinuteTick).from_(Source.cron("* * * * *", tz=TZ)).to(fan_out_life_tick, fan_out_voice)
wire(LightDayTick).from_(Source.cron("0,30 8-21 * * *", tz=TZ)).to(fan_out_light_day)
wire(LightNightTick).from_(Source.cron("0 22,23,0,1,2,4,5,6,7 * * *", tz=TZ)).to(fan_out_light_night)
wire(HeavyReviewTick).from_(Source.cron("0 3 * * *", tz=TZ)).to(fan_out_heavy)
wire(DailyPlanTick).from_(Source.cron("0 5 * * *", tz=TZ)).to(run_shared_daily_pipeline_node)
wire(GlimpseTick).from_(Source.cron("*/5 * * * *", tz=TZ)).to(fan_out_glimpse)

# Daily plan internal chain
wire(SharedDailyContext).to(fan_out_daily_plan)
wire(DailyPlanRequest).to(daily_plan_node)

# Per-persona business
wire(LifeTickRequest).to(life_tick_node)
wire(VoiceRequest).to(voice_node)
wire(LightReviewRequest).to(light_review_node)
wire(HeavyReviewRequest).to(heavy_review_node)

# Glimpse dual-path converge into GlimpseRequest
wire(GlimpseTickRequest).to(glimpse_tick_node)         # 5min periodic path
wire(LifeStateChanged).to(glimpse_event_node)          # immediate event path
wire(GlimpseRequest).to(run_glimpse_node).durable()    # durable multi-process
