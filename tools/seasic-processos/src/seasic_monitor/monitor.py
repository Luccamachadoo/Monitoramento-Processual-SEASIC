from __future__ import annotations

from datetime import datetime, timezone

from .collectors import DemoCollector
from .database import MonitorDatabase


def run_demo(database: MonitorDatabase, stagnant_after_days: int = 30) -> int:
    database.initialize()
    processes = database.demo_processes()
    now = datetime.now(timezone.utc)
    run_id = database.create_execution(mode="DEMO_SINTETICA", started_at=now.isoformat())
    collector = DemoCollector()
    succeeded = 0
    failed = 0
    for process in processes:
        observation = collector.collect(process, now)
        outcome = database.record_observation(
            run_id=run_id,
            observation=observation,
            stagnant_after_days=stagnant_after_days,
        )
        if outcome["valid"]:
            succeeded += 1
        else:
            failed += 1
    database.finish_execution(
        run_id=run_id,
        total=len(processes),
        succeeded=succeeded,
        failed=failed,
    )
    return run_id

