from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import asyncio

from seasic_monitor.collectors import LiveCollectionDisabled
from seasic_monitor.database import MonitorDatabase
from seasic_monitor.domain import CollectionStatus, Observation, ProcessRecord
from seasic_monitor.playwright_collector import (
    PlaywrightCollector,
    PlaywrightCollectorConfig,
    _normalize_movement_date,
)
from seasic_monitor.monitor import run_edoc
from seasic_monitor.reporting import render_markdown


def observation(
    *,
    number: str = "100/2026",
    collected_at: str = "2026-10-06T09:00:00+00:00",
    status: CollectionStatus = CollectionStatus.OK,
    units: tuple[str, ...] = ("GABINETE",),
    movement: str = "Análise recebida",
    movement_date: str = "2026-10-05",
    error: str = "",
) -> Observation:
    return Observation(
        system="SEI",
        number=number,
        collected_at=collected_at,
        status=status,
        units=units,
        executive_sector=units[0] if units else "",
        last_movement=movement,
        movement_date=movement_date,
        error_code="TESTE" if error else "",
        error_message=error,
    )


class MonitorDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = MonitorDatabase(Path(self.temp_dir.name) / "monitor.sqlite")
        self.database.initialize()
        self.database.upsert_process(
            ProcessRecord(system="SEI", number="100/2026", area="TESTE")
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def add_snapshot(self, item: Observation) -> dict:
        run_id = self.database.create_execution(mode="TESTE")
        result = self.database.record_observation(run_id, item, stagnant_after_days=30)
        self.database.finish_execution(
            run_id,
            total=1,
            succeeded=int(result["valid"]),
            failed=int(not result["valid"]),
        )
        return result

    def test_failure_is_saved_without_replacing_last_valid_snapshot(self) -> None:
        valid = observation()
        self.add_snapshot(valid)
        saved_hash = self.database.latest_valid_snapshot("SEI", "100/2026")[
            "content_hash"
        ]

        failure = observation(
            collected_at="2026-10-06T10:00:00+00:00",
            status=CollectionStatus.SESSION_EXPIRED,
            error="A sessão expirou.",
        )
        first_failure = self.add_snapshot(failure)
        repeated_failure = self.add_snapshot(
            observation(
                collected_at="2026-10-06T11:00:00+00:00",
                status=CollectionStatus.SESSION_EXPIRED,
                error="A sessão expirou.",
            )
        )

        self.assertFalse(first_failure["valid"])
        self.assertEqual(first_failure["occurrences_added"], 1)
        self.assertEqual(repeated_failure["occurrences_added"], 0)
        latest_valid = self.database.latest_valid_snapshot("SEI", "100/2026")
        self.assertEqual(latest_valid["content_hash"], saved_hash)

    def test_changes_are_detected_once_and_unit_order_is_normalized(self) -> None:
        self.add_snapshot(observation(units=("GABINETE", "PROTOCOLO")))
        unchanged = self.add_snapshot(
            observation(
                collected_at="2026-10-06T10:00:00+00:00",
                units=("PROTOCOLO", "GABINETE"),
            )
        )
        changed = self.add_snapshot(
            observation(
                collected_at="2026-10-06T11:00:00+00:00",
                units=("DIPLAN",),
                movement="Encaminhado para análise",
                movement_date="2026-10-06",
            )
        )
        repeated = self.add_snapshot(
            observation(
                collected_at="2026-10-06T12:00:00+00:00",
                units=("DIPLAN",),
                movement="Encaminhado para análise",
                movement_date="2026-10-06",
            )
        )

        self.assertEqual(unchanged["comparison_status"], "SEM_MUDANCA")
        self.assertEqual(changed["occurrences_added"], 2)
        self.assertEqual(repeated["comparison_status"], "SEM_MUDANCA")
        self.assertEqual(repeated["occurrences_added"], 0)

    def test_stagnation_alert_is_created_once_for_the_same_movement(self) -> None:
        stale = observation(
            collected_at="2026-10-06T09:00:00+00:00",
            movement_date="2026-08-21",
            movement="Aguardando análise",
        )
        first = self.add_snapshot(stale)
        second = self.add_snapshot(
            observation(
                collected_at="2026-10-07T09:00:00+00:00",
                movement_date="2026-08-21",
                movement="Aguardando análise",
            )
        )

        self.assertEqual(first["occurrences_added"], 1)
        self.assertEqual(second["occurrences_added"], 0)

    def test_same_number_in_different_systems_is_a_distinct_process(self) -> None:
        self.database.upsert_process(ProcessRecord(system="e-DOC", number="100/2026"))
        self.assertEqual(self.database.process_count(), 2)

    def test_no_execution_is_not_reported_as_no_movement(self) -> None:
        report = self.database.run_report()
        self.assertIsNone(report)
        text = render_markdown(report)
        self.assertIn("a rotina ainda não rodou", text.casefold())
        self.assertIn("não significa", text.casefold())

    def test_playwright_collector_is_disabled_by_default(self) -> None:
        config = PlaywrightCollectorConfig.from_mapping(
            "SEI",
            {"enabled": False},
            self.temp_dir.name,
        )
        collector = PlaywrightCollector(config)
        with self.assertRaises(LiveCollectionDisabled):
            asyncio.run(collector.start())

    def test_edoc_runner_refuses_to_start_before_approval(self) -> None:
        self.database.upsert_process(
            ProcessRecord(system="e-DOC", number="200/2026", area="TESTE")
        )
        with self.assertRaises(LiveCollectionDisabled):
            asyncio.run(
                run_edoc(
                    database=self.database,
                    collector_settings={"enabled": False},
                    config_dir=self.temp_dir.name,
                    stagnant_after_days=30,
                    min_interval_seconds=5,
                    max_processes_per_run=500,
                )
            )
        self.assertIsNone(self.database.run_report())

    def test_local_date_is_normalized_to_iso(self) -> None:
        self.assertEqual(_normalize_movement_date("06/10/2026"), "2026-10-06")


if __name__ == "__main__":
    unittest.main()

