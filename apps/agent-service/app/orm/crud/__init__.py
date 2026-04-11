"""Backward-compatible re-exports.

All public functions that were previously in ``app.orm.crud`` (the single-file
module) are re-exported here so that::

    from app.orm.crud import get_bot_persona

continues to work everywhere.
"""

# ── persona ──
from app.orm.crud.persona import (  # noqa: F401
    get_all_persona_ids,
    get_bot_persona,
    get_gray_config,
    resolve_bot_name_for_persona,
    resolve_mentioned_personas,
    resolve_persona_id,
)

# ── model_provider ──
from app.orm.crud.model_provider import (  # noqa: F401
    get_model_and_provider_info,
    parse_model_id,
)

# ── message ──
from app.orm.crud.message import (  # noqa: F401
    get_chat_messages_in_range,
    get_group_name,
    get_message_by_id,
    get_message_content,
    get_username,
    scan_pending_messages,
    update_agent_response_bot,
    update_safety_status,
    update_vector_status,
)

# ── schedule ──
from app.orm.crud.schedule import (  # noqa: F401
    delete_schedule,
    get_current_schedule,
    get_daily_entries_for_date,
    get_latest_plan,
    get_plan_for_period,
    list_schedules,
    upsert_schedule,
)

# ── life_engine ──
from app.orm.crud.life_engine import (  # noqa: F401
    get_today_activity_states,
    load_latest_state,
    save_state,
)
