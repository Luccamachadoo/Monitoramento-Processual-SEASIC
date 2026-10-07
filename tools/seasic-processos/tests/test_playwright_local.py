"""Coletor Playwright contra páginas sintéticas locais (tests/fixtures/paginas).

Nunca acessa SEI/e-DOC. É pulado quando o extra `browser` não está instalado.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
import unittest

from seasic_monitor.domain import CollectionStatus, ProcessRecord
from seasic_monitor.playwright_collector import PlaywrightCollector, PlaywrightCollectorConfig

try:
    import playwright  # noqa: F401
except ImportError:
    playwright = None


PAGES = Path(__file__).parent / "fixtures" / "paginas"

EDOC = {
    "enabled": True,
    "headless": True,
    "entry_url": (PAGES / "edoc.html").as_uri(),
    "selectors": {
        "process_input": "#numero",
        "search_button": "#pesquisar",
        # Casa com várias linhas: o coletor precisa esperar só a primeira.
        "result_ready": "#res tbody tr:visible, #vazio:visible",
        "no_result": "#vazio",
        "result_row": "#res tbody tr:visible",
        "process_number_cell": "td.num",
        "detail_link": "a.det",
        "detail_ready": "#detalhe",
        "units_open": "li.und",
        "last_movement": "#and",
        "movement_date": "#dt",
    },
}

SEI = {
    "enabled": True,
    "headless": True,
    "entry_url": (PAGES / "sei.html").as_uri(),
    "selectors": {
        "process_input": "#txtPesquisa",
        "search_button": "#btn",
        "result_ready": "#ok, #vazio:visible",
        "no_result": "#vazio",
        "units_open": "#unidades a.u",
        "last_movement": "tr.andamento td.desc",
        "movement_date": "tr.andamento td.data",
    },
    "frames": {
        "units_open": "#ifrVisualizacao",
        "last_movement": "#ifrVisualizacao",
        "movement_date": "#ifrVisualizacao",
    },
}


async def _collect(system: str, settings: dict, numbers: list[str], base: str) -> list:
    collector = PlaywrightCollector(
        PlaywrightCollectorConfig.from_mapping(system, settings, base)
    )
    await collector.start()
    try:
        return [
            await collector.collect(ProcessRecord(system=system, number=number))
            for number in numbers
        ]
    finally:
        await collector.close()


@unittest.skipIf(playwright is None, "extra browser (Playwright) não instalado")
class LocalPagesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_edoc_search_and_detail(self) -> None:
        found, ambiguous, missing = asyncio.run(
            _collect("EDOC", EDOC, ["2439/2026", "3300/2026", "9999/2026"], self.temp_dir.name)
        )
        self.assertTrue(found.is_valid)
        self.assertEqual(found.units, ("DIPLAN", "GSP"))
        self.assertEqual(found.movement_date, "2026-10-06")
        # 12439/2026 também aparece na lista e não pode ser confundido.
        self.assertEqual(found.display_number, "2439/2026-COMPR-SEASIC")
        self.assertEqual(ambiguous.error_code, "RESULTADO_AMBIGUO")
        self.assertEqual(missing.status, CollectionStatus.NOT_FOUND)

    def test_sei_fields_inside_iframe(self) -> None:
        (observation,) = asyncio.run(_collect("SEI", SEI, ["2439/2026"], self.temp_dir.name))
        self.assertTrue(observation.is_valid)
        self.assertEqual(observation.units, ("SEASIC-GAB",))
        self.assertEqual(observation.last_movement, "Processo recebido na unidade")
        self.assertEqual(observation.movement_date, "2026-09-01")

    def test_previous_result_is_not_reused_for_next_process(self) -> None:
        first, second, missing = asyncio.run(
            _collect(
                "SEI", SEI, ["2439/2026", "5555/2026", "9999/2026"], self.temp_dir.name
            )
        )
        self.assertEqual(first.units, ("SEASIC-GAB",))
        # Sem recarregar a busca, o resultado de 2439/2026 ainda estaria na tela
        # e seria gravado como se fosse de 5555/2026.
        self.assertEqual(second.units, ("DIPLAN",))
        self.assertEqual(second.movement_date, "2026-10-02")
        self.assertEqual(missing.status, CollectionStatus.NOT_FOUND)

if __name__ == "__main__":
    unittest.main()
