"""coe-* lane 业务表自动建。

spec: docs/superpowers/specs/2026-05-11-dev-workflow-v2-phase-2-design.md §agent-service coe-* lane 自动建表
"""
import logging

from app.data.models import Base
from app.data.session import engine
from app.infra.config import settings

logger = logging.getLogger(__name__)


async def ensure_business_schema() -> None:
    """仅 coe-* lane 触发 SQLAlchemy Base.metadata.create_all。

    严格白名单守门：prod / blue / ppe-* / None lane 一律不建表。
    create_all 失败必须 raise（不 swallow）让 pod CrashLoopBackoff。
    """
    lane = settings.lane
    if not lane or not lane.startswith("coe-"):
        return
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception:
        logger.exception("auto create_all failed for coe lane %s, aborting startup", lane)
        raise
