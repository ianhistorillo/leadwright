"""Continue an interrupted quick-run job in-process (no HTTP needed)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.database.session import get_session_factory
from app.services.quick_pipeline import QuickPipelineService


async def main(job_id: str) -> int:
    factory = get_session_factory()
    session = factory()
    try:
        service = QuickPipelineService(session)
        result = service.prepare_continue(job_id)
        print(json.dumps({k: v for k, v in result.items() if k != "options"}, indent=2))
        if not result.get("should_start"):
            print(result.get("message") or "Nothing to start")
            return 0
        options = result.get("options") or {}
    finally:
        session.close()

    # process_until_done opens its own session
    await QuickPipelineService(factory()).process_until_done(job_id, options)
    print("done")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("job_id")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.job_id)))
