from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
import tempfile
import unittest

from seasic_monitor.database import MonitorDatabase
from seasic_monitor.domain import CollectionStatus, Observation, ProcessRecord, StagnationRule
from seasic_monitor.logs import LogSettings, configure_logging, logger, purge_old_logs
from seasic_monitor.rotina import rotate_backups, run_routine


NOW = datetime(2026, 10, 13, 9, 30, tzinfo=timezone.utc)  # 06:30 em Brasília
RULE = StagnationRule(limit=20, business_days=True)


def detach_file_handlers() -> None:
    for handler in list(logger.handlers):
        if getattr(handler, "_seasic_file", False):
            logger.removeHandler(handler)
            handler.close()


class FakeWriter:
    def __init__(self, settings) -> None:
        self.calls = []

    def replace(self, worksheet, values) -> None:
        self.calls.append(worksheet)


class RoutineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.database = MonitorDatabase(self.base / "data" / "processos.sqlite")
        self.database.initialize()
        self.database.upsert_process(ProcessRecord(system="EDOC", number="1/2026", area="DSAN"))
        self.config = {"collectors": {"EDOC": {"enabled": True}}}

    def tearDown(self) -> None:
        detach_file_handlers()
        self.temp_dir.cleanup()

    def fake_collect(self, status: str = "OK"):
        def collect(system: str) -> int:
            run_id = self.database.create_execution(mode="TESTE", scope=system)
            if status == "OK":
                self.database.record_observation(
                    run_id,
                    Observation(
                        system=system,
                        number="1/2026",
                        collected_at="2026-10-13T09:00:00+00:00",
                        status=CollectionStatus.OK,
                        units=("GSP",),
                        last_movement="Recebido",
                        movement_date="2026-08-03",
                    ),
                    RULE,
                )
                self.database.finish_execution(run_id, total=1, succeeded=1, failed=0)
            else:
                self.database.finish_execution(
                    run_id, total=0, succeeded=0, failed=0, notes="Sessão expirada.", planned=1
                )
            return run_id

        return collect

    def test_successful_routine_writes_reports_backup_and_marks_summary(self) -> None:
        results = run_routine(
            self.database, self.config, self.base, RULE, collect=self.fake_collect(), now=NOW
        )
        self.assertTrue(all(result.ok for result in results), results)
        self.assertEqual(
            [result.step for result in results],
            ["coleta e-DOC", "resumo", "visão", "backup", "logs"],
        )
        summary = self.base / "data" / "relatorios" / "resumo-2026-10-13-0630.md"
        self.assertIn("Parado", summary.read_text())
        self.assertTrue((self.base / "data" / "relatorios" / "visao-2026-10-13-0630.csv").is_file())
        self.assertTrue((self.base / "data" / "backup" / "processos-2026-10-13.sqlite").is_file())
        # O resumo gravado conta como comunicado: não se repete na próxima rotina.
        self.assertEqual(self.database.pending_occurrences()["occurrences"], [])
        self.assertEqual(summary.stat().st_mode & 0o777, 0o600)

    def test_failed_collection_does_not_stop_the_other_steps(self) -> None:
        results = run_routine(
            self.database,
            self.config,
            self.base,
            RULE,
            collect=self.fake_collect(status="FALHOU"),
            now=NOW,
        )
        by_step = {result.step: result for result in results}
        self.assertFalse(by_step["coleta e-DOC"].ok)
        self.assertIn("Sessão expirada", by_step["coleta e-DOC"].message)
        self.assertTrue(by_step["resumo"].ok and by_step["backup"].ok)
        summary = (self.base / "data" / "relatorios" / "resumo-2026-10-13-0630.md").read_text()
        self.assertIn("nenhuma", summary)  # carimbo: não houve coleta válida

    def test_collector_exception_is_reported_as_failed_step(self) -> None:
        def broken(system: str) -> int:
            raise ValueError("Configuração incompleta para EDOC: entry_url.")

        results = run_routine(self.database, self.config, self.base, RULE, collect=broken, now=NOW)
        self.assertFalse(results[0].ok)
        self.assertIn("entry_url", results[0].message)

    def test_no_enabled_collector_is_a_failure_not_silence(self) -> None:
        results = run_routine(self.database, {}, self.base, RULE, collect=self.fake_collect(), now=NOW)
        self.assertFalse(results[0].ok)
        self.assertIn("Nenhum coletor habilitado", results[0].message)

    def test_sheet_is_published_when_enabled(self) -> None:
        credential = self.base / "conta.json"
        credential.write_text("{}")
        writers: list[FakeWriter] = []

        def factory(settings):
            writers.append(FakeWriter(settings))
            return writers[-1]

        config = {
            **self.config,
            "sheets": {
                "enabled": True,
                "spreadsheet_id": "x",
                "credentials_path": str(credential),
            },
        }
        results = run_routine(
            self.database,
            config,
            self.base,
            RULE,
            collect=self.fake_collect(),
            sheet_writer=factory,
            now=NOW,
        )
        self.assertIn("planilha", [result.step for result in results])
        self.assertEqual(writers[0].calls, ["Visão atual — robô"])

    def test_old_backups_are_rotated(self) -> None:
        folder = self.base / "backup"
        folder.mkdir()
        for day in range(1, 6):
            (folder / f"processos-2026-10-0{day}.sqlite").write_text("")
        (folder / "outro-arquivo.sqlite").write_text("")
        removed = rotate_backups(folder, keep=2)
        self.assertEqual(
            sorted(path.name for path in removed),
            ["processos-2026-10-01.sqlite", "processos-2026-10-02.sqlite", "processos-2026-10-03.sqlite"],
        )
        self.assertTrue((folder / "outro-arquivo.sqlite").exists())


class LogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.settings = LogSettings.from_config({"logs": {"retention_days": 30}}, self.temp_dir.name)

    def tearDown(self) -> None:
        detach_file_handlers()
        self.temp_dir.cleanup()

    def test_log_file_per_day_without_process_content(self) -> None:
        path = configure_logging(self.settings, date(2026, 10, 13))
        self.assertEqual(path.name, "seasic-2026-10-13.log")
        database = MonitorDatabase(Path(self.temp_dir.name) / "m.sqlite")
        database.initialize()
        database.upsert_process(ProcessRecord(system="EDOC", number="1/2026"))

        import asyncio
        from seasic_monitor.monitor import RunPolicy, run_collection

        class Collector:
            async def start(self): ...
            async def close(self): ...
            async def is_session_expired(self): return False
            async def is_ready(self): return True
            async def collect(self, process):
                return Observation(
                    system=process.system,
                    number=process.number,
                    collected_at="2026-10-13T09:00:00+00:00",
                    status=CollectionStatus.OK,
                    units=("UNIDADE SIGILOSA",),
                    last_movement="Andamento com dado pessoal",
                    movement_date="2026-10-13",
                )

        asyncio.run(
            run_collection(
                database, "EDOC", {}, self.temp_dir.name,
                RunPolicy(min_interval_seconds=0), collector=Collector(),
                now=datetime(2026, 10, 13, 12, tzinfo=timezone.utc),
            )
        )
        text = path.read_text()
        self.assertIn("processo=EDOC:1/2026 status=OK", text)
        self.assertNotIn("SIGILOSA", text)
        self.assertNotIn("dado pessoal", text)

    def test_purge_respects_retention(self) -> None:
        self.settings.directory.mkdir(parents=True)
        for name in ("seasic-2026-08-01.log", "seasic-2026-09-20.log", "notas.log"):
            (self.settings.directory / name).write_text("")
        removed = purge_old_logs(self.settings, date(2026, 10, 13))
        self.assertEqual([path.name for path in removed], ["seasic-2026-08-01.log"])
        self.assertTrue((self.settings.directory / "notas.log").exists())

    def test_invalid_retention_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            LogSettings.from_config({"logs": {"retention_days": 0}}, self.temp_dir.name)


if __name__ == "__main__":
    unittest.main()
