"""Rotina diária num único comando, para o Agendador de Tarefas.

Ordem: coleta de cada sistema habilitado → resumo e visão em arquivo → planilha
(se habilitada) → backup com rotação → limpeza de logs antigos. Cada etapa é
isolada: uma falha é registrada e as seguintes ainda rodam, para que o resumo e a
planilha mostrem o carimbo de "não houve coleta" em vez de ficarem desatualizados
em silêncio.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import re
from typing import Any, Callable

from .collectors import LiveCollectionDisabled
from .database import MonitorDatabase
from .domain import BRAZIL_TZ, StagnationRule, display_system
from .logs import LogSettings, logger, purge_old_logs
from .monitor import RunPolicy, run_collection
from .reporting import render_current_view, render_executive_summary
from .sheets import GspreadWriter, SheetsPublishError, SheetsSettings, SheetWriter, publish


SYSTEMS = ("EDOC", "SEI")
_BACKUP_PATTERN = re.compile(r"processos-(\d{4}-\d{2}-\d{2})\.sqlite")
EXPECTED_ERRORS = (OSError, ValueError, LiveCollectionDisabled, SheetsPublishError)


@dataclass(frozen=True)
class StepResult:
    step: str
    ok: bool
    message: str


@dataclass(frozen=True)
class RoutineSettings:
    systems: tuple[str, ...]
    reports_dir: Path
    mark_communicated: bool
    backup_dir: Path
    backup_keep: int

    @classmethod
    def from_config(cls, config: dict[str, Any], base_dir: str | Path) -> RoutineSettings:
        routine = config.get("rotina", {})
        collectors = config.get("collectors", {})

        def resolve(value: str) -> Path:
            path = Path(value).expanduser()
            return path if path.is_absolute() else Path(base_dir) / path

        keep = int(routine.get("backup_keep", 30))
        if keep < 1:
            raise ValueError("rotina.backup_keep precisa ser maior que zero.")
        return cls(
            systems=tuple(
                system for system in SYSTEMS if collectors.get(system, {}).get("enabled", False)
            ),
            reports_dir=resolve(str(routine.get("reports_dir", "data/relatorios"))),
            mark_communicated=bool(routine.get("mark_communicated", True)),
            backup_dir=resolve(str(routine.get("backup_dir", "data/backup"))),
            backup_keep=keep,
        )


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _write_private(path: Path, text: str, encoding: str = "utf-8") -> None:
    _private_dir(path.parent)
    path.write_text(text, encoding=encoding)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def rotate_backups(directory: Path, keep: int) -> list[Path]:
    """Mantém os `keep` backups diários mais recentes; devolve os removidos."""
    if not directory.is_dir():
        return []
    backups = sorted(
        (path for path in directory.iterdir() if _BACKUP_PATTERN.fullmatch(path.name)),
        key=lambda path: path.name,
        reverse=True,
    )
    removed = backups[keep:]
    for path in removed:
        path.unlink()
    return removed


def _default_collect(
    database: MonitorDatabase, config: dict[str, Any], config_dir: Path
) -> Callable[[str], int]:
    def collect(system: str) -> int:
        return asyncio.run(
            run_collection(
                database,
                system,
                config.get("collectors", {}).get(system, {}),
                config_dir,
                RunPolicy.from_config(config, system),
            )
        )

    return collect


def run_routine(
    database: MonitorDatabase,
    config: dict[str, Any],
    config_dir: str | Path,
    rule: StagnationRule,
    *,
    collect: Callable[[str], int] | None = None,
    sheet_writer: Callable[[SheetsSettings], SheetWriter] = GspreadWriter,
    now: datetime | None = None,
) -> list[StepResult]:
    config_dir = Path(config_dir)
    now = (now or datetime.now(BRAZIL_TZ)).astimezone(BRAZIL_TZ)
    settings = RoutineSettings.from_config(config, config_dir)
    collect = collect or _default_collect(database, config, config_dir)
    results: list[StepResult] = []

    def step(name: str, action: Callable[[], str]) -> None:
        try:
            message = action()
            results.append(StepResult(name, True, message))
            logger.info("rotina etapa=%s ok", name)
        except EXPECTED_ERRORS as exc:
            results.append(StepResult(name, False, str(exc)))
            logger.warning("rotina etapa=%s falhou erro=%s", name, type(exc).__name__)

    database.initialize()
    logger.info("rotina inicio sistemas=%s", ",".join(settings.systems) or "-")

    if not settings.systems:
        results.append(
            StepResult(
                "coleta",
                False,
                "Nenhum coletor habilitado no config.toml; nada foi consultado.",
            )
        )
    for system in settings.systems:
        def run_system(system: str = system) -> str:
            run_id = collect(system)
            execution = database.run_report(run_id)["execution"]
            if execution["status"] == "FALHOU":
                raise ValueError(
                    f"execução {run_id} FALHOU — {execution['notes'] or 'sem detalhes'}"
                )
            return (
                f"execução {run_id} {execution['status']}: "
                f"{execution['succeeded']} com êxito, {execution['failed']} falha(s)"
            )

        step(f"coleta {display_system(system)}", run_system)

    stamp = now.strftime("%Y-%m-%d-%H%M")

    def summary() -> str:
        pending = database.pending_occurrences()
        path = settings.reports_dir / f"resumo-{stamp}.md"
        _write_private(path, render_executive_summary(pending))
        marked = 0
        if settings.mark_communicated:
            marked = database.mark_communicated([item["id"] for item in pending["occurrences"]])
        return f"{path} ({len(pending['occurrences'])} ocorrência(s), {marked} marcada(s))"

    step("resumo", summary)

    def view() -> str:
        path = settings.reports_dir / f"visao-{stamp}.csv"
        # utf-8-sig para o Excel reconhecer acentos ao abrir o CSV.
        _write_private(
            path, render_current_view(database.current_view(), "csv", rule=rule), "utf-8-sig"
        )
        return str(path)

    step("visão", view)

    sheets = SheetsSettings.from_config(config, config_dir)
    if sheets.enabled:
        def sheet() -> str:
            sheets.validate()
            count = publish(database, sheet_writer(sheets), sheets.worksheet, rule)
            return f"aba \"{sheets.worksheet}\" com {count} processo(s)"

        step("planilha", sheet)

    def backup() -> str:
        _private_dir(settings.backup_dir)
        path = database.backup(settings.backup_dir / f"processos-{now.date().isoformat()}.sqlite")
        removed = rotate_backups(settings.backup_dir, settings.backup_keep)
        return f"{path} ({len(removed)} antigo(s) removido(s))"

    step("backup", backup)

    def logs() -> str:
        removed = purge_old_logs(LogSettings.from_config(config, config_dir), now.date())
        return f"{len(removed)} arquivo(s) de log antigo(s) removido(s)"

    step("logs", logs)
    logger.info(
        "rotina fim ok=%s falhas=%s",
        sum(result.ok for result in results),
        sum(not result.ok for result in results),
    )
    return results


def render_routine(results: list[StepResult]) -> str:
    lines = ["Rotina diária — SEASIC", ""]
    for result in results:
        mark = "OK   " if result.ok else "FALHA"
        lines.append(f"[{mark}] {result.step}: {result.message}")
    return "\n".join(lines) + "\n"
