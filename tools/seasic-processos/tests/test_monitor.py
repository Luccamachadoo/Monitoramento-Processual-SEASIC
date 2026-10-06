from __future__ import annotations

from datetime import date
from pathlib import Path
import sqlite3
import tempfile
import unittest
import asyncio

from seasic_monitor.collectors import LiveCollectionDisabled
from seasic_monitor.database import SCHEMA as LEGACY_SCHEMA, MonitorDatabase
from seasic_monitor.domain import (
    CollectionStatus,
    Observation,
    ProcessRecord,
    local_date,
    number_matches,
)
from seasic_monitor.playwright_collector import (
    PlaywrightCollector,
    PlaywrightCollectorConfig,
    _normalize_movement_date,
)
from seasic_monitor.monitor import RunPolicy, prepare_session, run_collection
from seasic_monitor.reporting import (
    render_current_view,
    render_executive_summary,
    render_markdown,
)


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

    def test_detail_navigation_requires_a_scoped_result_and_ready_marker(self) -> None:
        selectors = {
            "process_input": "#process",
            "search_button": "#search",
            "result_ready": "#results",
            "result_row": "table tbody tr",
            "detail_link": "a.details",
            "units_open": ".units",
            "last_movement": ".movement",
            "movement_date": ".movement-date",
        }
        config = PlaywrightCollectorConfig.from_mapping(
            "EDOC",
            {
                "enabled": True,
                "entry_url": "https://example.invalid/search",
                "selectors": selectors,
            },
            self.temp_dir.name,
        )
        with self.assertRaises(LiveCollectionDisabled):
            PlaywrightCollector(config).validate_config()

        selectors["process_number_cell"] = "td:first-child"
        selectors["detail_ready"] = ".process-details"
        config = PlaywrightCollectorConfig.from_mapping(
            "EDOC",
            {
                "enabled": True,
                "entry_url": "https://example.invalid/search",
                "selectors": selectors,
            },
            self.temp_dir.name,
        )
        PlaywrightCollector(config).validate_config()

    def test_manual_login_can_open_before_collection_selectors_are_mapped(self) -> None:
        config = PlaywrightCollectorConfig.from_mapping(
            "EDOC",
            {
                "enabled": True,
                "entry_url": "https://example.invalid/search",
                "selectors": {},
            },
            self.temp_dir.name,
        )
        collector = PlaywrightCollector(config)
        collector.validate_config(require_collection_selectors=False)
        with self.assertRaises(LiveCollectionDisabled):
            collector.validate_config()

    def test_edoc_runner_refuses_to_start_before_approval(self) -> None:
        self.database.upsert_process(
            ProcessRecord(system="e-DOC", number="200/2026", area="TESTE")
        )
        with self.assertRaises(LiveCollectionDisabled):
            asyncio.run(
                run_collection(
                    self.database,
                    "EDOC",
                    {"enabled": False},
                    self.temp_dir.name,
                    RunPolicy(),
                )
            )
        self.assertIsNone(self.database.run_report())

    def test_local_date_is_normalized_to_iso(self) -> None:
        self.assertEqual(_normalize_movement_date("06/10/2026"), "2026-10-06")
        self.assertEqual(
            _normalize_movement_date("Enviado em 06/10/2026 às 13:35"),
            "2026-10-06",
        )

    def test_login_failure_is_reported_without_name_error(self) -> None:
        settings = {"enabled": True, "entry_url": "https://example.invalid/"}
        # Sem Playwright instalado, start() falha; a falha deve virar
        # LiveCollectionDisabled (mensagem ao operador), nunca NameError.
        with self.assertRaises(LiveCollectionDisabled):
            asyncio.run(prepare_session("EDOC", settings, self.temp_dir.name))

    def test_interrupted_execution_is_closed_on_next_run(self) -> None:
        orphan = self.database.create_execution(mode="TESTE")
        self.database.record_observation(orphan, observation(), stagnant_after_days=30)
        self.database.create_execution(mode="TESTE")
        report = self.database.run_report(orphan)
        self.assertEqual(report["execution"]["status"], "FALHOU")
        self.assertEqual(report["execution"]["succeeded"], 1)
        self.assertIn("interrompida", report["execution"]["notes"])

    def test_days_without_movement_use_brasilia_date(self) -> None:
        # 01:30 UTC de 07/10 ainda é 06/10 em Brasília.
        self.assertEqual(local_date("2026-10-07T01:30:00+00:00").isoformat(), "2026-10-06")
        result = self.add_snapshot(
            observation(
                collected_at="2026-10-07T01:30:00+00:00",
                movement_date="2026-09-07",
            )
        )
        self.assertEqual(result["occurrences_added"], 0)  # 29 dias, abaixo de 30

    def test_edoc_number_with_suffix_matches_registered_short_number(self) -> None:
        self.assertTrue(number_matches("2439/2026", "2439/2026-COMPR-SEASIC"))
        self.assertTrue(number_matches("2439/2026-COMPR-SEASIC", " 2439/2026-compr-seasic "))
        self.assertFalse(number_matches("439/2026", "2439/2026-COMPR-SEASIC"))
        self.assertFalse(number_matches("2439/2026", "2439/20261"))
        self.assertFalse(number_matches("2439/2026-COMPR", "2439/2026-COMPR-SEASIC"))
        self.assertFalse(number_matches("2439/2026", ""))

    def test_display_number_is_stored_and_old_databases_are_migrated(self) -> None:
        legacy = Path(self.temp_dir.name) / "legacy.sqlite"
        connection = sqlite3.connect(legacy)
        connection.executescript(LEGACY_SCHEMA)  # esquema sem display_number
        connection.close()
        MonitorDatabase(legacy).initialize()
        connection = sqlite3.connect(legacy)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(snapshots)")}
        connection.close()
        self.assertIn("display_number", columns)

        result = self.add_snapshot(
            Observation(
                system="SEI",
                number="100/2026",
                collected_at="2026-10-06T09:00:00+00:00",
                status=CollectionStatus.OK,
                units=("GABINETE",),
                last_movement="Recebido",
                movement_date="2026-10-06",
                display_number="100/2026-COMPR-SEASIC",
            )
        )
        history = self.database.process_history("SEI", "100/2026")
        self.assertEqual(history["snapshots"][0]["id"], result["snapshot_id"])
        self.assertEqual(history["snapshots"][0]["display_number"], "100/2026-COMPR-SEASIC")

    def test_current_view_shows_last_valid_state_and_current_failure(self) -> None:
        self.database.upsert_process(
            ProcessRecord(system="SEI", number="100/2026", area="DSAN", description="Câmaras frias")
        )
        self.add_snapshot(observation(units=("GSP", "DIPLAN"), movement_date="2026-09-06"))
        self.add_snapshot(
            observation(
                collected_at="2026-10-06T10:00:00+00:00",
                status=CollectionStatus.UNAVAILABLE,
                error="fora do ar",
            )
        )
        view = self.database.current_view()
        markdown = render_current_view(view, today=date(2026, 10, 6))
        self.assertIn("DIPLAN; GSP", markdown)
        self.assertIn("06/09/2026", markdown)
        self.assertIn("| 30 |", markdown)
        self.assertIn("Falha atual: INDISPONIVEL", markdown)
        self.assertIn("Câmaras frias", markdown)
        csv_text = render_current_view(view, "csv", today=date(2026, 10, 6))
        self.assertTrue(csv_text.startswith("Sistema,Processo,"))
        self.assertIn("SEI,100/2026,DSAN", csv_text)

    def test_current_view_without_execution_warns_instead_of_showing_no_movement(self) -> None:
        markdown = render_current_view(self.database.current_view())
        self.assertIn("nenhuma", markdown)
        self.assertIn("Nunca consultado", markdown)

    def test_executive_summary_lists_each_occurrence_once(self) -> None:
        self.add_snapshot(observation(units=("GABINETE",)))
        self.add_snapshot(
            observation(
                collected_at="2026-10-06T11:00:00+00:00",
                units=("DIPLAN",),
                movement="Encaminhado",
                movement_date="2026-10-06",
            )
        )
        pending = self.database.pending_occurrences()
        summary = render_executive_summary(pending)
        self.assertIn("**Mudou de unidade:** 1", summary)
        self.assertIn("GABINETE → DIPLAN", summary)
        self.assertIn("## TESTE", summary)
        marked = self.database.mark_communicated([item["id"] for item in pending["occurrences"]])
        self.assertEqual(marked, 2)
        again = render_executive_summary(self.database.pending_occurrences())
        self.assertIn("Nenhuma novidade desde o último resumo.", again)

    def test_frames_must_reference_configured_selectors(self) -> None:
        with self.assertRaises(ValueError):
            PlaywrightCollectorConfig.from_mapping(
                "SEI",
                {"selectors": {"units_open": ".u"}, "frames": {"unidades": "#ifr"}},
                self.temp_dir.name,
            )
        config = PlaywrightCollectorConfig.from_mapping(
            "SEI",
            {"selectors": {"units_open": ".u"}, "frames": {"units_open": ["#a", "#b"]}},
            self.temp_dir.name,
        )
        self.assertEqual(config.frames, {"units_open": ("#a", "#b")})


if __name__ == "__main__":
    unittest.main()

