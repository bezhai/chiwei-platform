"""一次性脚本：从已有的 DiaryEntry 回溯生成历史 Journal

用法：
    cd apps/agent-service
    uv run python -m scripts.backfill_journals --start 2026-03-01 --end 2026-03-25
"""

import argparse
import asyncio
import logging
from datetime import date, timedelta

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")


async def backfill(start: date, end: date) -> None:
    from app.workers.journal_worker import generate_daily_journal, generate_weekly_journal

    # 1. 按日期顺序生成 daily journals
    current = start
    while current <= end:
        try:
            result = await generate_daily_journal(current)
            status = f"{len(result)} chars" if result else "skipped"
            logging.info(f"Daily journal {current}: {status}")
        except Exception as e:
            logging.error(f"Failed for {current}: {e}")
        current += timedelta(days=1)

    # 2. 生成涉及的 weekly journals
    monday = start - timedelta(days=start.weekday())
    while monday <= end:
        try:
            result = await generate_weekly_journal(monday)
            status = f"{len(result)} chars" if result else "skipped"
            logging.info(f"Weekly journal {monday}: {status}")
        except Exception as e:
            logging.error(f"Failed for week {monday}: {e}")
        monday += timedelta(days=7)


def main():
    parser = argparse.ArgumentParser(description="回溯生成历史 Journal")
    parser.add_argument("--start", required=True, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="结束日期 YYYY-MM-DD")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    asyncio.run(backfill(start, end))


if __name__ == "__main__":
    main()
