from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any, Protocol

from .collectors import DemoCollector, LiveCollectionDisabled
from .database import MonitorDatabase
from .logs import logger
from .domain import (
    BRAZIL_TZ,
    CollectionStatus,
    Observation,
    ProcessRecord,
    StagnationRule,
    canonical_system,
    display_system,
)
from .playwright_collector import PlaywrightCollector, PlaywrightCollectorConfig


# Falhas que indicam problema técnico (layout, rede, sistema fora do ar).
# "Não localizado" é resposta legítima do sistema e não conta para a interrupção.
TECHNICAL_FAILURES = {CollectionStatus.EXTRACTION_ERROR, CollectionStatus.UNAVAILABLE}


class Collector(Protocol):
    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def is_session_expired(self) -> bool: ...
    async def is_ready(self) -> bool: ...
    async def collect(self, process: ProcessRecord) -> Observation: ...


@dataclass(frozen=True)
class RunPolicy:
    stagnation: StagnationRule = field(default_factory=StagnationRule)
    min_interval_seconds: float = 5
    max_processes_per_run: int = 500
    max_processes_per_day: int = 500
    max_consecutive_failures: int = 3
    allowed_hours: str = ""
    canary: str = ""

    @classmethod
    def from_config(cls, config: dict[str, Any], system: str) -> RunPolicy:
        monitor = config.get("monitor", {})
        collector = config.get("collectors", {}).get(canonical_system(system), {})
        return cls(
            stagnation=StagnationRule.from_config(monitor),
            min_interval_seconds=float(monitor.get("min_interval_seconds", 5)),
            max_processes_per_run=int(monitor.get("max_processes_per_run", 500)),
            max_processes_per_day=int(monitor.get("max_processes_per_day", 500)),
            max_consecutive_failures=int(monitor.get("max_consecutive_failures", 3)),
            allowed_hours=str(monitor.get("allowed_hours", "")).strip(),
            canary=str(collector.get("canary", "")).strip(),
        )

    def validate(self) -> None:
        self.stagnation.validate()
        if self.min_interval_seconds < 0:
            raise ValueError("O intervalo mínimo não pode ser negativo.")
        if self.max_processes_per_run < 1:
            raise ValueError("O teto de processos precisa ser maior que zero.")
        if self.max_processes_per_day < 1:
            raise ValueError("O teto diário de processos precisa ser maior que zero.")
        if self.max_consecutive_failures < 1:
            raise ValueError("O limite de falhas consecutivas precisa ser maior que zero.")
        self.window()

    def window(self) -> tuple[time, time] | None:
        if not self.allowed_hours:
            return None
        try:
            start_text, end_text = self.allowed_hours.split("-")
            return time.fromisoformat(start_text.strip()), time.fromisoformat(
                end_text.strip()
            )
        except ValueError as exc:
            raise ValueError(
                'allowed_hours deve seguir o formato "HH:MM-HH:MM".'
            ) from exc


def _within_window(window: tuple[time, time], moment: time) -> bool:
    start, end = window
    if start <= end:
        return start <= moment <= end
    return moment >= start or moment <= end  # janela que atravessa a meia-noite


def playwright_collector(
    system: str,
    collector_settings: dict[str, Any],
    config_dir: str | Path,
    *,
    require_collection_selectors: bool = True,
) -> PlaywrightCollector:
    collector = PlaywrightCollector(
        PlaywrightCollectorConfig.from_mapping(system, collector_settings, config_dir)
    )
    collector.validate_config(
        require_collection_selectors=require_collection_selectors
    )
    return collector


async def prepare_session(
    system: str,
    collector_settings: dict[str, Any],
    config_dir: str | Path,
) -> bool:
    collector = playwright_collector(
        system,
        collector_settings,
        config_dir,
        require_collection_selectors=False,
    )
    # O login é sempre feito pelo operador numa janela visível.
    collector.config = replace(collector.config, headless=False)
    try:
        try:
            await collector.start(require_collection_selectors=False)
            return await collector.wait_for_manual_login(
                require_search_ready=bool(
                    collector.config.selectors.get("process_input")
                )
            )
        except LiveCollectionDisabled:
            raise
        except Exception as exc:
            raise LiveCollectionDisabled(
                f"Não foi possível abrir ou validar o perfil do "
                f"{display_system(system)} ({type(exc).__name__})."
            ) from exc
    finally:
        await collector.close()


async def consult_one(
    system: str,
    number: str,
    collector_settings: dict[str, Any],
    config_dir: str | Path,
    *,
    collector: Collector | None = None,
) -> Observation:
    """Consulta um único processo e devolve a fotografia, sem gravar no banco."""
    process = ProcessRecord(system=system, number=number)
    collector = collector or playwright_collector(
        process.system, collector_settings, config_dir
    )
    try:
        try:
            await collector.start()
            ready = not await collector.is_session_expired() and await collector.is_ready()
        except LiveCollectionDisabled:
            raise
        except Exception as exc:
            raise LiveCollectionDisabled(
                f"Falha ao iniciar ou validar o navegador ({type(exc).__name__})."
            ) from exc
        if not ready:
            raise LiveCollectionDisabled(
                "A sessão não está pronta para consulta; execute o comando login."
            )
        return await collector.collect(process)
    finally:
        await collector.close()


