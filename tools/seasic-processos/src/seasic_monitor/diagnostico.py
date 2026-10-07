"""Diagnóstico para a implantação no ambiente institucional.

Sem `--processo`: confere configuração, cadastro, extras instalados e pastas, sem
abrir navegador. Com `--sistema` e `--processo`: faz uma consulta passo a passo e
mostra qual seletor funcionou ou falhou. Nada é gravado no banco nem no log.
"""
from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import os
from pathlib import Path
from typing import Any

from .collectors import LiveCollectionDisabled
from .database import MonitorDatabase
from .domain import ProcessRecord, StagnationRule, canonical_system, display_system
from .logs import LogSettings
from .monitor import Collector, playwright_collector
from .playwright_collector import TraceStep
from .sheets import SheetsPublishError, SheetsSettings


SYSTEMS = ("EDOC", "SEI")
RECOMMENDED_SELECTORS = ("session_expired", "no_result")


@dataclass(frozen=True)
class Check:
    item: str
    ok: bool
    detail: str
    # Itens desativados de propósito não contam como falha.
    optional: bool = False


def _installed(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def _writable_dir(path: Path) -> tuple[bool, str]:
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    if not os.access(probe, os.W_OK):
        return False, f"sem permissão de escrita em {probe}"
    return True, str(path)


def static_checks(
    config: dict[str, Any],
    config_path: Path | None,
    config_dir: Path,
    database: MonitorDatabase,
) -> list[Check]:
    checks: list[Check] = []
    if config_path is None:
        checks.append(
            Check("config.toml", False, "não encontrado; copie config.example.toml para config.toml")
        )
    else:
        checks.append(Check("config.toml", True, str(config_path)))

    ok, detail = _writable_dir(database.path.parent)
    checks.append(Check("banco", ok, detail if not ok else str(database.path)))

    try:
        rule = StagnationRule.from_config(config.get("monitor", {}))
        kind = "dias úteis" if rule.business_days else "dias corridos"
        checks.append(
            Check(
                "processo parado",
                True,
                f"{rule.limit} {kind}; {len(rule.holidays)} feriado(s) extra(s) em holidays",
            )
        )
    except ValueError as exc:
        checks.append(Check("processo parado", False, str(exc)))

    active: dict[str, list[ProcessRecord]] = {system: [] for system in SYSTEMS}
    if database.path.exists():
        database.initialize()
        for process in database.active_processes():
            active.setdefault(process.system, []).append(process)
    total = sum(len(items) for items in active.values())
    checks.append(
        Check(
            "cadastro mestre",
            total > 0,
            ", ".join(f"{display_system(s)}: {len(active[s])}" for s in SYSTEMS)
            + " processo(s) ativo(s)"
            + ("" if total else " — importe o CSV com import-catalog"),
        )
    )

    any_enabled = False
    for system in SYSTEMS:
        settings = config.get("collectors", {}).get(system, {})
        label = f"coletor {display_system(system)}"
        if not settings.get("enabled", False):
            checks.append(Check(label, False, "desativado (enabled = false)", optional=True))
            continue
        any_enabled = True
        try:
            playwright_collector(system, settings, config_dir)
            checks.append(Check(label, True, "URL e seletores obrigatórios preenchidos"))
            selectors = settings.get("selectors", {})
            missing = [key for key in RECOMMENDED_SELECTORS if not str(selectors.get(key, "")).strip()]
            if missing:
                checks.append(
                    Check(
                        f"{label}: seletores recomendados",
                        False,
                        "faltam " + ", ".join(missing)
                        + " — sem eles, sessão expirada e processo não localizado viram erro genérico",
                        optional=True,
                    )
                )
        except (LiveCollectionDisabled, ValueError) as exc:
            checks.append(Check(label, False, str(exc)))
        canary = str(settings.get("canary", "")).strip()
        numbers = {process.number for process in active.get(system, [])}
        if not canary:
            checks.append(
                Check(f"{label}: processo de referência", False, "canary vazio (recomendado)", optional=True)
            )
        elif canary not in numbers:
            checks.append(
                Check(f"{label}: processo de referência", False, f"{canary} não está ativo no cadastro")
            )
        else:
            checks.append(Check(f"{label}: processo de referência", True, canary))
        if not active.get(system):
            checks.append(Check(f"{label}: cadastro", False, "nenhum processo ativo deste sistema"))

    if not any_enabled:
        checks.append(
            Check("coletores", False, "nenhum habilitado; a rotina não consultaria nada")
        )
    else:
        checks.append(
            Check(
                "extra browser (Playwright)",
                _installed("playwright"),
                "instalado" if _installed("playwright") else "ausente: python -m pip install -e .[browser]",
            )
        )

    sheets = SheetsSettings.from_config(config, config_dir)
    if not sheets.enabled:
        checks.append(Check("Google Sheets", False, "desativado ([sheets] enabled = false)", optional=True))
    else:
        try:
            sheets.validate()
            checks.append(Check("Google Sheets", True, f"aba \"{sheets.worksheet}\""))
        except SheetsPublishError as exc:
            checks.append(Check("Google Sheets", False, str(exc)))
        checks.append(
            Check(
                "extra sheets (gspread)",
                _installed("gspread"),
                "instalado" if _installed("gspread") else "ausente: python -m pip install -e .[sheets]",
            )
        )

    try:
        logs = LogSettings.from_config(config, config_dir)
        ok, detail = _writable_dir(logs.directory)
        checks.append(Check("logs", ok, f"{detail}; retenção de {logs.retention_days} dias" if ok else detail))
    except ValueError as exc:
        checks.append(Check("logs", False, str(exc)))
    return checks


async def trace_consultation(
    system: str,
    number: str,
    collector_settings: dict[str, Any],
    config_dir: str | Path,
    *,
    collector: Collector | None = None,
) -> tuple[list[TraceStep], Any]:
    """Consulta um processo registrando cada passo. Não grava no banco."""
    process = ProcessRecord(system=system, number=number)
    collector = collector or playwright_collector(process.system, collector_settings, config_dir)
    trace: list[TraceStep] = []
    try:
        try:
            await collector.start()
        except LiveCollectionDisabled:
            raise
        except Exception as exc:
            trace.append(TraceStep("navegador", False, type(exc).__name__))
            return trace, None
        trace.append(TraceStep("navegador", True, "aberto com o perfil salvo"))
        if await collector.is_session_expired():
            trace.append(TraceStep("sessão", False, "expirada; execute o comando login"))
            return trace, None
        observation = await collector.collect(process, trace=trace)
        return trace, observation
    finally:
        await collector.close()


def render_checks(checks: list[Check]) -> str:
    lines = ["Diagnóstico — configuração", ""]
    for check in checks:
        mark = "OK   " if check.ok else ("—    " if check.optional else "FALHA")
        lines.append(f"[{mark}] {check.item}: {check.detail}")
    return "\n".join(lines) + "\n"


def render_trace(system: str, number: str, trace: list[TraceStep], observation: Any) -> str:
    lines = [f"Diagnóstico — consulta {display_system(canonical_system(system))} {number}", ""]
    for step in trace:
        mark = "OK   " if step.ok else "FALHA"
        lines.append(f"[{mark}] {step.step}" + (f": {step.detail}" if step.detail else ""))
    lines.append("")
    if observation is None:
        lines.append("Resultado: a consulta não chegou a ser feita.")
    elif observation.is_valid:
        lines.append("Resultado: OK — os campos mínimos foram extraídos. Confira no sistema oficial.")
    else:
        lines.append(f"Resultado: {observation.status.value} ({observation.error_code}).")
    lines.append("Nada foi gravado no banco nem no log; os textos acima só aparecem nesta tela.")
    return "\n".join(lines) + "\n"


def checks_passed(checks: list[Check]) -> bool:
    return all(check.ok or check.optional for check in checks)
