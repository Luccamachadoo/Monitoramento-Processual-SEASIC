from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from seasic_monitor.database import MonitorDatabase
from seasic_monitor.domain import CollectionStatus, Observation, ProcessRecord, StagnationRule
from seasic_monitor.sheets import (
    CREDENTIALS_ENV,
    HEADER_ROW,
    GspreadWriter,
    SheetsPublishError,
    SheetsSettings,
    build_sheet_values,
    publish,
)

try:
    import gspread
except ImportError:
    gspread = None


NOW = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
RULE = StagnationRule(limit=20, business_days=True)


class FakeWriter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[list[str]]]] = []

    def replace(self, worksheet: str, values: list[list[str]]) -> None:
        self.calls.append((worksheet, values))


class SheetValuesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = MonitorDatabase(Path(self.temp_dir.name) / "monitor.sqlite")
        self.database.initialize()
        self.database.upsert_process(
            ProcessRecord(system="EDOC", number="2439/2026", area="DSAN", description="=HYPERLINK(1)")
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def collect(self) -> None:
        run_id = self.database.create_execution(mode="TESTE")
        self.database.record_observation(
            run_id,
            Observation(
                system="EDOC",
                number="2439/2026",
                collected_at="2026-10-13T11:00:00+00:00",
                status=CollectionStatus.OK,
                units=("GSP",),
                last_movement="Para análise",
                movement_date="2026-10-09",
            ),
            RULE,
        )
        self.database.finish_execution(run_id, total=1, succeeded=1, failed=0)

    def test_stamp_has_fixed_position_and_rows_follow_header(self) -> None:
        self.collect()
        values = build_sheet_values(self.database.current_view(), RULE, NOW)
        self.assertTrue(values[1][0].startswith("Última execução bem-sucedida: "))
        self.assertEqual(values[2][0], "")  # sem alerta
        self.assertIn("13/10/2026 09:00", values[3][0])  # horário de Brasília
        header = values[HEADER_ROW - 1]
        self.assertIn("Dias úteis sem movimento", header)
        row = dict(zip(header, values[HEADER_ROW]))
        self.assertEqual(row["Processo"], "2439/2026")
        self.assertEqual(row["Dias úteis sem movimento"], "1")  # 12/10 é feriado
        # Texto com cara de fórmula segue como texto; a escrita usa RAW.
        self.assertEqual(row["Nota"], "=HYPERLINK(1)")
        self.assertEqual({len(line) for line in values}, {len(header)})

    def test_failed_last_execution_shows_alert(self) -> None:
        self.collect()
        failed = self.database.create_execution(mode="TESTE")
        self.database.finish_execution(
            failed, total=0, succeeded=0, failed=0, notes="Sessão expirada.", planned=1
        )
        values = build_sheet_values(self.database.current_view(), RULE, NOW)
        self.assertIn("Atenção", values[2][0])
        self.assertIn("FALHOU", values[2][0])

    def test_without_execution_says_none_instead_of_no_movement(self) -> None:
        values = build_sheet_values(self.database.current_view(), RULE, NOW)
        self.assertIn("nenhuma", values[1][0])
        row = dict(zip(values[HEADER_ROW - 1], values[HEADER_ROW]))
        self.assertEqual(row["Situação da consulta"], "Nunca consultado")
        self.assertEqual(row["Dias úteis sem movimento"], "")

    def test_publish_writes_only_the_robot_worksheet(self) -> None:
        self.collect()
        writer = FakeWriter()
        count = publish(self.database, writer, "Visão atual — robô", RULE, NOW)
        self.assertEqual(count, 1)
        self.assertEqual([name for name, _ in writer.calls], ["Visão atual — robô"])


class SheetsSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_disabled_or_incomplete_settings_are_refused(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(CREDENTIALS_ENV, None)
            with self.assertRaises(SheetsPublishError):
                SheetsSettings.from_config({}, self.base).validate()
            with self.assertRaisesRegex(SheetsPublishError, "spreadsheet_id"):
                SheetsSettings.from_config({"sheets": {"enabled": True}}, self.base).validate()
            settings = SheetsSettings.from_config(
                {"sheets": {"enabled": True, "spreadsheet_id": "x", "credentials_path": "nao.json"}},
                self.base,
            )
            self.assertEqual(settings.credentials_path, self.base / "nao.json")
            with self.assertRaisesRegex(SheetsPublishError, "não encontrado"):
                settings.validate()

    def test_environment_variable_overrides_credentials_path(self) -> None:
        credential = self.base / "conta.json"
        credential.write_text("{}")
        with mock.patch.dict(os.environ, {CREDENTIALS_ENV: str(credential)}):
            settings = SheetsSettings.from_config(
                {"sheets": {"enabled": True, "spreadsheet_id": "x"}}, self.base
            )
        settings.validate()
        self.assertEqual(settings.worksheet, "Visão atual — robô")


class FakeWorksheet:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def resize(self, rows=None, cols=None):
        self.calls.append(("resize", rows, cols))

    def clear(self):
        self.calls.append(("clear",))

    def update(self, values=None, range_name=None, value_input_option=None):
        self.calls.append(("update", range_name, value_input_option, values))

    def freeze(self, rows=None, cols=None):
        self.calls.append(("freeze", rows))


class GspreadWriterTests(unittest.TestCase):
    def make_writer(self, existing: bool) -> tuple[GspreadWriter, FakeWorksheet, list]:
        class NotFound(Exception):
            pass

        sheet = FakeWorksheet()
        created: list = []

        def worksheet(title):
            if not existing:
                raise NotFound(title)
            return sheet

        def add_worksheet(title, rows, cols):
            created.append((title, rows, cols))
            return sheet

        writer = GspreadWriter.__new__(GspreadWriter)
        writer._gspread = SimpleNamespace(exceptions=SimpleNamespace(WorksheetNotFound=NotFound))
        writer._spreadsheet = SimpleNamespace(worksheet=worksheet, add_worksheet=add_worksheet)
        return writer, sheet, created

    def test_replace_rewrites_whole_worksheet_as_raw_text(self) -> None:
        writer, sheet, created = self.make_writer(existing=True)
        values = [["a", "b"], ["=1+1", ""]]
        writer.replace("Aba", values)
        self.assertEqual(created, [])
        self.assertEqual(
            sheet.calls,
            [
                ("resize", 2, 2),
                ("clear",),
                ("update", "A1", "RAW", values),
                ("freeze", HEADER_ROW),
            ],
        )

    def test_missing_worksheet_is_created(self) -> None:
        writer, _, created = self.make_writer(existing=False)
        writer.replace("Aba", [["a"]])
        self.assertEqual(created, [("Aba", 1, 1)])

    def test_api_errors_become_safe_messages(self) -> None:
        writer, sheet, _ = self.make_writer(existing=True)
        response = SimpleNamespace(status_code=403, text="conteúdo sigiloso")
        error = RuntimeError("conteúdo sigiloso")
        error.response = response
        sheet.clear = mock.Mock(side_effect=error)
        with self.assertRaises(SheetsPublishError) as raised:
            writer.replace("Aba", [["a"]])
        self.assertIn("HTTP 403", str(raised.exception))
        self.assertNotIn("sigiloso", str(raised.exception))

    @unittest.skipIf(gspread is None, "extra sheets (gspread) não instalado")
    def test_invalid_credential_file_is_reported_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            credential = Path(folder) / "conta.json"
            credential.write_text('{"type": "service_account"}')
            settings = SheetsSettings(True, "planilha", "Aba", credential)
            with self.assertRaises(SheetsPublishError) as raised:
                GspreadWriter(settings)
        self.assertIn("abrir a planilha", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
