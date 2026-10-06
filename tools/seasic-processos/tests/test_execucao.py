from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from seasic_monitor.collectors import LiveCollectionDisabled
from seasic_monitor.database import MonitorDatabase
from seasic_monitor.domain import CollectionStatus, Observation, ProcessRecord, utc_now
from seasic_monitor.monitor import RunPolicy, consult_one, run_collection
from seasic_monitor.reporting import render_markdown


# 09:00 em Brasília.
MORNING = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)


class FakeCollector:
    """Coletor sem rede: devolve o status programado para cada número."""

    def __init__(
        self,
        outcomes: dict[str, CollectionStatus] | None = None,
        *,
        expired: bool = False,
        ready: bool = True,
        start_error: BaseException | None = None,
        interrupt_on: str = "",
    ) -> None:
        self.outcomes = outcomes or {}
        self.expired = expired
        self.ready = ready
        self.start_error = start_error
        self.interrupt_on = interrupt_on
        self.consulted: list[str] = []
        self.closed = False

    async def start(self) -> None:
        if self.start_error:
            raise self.start_error

    async def close(self) -> None:
        self.closed = True

    async def is_session_expired(self) -> bool:
        return self.expired

    async def is_ready(self) -> bool:
        return self.ready

    async def collect(self, process: ProcessRecord) -> Observation:
        self.consulted.append(process.number)
        if process.number == self.interrupt_on:
            raise KeyboardInterrupt
        status = self.outcomes.get(process.number, CollectionStatus.OK)
        if status == CollectionStatus.OK:
            return Observation(
                system=process.system,
                number=process.number,
                collected_at=utc_now(),
                status=status,
                units=("GABINETE",),
                last_movement="Recebido",
                movement_date="2026-10-06",
            )
        return Observation(
            system=process.system,
            number=process.number,
            collected_at=utc_now(),
            status=status,
            error_code=status.value,
            error_message="Falha simulada.",
        )


class RunCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = MonitorDatabase(Path(self.temp_dir.name) / "monitor.sqlite")
        self.database.initialize()
        for number in ("1/2026", "2/2026", "3/2026", "4/2026"):
            self.database.upsert_process(ProcessRecord(system="EDOC", number=number))
        self.database.upsert_process(ProcessRecord(system="SEI", number="9/2026"))

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_with(
        self,
        collector: FakeCollector,
        policy: RunPolicy | None = None,
        now: datetime = MORNING,
    ) -> dict:
        run_id = asyncio.run(
            run_collection(
                self.database,
                "EDOC",
                {},
                self.temp_dir.name,
                policy or RunPolicy(min_interval_seconds=0),
                collector=collector,
                now=now,
            )
        )
        return self.database.run_report(run_id)

    def test_complete_run_only_counts_the_selected_system(self) -> None:
        collector = FakeCollector()
        report = self.run_with(collector)
        self.assertEqual(report["execution"]["status"], "OK")
        self.assertEqual(report["execution"]["scope"], "EDOC")
        self.assertEqual(report["active_count"], 4)  # o processo SEI não conta
        self.assertNotIn("não foram consultados", render_markdown(report))
        self.assertTrue(collector.closed)

    def test_session_expiring_mid_run_stops_the_round(self) -> None:
        collector = FakeCollector({"2/2026": CollectionStatus.SESSION_EXPIRED})
        report = self.run_with(collector)
        self.assertEqual(collector.consulted, ["1/2026", "2/2026"])
        self.assertEqual(report["execution"]["status"], "PARCIAL")
        self.assertIn("Sessão expirou", report["execution"]["notes"])
        self.assertIn("2 processo(s) ativo(s) não foram consultados", render_markdown(report))

    def test_expired_session_at_start_records_no_snapshot(self) -> None:
        collector = FakeCollector(expired=True)
        report = self.run_with(collector)
        self.assertEqual(collector.consulted, [])
        self.assertEqual(report["snapshots"], [])
        self.assertEqual(report["occurrences"], [])
        self.assertEqual(report["execution"]["status"], "FALHOU")
        self.assertIn("login", report["execution"]["notes"])

    def test_browser_start_error_is_recorded_as_failed_run(self) -> None:
        report = self.run_with(FakeCollector(start_error=RuntimeError("boom")))
        self.assertEqual(report["execution"]["status"], "FALHOU")
        self.assertIn("RuntimeError", report["execution"]["notes"])

    def test_consecutive_technical_failures_stop_the_round(self) -> None:
        collector = FakeCollector(
            {
                "1/2026": CollectionStatus.EXTRACTION_ERROR,
                "2/2026": CollectionStatus.UNAVAILABLE,
                "3/2026": CollectionStatus.EXTRACTION_ERROR,
            }
        )
        report = self.run_with(
            collector, RunPolicy(min_interval_seconds=0, max_consecutive_failures=2)
        )
        self.assertEqual(collector.consulted, ["1/2026", "2/2026"])
        self.assertIn("falhas técnicas seguidas", report["execution"]["notes"])

    def test_not_found_does_not_count_as_technical_failure(self) -> None:
        collector = FakeCollector(
            {
                "1/2026": CollectionStatus.NOT_FOUND,
                "2/2026": CollectionStatus.NOT_FOUND,
            }
        )
        self.run_with(
            collector, RunPolicy(min_interval_seconds=0, max_consecutive_failures=2)
        )
        self.assertEqual(len(collector.consulted), 4)

    def test_canary_runs_first_and_aborts_round_when_it_fails(self) -> None:
        collector = FakeCollector({"3/2026": CollectionStatus.EXTRACTION_ERROR})
        report = self.run_with(
            collector, RunPolicy(min_interval_seconds=0, canary="3/2026")
        )
        self.assertEqual(collector.consulted, ["3/2026"])
        self.assertIn("processo de referência", report["execution"]["notes"])

    def test_canary_must_be_active_in_catalog(self) -> None:
        with self.assertRaises(ValueError):
            self.run_with(FakeCollector(), RunPolicy(min_interval_seconds=0, canary="99/2026"))
        self.assertIsNone(self.database.run_report())

    def test_daily_cap_counts_previous_runs_of_the_same_day(self) -> None:
        policy = RunPolicy(min_interval_seconds=0, max_processes_per_day=6)
        self.run_with(FakeCollector(), policy, now=datetime.now(timezone.utc))
        with self.assertRaises(ValueError) as error:
            self.run_with(FakeCollector(), policy, now=datetime.now(timezone.utc))
        self.assertIn("teto diário", str(error.exception))

    def test_outside_allowed_hours_nothing_is_collected(self) -> None:
        collector = FakeCollector()
        with self.assertRaises(ValueError):
            self.run_with(collector, RunPolicy(allowed_hours="05:00-08:00"))
        self.assertEqual(collector.consulted, [])
        self.assertIsNone(self.database.run_report())
        # Janela que atravessa a meia-noite: 22:00-10:00 inclui 09:00.
        self.run_with(FakeCollector(), RunPolicy(min_interval_seconds=0, allowed_hours="22:00-10:00"))

    def test_interrupted_run_is_finalized(self) -> None:
        collector = FakeCollector(interrupt_on="3/2026")
        with self.assertRaises(KeyboardInterrupt):
            self.run_with(collector)
        report = self.database.run_report()
        self.assertEqual(report["execution"]["status"], "PARCIAL")
        self.assertEqual(report["execution"]["succeeded"], 2)
        self.assertIn("interrompida", report["execution"]["notes"])
        self.assertTrue(collector.closed)

    def test_single_consultation_does_not_write_to_database(self) -> None:
        observation = asyncio.run(
            consult_one("e-DOC", "1/2026", {}, self.temp_dir.name, collector=FakeCollector())
        )
        self.assertTrue(observation.is_valid)
        self.assertIsNone(self.database.run_report())

    def test_single_consultation_requires_ready_session(self) -> None:
        with self.assertRaises(LiveCollectionDisabled):
            asyncio.run(
                consult_one(
                    "e-DOC", "1/2026", {}, self.temp_dir.name,
                    collector=FakeCollector(expired=True),
                )
            )


if __name__ == "__main__":
    unittest.main()
