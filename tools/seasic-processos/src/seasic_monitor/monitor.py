from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .collectors import DemoCollector
from .database import MonitorDatabase
from .domain import CollectionStatus, Observation, canonical_system, utc_now
from .playwright_collector import PlaywrightCollector, PlaywrightCollectorConfig


def _edoc_collector(
    collector_settings: dict[str, Any],
    config_dir: str | Path,
    *,
    require_collection_selectors: bool = True,
) -> PlaywrightCollector:
    collector = PlaywrightCollector(
        PlaywrightCollectorConfig.from_mapping(
            "EDOC", collector_settings, config_dir
        )
    )
    collector.validate_config(
        require_collection_selectors=require_collection_selectors
    )
    return collector


async def prepare_edoc_session(
    collector_settings: dict[str, Any],
    config_dir: str | Path,
) -> bool:
    collector = _edoc_collector(
        collector_settings,
        config_dir,
        require_collection_selectors=False,
    )
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
                f"Não foi possível abrir ou validar o perfil do e-DOC "
                f"({type(exc).__name__})."
            ) from exc
    finally:
        await collector.close()


def _record_all_failures(
    database: MonitorDatabase,
    run_id: int,
    processes: list,
    status: CollectionStatus,
    code: str,
    message: str,
    stagnant_after_days: int,
) -> None:
    for process in processes:
        database.record_observation(
            run_id,
            Observation(
                system=process.system,
                number=process.number,
                collected_at=utc_now(),
                status=status,
                error_code=code,
                error_message=message,
            ),
            stagnant_after_days,
        )


def run_demo(database: MonitorDatabase, stagnant_after_days: int = 30) -> int:
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


async def run_edoc(
    database: MonitorDatabase,
    collector_settings: dict[str, Any],
    config_dir: str | Path,
    stagnant_after_days: int,
    min_interval_seconds: float,
    max_processes_per_run: int,
) -> int:
    if min_interval_seconds < 0:
        raise ValueError("O intervalo mínimo não pode ser negativo.")
    if max_processes_per_run < 1:
        raise ValueError("O teto de processos precisa ser maior que zero.")

    collector = _edoc_collector(collector_settings, config_dir)

    database.initialize()
    processes = [
        process
        for process in database.active_processes()
        if canonical_system(process.system) == "EDOC"
    ]
    if not processes:
        raise ValueError("Não há processos e-DOC ativos no Cadastro Mestre.")
    if len(processes) > max_processes_per_run:
        raise ValueError(
            f"Há {len(processes)} processos e-DOC ativos, acima do teto configurado "
            f"({max_processes_per_run}); a execução foi interrompida sem coleta."
        )

    run_id = database.create_execution(mode="LIVE_EDOC")
    succeeded = 0
    failed = 0
    notes = ""
    try:
        try:
            await collector.start()
        except Exception as exc:
            notes = f"Falha ao iniciar o navegador ({type(exc).__name__})."
            failed = len(processes)
            _record_all_failures(
                database,
                run_id,
                processes,
                CollectionStatus.UNAVAILABLE,
                "FALHA_INICIALIZACAO",
                notes,
                stagnant_after_days,
            )
        else:
            try:
                expired = await collector.is_session_expired()
                ready = await collector.is_ready()
            except Exception as exc:
                notes = f"Falha ao validar a sessão do navegador ({type(exc).__name__})."
                failed = len(processes)
                _record_all_failures(
                    database,
                    run_id,
                    processes,
                    CollectionStatus.UNAVAILABLE,
                    "FALHA_VALIDACAO_SESSAO",
                    notes,
                    stagnant_after_days,
                )
            else:
                if expired or not ready:
                    if expired:
                        status = CollectionStatus.SESSION_EXPIRED
                        code = "SESSAO_EXPIRADA"
                        message = "Sessão não autenticada; use login-edoc manualmente."
                    else:
                        status = CollectionStatus.EXTRACTION_ERROR
                        code = "TELA_CONSULTA_AUSENTE"
                        message = "Tela de consulta não apareceu; revise URL e seletores."
                    failed = len(processes)
                    _record_all_failures(
                        database,
                        run_id,
                        processes,
                        status,
                        code,
                        message,
                        stagnant_after_days,
                    )
                else:
                    for index, process in enumerate(processes):
                        observation = await collector.collect(process)
                        result = database.record_observation(
                            run_id,
                            observation,
                            stagnant_after_days,
                        )
                        if result["valid"]:
                            succeeded += 1
                        else:
                            failed += 1
                        if index + 1 < len(processes) and min_interval_seconds:
                            await asyncio.sleep(min_interval_seconds)
    finally:
        try:
            await collector.close()
        except Exception as exc:
            notes = notes or f"Falha ao encerrar o navegador ({type(exc).__name__})."

    database.finish_execution(
        run_id=run_id,
        total=len(processes),
        succeeded=succeeded,
        failed=failed,
        notes=notes,
    )
    return run_id

