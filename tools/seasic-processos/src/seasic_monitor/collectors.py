from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .domain import CollectionStatus, Observation, ProcessRecord


class LiveCollectionDisabled(RuntimeError):
    """Raised until live access is approved and configured in its target environment."""


class DemoCollector:
    """Returns synthetic data only; it never opens SEI or e-DOC."""

    def collect(self, process: ProcessRecord, now: datetime) -> Observation:
        collected_at = now.astimezone(timezone.utc).isoformat(timespec="seconds")
        if process.number == "DEMO-001":
            return Observation(
                system=process.system,
                number=process.number,
                collected_at=collected_at,
                status=CollectionStatus.OK,
                units=("GABINETE",),
                executive_sector="GABINETE",
                last_movement="Distribuído para análise",
                movement_date=now.date().isoformat(),
            )
        if process.number == "DEMO-002":
            old_date = (now.date() - timedelta(days=46)).isoformat()
            return Observation(
                system=process.system,
                number=process.number,
                collected_at=collected_at,
                status=CollectionStatus.OK,
                units=("UNIDADE TÉCNICA",),
                executive_sector="UNIDADE TÉCNICA",
                last_movement="Aguardando análise pela unidade",
                movement_date=old_date,
            )
        if process.number == "DEMO-003":
            return Observation(
                system=process.system,
                number=process.number,
                collected_at=collected_at,
                status=CollectionStatus.UNAVAILABLE,
                error_code="DEMO_INDISPONIVEL",
                error_message="Falha sintética para demonstrar o relatório de exceções.",
            )
        return Observation(
            system=process.system,
            number=process.number,
            collected_at=collected_at,
            status=CollectionStatus.EXTRACTION_ERROR,
            error_code="DEMO_SEM_CENARIO",
            error_message="Este processo não faz parte da base sintética de demonstração.",
        )


def collect_live(_process: ProcessRecord) -> Observation:
    raise LiveCollectionDisabled(
        "Coletores reais estão desativados. Configure-os e execute-os apenas em "
        "ambiente institucional autorizado, com conta própria e sem contornar MFA, "
        "CAPTCHA ou controles de acesso."
    )

