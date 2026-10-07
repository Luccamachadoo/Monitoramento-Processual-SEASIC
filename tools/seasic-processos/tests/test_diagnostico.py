from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from seasic_monitor.database import MonitorDatabase
from seasic_monitor.diagnostico import (
    checks_passed,
    render_trace,
    static_checks,
    trace_consultation,
)
from seasic_monitor.domain import ProcessRecord

try:
    import playwright  # noqa: F401
except ImportError:
    playwright = None

from test_playwright_local import EDOC


class StaticCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.database = MonitorDatabase(self.base / "processos.sqlite")
        self.database.initialize()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def by_item(self, config: dict) -> dict:
        checks = static_checks(config, self.base / "config.toml", self.base, self.database)
        return {check.item: check for check in checks}, checks_passed(checks)

    def test_nothing_enabled_is_reported(self) -> None:
        checks, passed = self.by_item({})
        self.assertFalse(passed)
        self.assertFalse(checks["coletores"].ok)
        self.assertFalse(checks["cadastro mestre"].ok)
        self.assertTrue(checks["coletor SEI"].optional)

    def test_enabled_collector_lists_missing_selectors_and_canary(self) -> None:
        self.database.upsert_process(ProcessRecord(system="EDOC", number="2439/2026"))
        checks, passed = self.by_item(
            {"collectors": {"EDOC": {"enabled": True, "canary": "1/2026"}}}
        )
        self.assertFalse(passed)
        self.assertIn("entry_url", checks["coletor e-DOC"].detail)
        self.assertIn("não está ativo", checks["coletor e-DOC: processo de referência"].detail)

    def test_complete_configuration_passes(self) -> None:
        self.database.upsert_process(ProcessRecord(system="EDOC", number="2439/2026"))
        checks, passed = self.by_item(
            {"collectors": {"EDOC": {**EDOC, "canary": "2439/2026"}}}
        )
        self.assertTrue(checks["coletor e-DOC"].ok, checks["coletor e-DOC"].detail)
        self.assertTrue(checks["coletor e-DOC: processo de referência"].ok)
        self.assertIn("extra browser (Playwright)", checks)
        if playwright is not None:
            self.assertTrue(passed, [c for c in checks.values() if not c.ok])

    def test_missing_recommended_selectors_are_flagged(self) -> None:
        self.database.upsert_process(ProcessRecord(system="EDOC", number="2439/2026"))
        checks, _ = self.by_item({"collectors": {"EDOC": EDOC}})
        flagged = checks["coletor e-DOC: seletores recomendados"]
        self.assertTrue(flagged.optional)
        self.assertIn("session_expired", flagged.detail)
        self.assertNotIn("no_result", flagged.detail)

    def test_invalid_stagnation_config_is_reported(self) -> None:
        checks, _ = self.by_item({"monitor": {"stagnant_day_kind": "semanas"}})
        self.assertFalse(checks["processo parado"].ok)

    def test_enabled_sheets_without_credentials_fails(self) -> None:
        checks, _ = self.by_item({"sheets": {"enabled": True}})
        self.assertFalse(checks["Google Sheets"].ok)
        self.assertIn("spreadsheet_id", checks["Google Sheets"].detail)


class ExpiredCollector:
    async def start(self): ...
    async def close(self): ...
    async def is_session_expired(self): return True
    async def is_ready(self): return False


class TraceTests(unittest.TestCase):
    def test_expired_session_stops_before_collecting(self) -> None:
        trace, observation = asyncio.run(
            trace_consultation("EDOC", "1/2026", {}, ".", collector=ExpiredCollector())
        )
        self.assertIsNone(observation)
        self.assertEqual([step.step for step in trace], ["navegador", "sessão"])
        self.assertIn("login", render_trace("EDOC", "1/2026", trace, observation))


@unittest.skipIf(playwright is None, "extra browser (Playwright) não instalado")
class LiveTraceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def trace(self, settings: dict, number: str):
        return asyncio.run(trace_consultation("EDOC", number, settings, self.temp_dir.name))

    def test_successful_trace_shows_each_selector(self) -> None:
        trace, observation = self.trace(EDOC, "2439/2026")
        self.assertTrue(observation.is_valid)
        steps = {step.step: step for step in trace}
        for key in ("entry_url", "process_input", "result_ready", "detail_ready", "units_open"):
            self.assertTrue(steps[key].ok, key)
        self.assertIn("→ 2026-10-06", steps["movement_date"].detail)
        # As duas linhas que contêm 2439/2026 aparecem; só uma corresponde.
        cells = steps["process_number_cell"]
        self.assertTrue(cells.ok)
        self.assertIn("2 linha(s)", cells.detail)
        self.assertIn("12439/2026-COMPR-SEASIC", cells.detail)
        self.assertIn("1 corresponde(m)", cells.detail)

    def test_ambiguous_rows_fail_at_the_number_cell_step(self) -> None:
        trace, observation = self.trace(EDOC, "3300/2026")
        self.assertEqual(observation.error_code, "RESULTADO_AMBIGUO")
        self.assertEqual([step.step for step in trace if not step.ok], ["process_number_cell"])

    def test_broken_selector_is_named(self) -> None:
        broken = {**EDOC, "selectors": {**EDOC["selectors"], "units_open": "li.nao-existe"}}
        trace, observation = self.trace(broken, "2439/2026")
        self.assertFalse(observation.is_valid)
        self.assertEqual(observation.error_code, "UNIDADES_AUSENTES")
        failed = [step.step for step in trace if not step.ok]
        self.assertEqual(failed, ["units_open"])
        text = render_trace("EDOC", "2439/2026", trace, observation)
        self.assertIn("[FALHA] units_open: nenhuma unidade encontrada", text)


if __name__ == "__main__":
    unittest.main()
