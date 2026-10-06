from __future__ import annotations

import contextlib
import io
from pathlib import Path
import tempfile
import unittest

from seasic_monitor import cli
from seasic_monitor.database import MonitorDatabase


class CatalogImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.temp_dir.name)
        self.db_path = self.base / "processos.sqlite"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def csv(self, name: str, text: str) -> Path:
        path = self.base / name
        path.write_text(text, encoding="utf-8")
        return path

    def run_cli(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        args = cli._parser().parse_args(
            ["--config", str(self.base / "sem-config.toml"), "--database", str(self.db_path), *argv]
        )
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli._run(args)
        return code, out.getvalue(), err.getvalue()

    def active(self) -> set[str]:
        return {p.number for p in MonitorDatabase(self.db_path).active_processes()}

    def test_absent_processes_are_reported_and_optionally_inactivated(self) -> None:
        full = self.csv("a.csv", "system,numero\nSEI,1/2026\ne-DOC,2/2026\n")
        partial = self.csv("b.csv", "system,numero\nSEI,1/2026\n")
        self.run_cli("import-catalog", str(full))

        _, _, err = self.run_cli("import-catalog", str(partial))
        self.assertIn("e-DOC 2/2026", err)
        self.assertEqual(self.active(), {"1/2026", "2/2026"})  # só avisa

        _, out, _ = self.run_cli("import-catalog", str(partial), "--inativar-ausentes")
        self.assertIn("inativado", out)
        self.assertEqual(self.active(), {"1/2026"})
        history = MonitorDatabase(self.db_path).process_history("EDOC", "2/2026")
        self.assertIn("Ausente do cadastro importado", history["process"]["inactivation_reason"])

    def test_invalid_csv_writes_nothing(self) -> None:
        bad = self.csv("bad.csv", "system,numero,ativo\nSEI,1/2026,sim\nSEI,2/2026,talvez\n")
        with self.assertRaisesRegex(ValueError, "Linha 3"):
            self.run_cli("import-catalog", str(bad))
        self.assertEqual(self.active(), set())

    def test_duplicate_process_is_rejected(self) -> None:
        duplicated = self.csv("dup.csv", "system,numero\nSEI,1/2026\nSEI, 1/2026 \n")
        with self.assertRaisesRegex(ValueError, "repetido"):
            self.run_cli("import-catalog", str(duplicated))
        self.assertEqual(self.active(), set())


if __name__ == "__main__":
    unittest.main()