def run_demo(
    database: MonitorDatabase, stagnant_after_days: StagnationRule | int = 30
) -> int:
    database.initialize()
    processes = database.demo_processes()
    now = datetime.now(timezone.utc)
    run_id = database.create_execution(mode="DEMO_SINTETICA", started_at=now.isoformat())
    collector = DemoCollector()
    succeeded = 0
    failed = 0
    for process in processes:
        observation = collector.collect(process, now)
        outcome = database.record_observation(
            run_id=run_id,
            observation=observation,
            stagnant_after_days=stagnant_after_days,
        )
        if outcome["valid"]:
            succeeded += 1
        else:
            failed += 1
    database.finish_execution(
        run_id=run_id,
        total=len(processes),
        succeeded=succeeded,
        failed=failed,
    )
    return run_id


def _ordered_with_canary(
    processes: list[ProcessRecord], canary: str
) -> list[ProcessRecord]:
    if not canary:
        return processes
    for index, process in enumerate(processes):
        if process.number == canary:
            return [process, *processes[:index], *processes[index + 1 :]]
    raise ValueError(
        f"O processo de referência {canary} não está ativo no Cadastro Mestre."
    )


async def run_collection(
    database: MonitorDatabase,
    system: str,
    collector_settings: dict[str, Any],
    config_dir: str | Path,
    policy: RunPolicy,
    *,
    collector: Collector | None = None,
    now: datetime | None = None,
) -> int:
    system = canonical_system(system)
    label = display_system(system)
    policy.validate()
    collector = collector or playwright_collector(system, collector_settings, config_dir)

    moment = (now or datetime.now(timezone.utc)).astimezone(BRAZIL_TZ)
    window = policy.window()
    if window and not _within_window(window, moment.time()):
        raise ValueError(
            f"Fora da janela de execução configurada ({policy.allowed_hours}); "
            "nenhuma consulta foi feita."
        )

    database.initialize()
    processes = [
        process
        for process in database.active_processes()
        if process.system == system
    ]
    if not processes:
        raise ValueError(f"Não há processos {label} ativos no Cadastro Mestre.")
    if len(processes) > policy.max_processes_per_run:
        raise ValueError(
            f"Há {len(processes)} processos {label} ativos, acima do teto por execução "
            f"({policy.max_processes_per_run}); a execução foi interrompida sem coleta."
        )
    day_start = (
        datetime.combine(moment.date(), time(0), BRAZIL_TZ)
        .astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
    )
    already_today = database.count_snapshots_since(system, day_start)
    if already_today + len(processes) > policy.max_processes_per_day:
        raise ValueError(
            f"O teto diário do {label} ({policy.max_processes_per_day}) seria excedido: "
            f"{already_today} consulta(s) hoje + {len(processes)} nesta execução. "
            "Nenhuma consulta foi feita."
        )
    processes = _ordered_with_canary(processes, policy.canary)

    run_id = database.create_execution(mode=f"LIVE_{system}", scope=system)
    logger.info("execucao=%s sistema=%s inicio planejados=%s", run_id, system, len(processes))
    succeeded = 0
    failed = 0
    consecutive_failures = 0
    notes = ""
    try:
        try:
            await collector.start()
            expired = await collector.is_session_expired()
            ready = not expired and await collector.is_ready()
        except LiveCollectionDisabled as exc:
            notes = str(exc)
        except Exception as exc:
            notes = f"Falha ao iniciar ou validar o navegador ({type(exc).__name__})."
        else:
            if expired:
                notes = "Sessão não autenticada; execute o comando login manualmente."
            elif not ready:
                notes = "Tela de consulta não apareceu; revise URL e seletores."
            else:
                for index, process in enumerate(processes):
                    observation = await collector.collect(process)
                    result = database.record_observation(
                        run_id,
                        observation,
                        policy.stagnation,
                    )
                    # Só identificador, resultado e código de erro (seção 9).
                    logger.info(
                        "execucao=%s processo=%s:%s status=%s comparacao=%s erro=%s "
                        "ocorrencias=%s",
                        run_id,
                        system,
                        process.number,
                        result["status"],
                        result["comparison_status"],
                        observation.error_code or "-",
                        result["occurrences_added"],
                    )
                    if result["valid"]:
                        succeeded += 1
                        consecutive_failures = 0
                    else:
                        failed += 1
                        if observation.status in TECHNICAL_FAILURES:
                            consecutive_failures += 1
                    remaining = len(processes) - index - 1
                    if observation.status == CollectionStatus.SESSION_EXPIRED:
                        notes = (
                            f"Sessão expirou durante a rodada; {remaining} processo(s) "
                            "não consultado(s). Execute o comando login."
                        )
                        break
                    if index == 0 and policy.canary and not result["valid"]:
                        notes = (
                            f"O processo de referência {policy.canary} falhou "
                            f"({result['status']}); a rodada foi interrompida para "
                            "evitar falhas em massa. Verifique layout e seletores."
                        )
                        break
                    if consecutive_failures >= policy.max_consecutive_failures:
                        notes = (
                            f"{consecutive_failures} falhas técnicas seguidas; rodada "
                            f"interrompida com {remaining} processo(s) não consultado(s)."
                        )
                        break
                    if remaining and policy.min_interval_seconds:
                        await asyncio.sleep(policy.min_interval_seconds)
    except BaseException:
        notes = notes or "Execução interrompida antes do fim."
        raise
    finally:
        try:
            await collector.close()
        except Exception as exc:
            notes = notes or f"Falha ao encerrar o navegador ({type(exc).__name__})."
        database.finish_execution(
            run_id=run_id,
            total=succeeded + failed,
            succeeded=succeeded,
            failed=failed,
            notes=notes,
            planned=len(processes),
        )
        logger.info(
            "execucao=%s sistema=%s fim exito=%s falhas=%s planejados=%s obs=%s",
            run_id,
            system,
            succeeded,
            failed,
            len(processes),
            notes or "-",
        )
    return run_id

