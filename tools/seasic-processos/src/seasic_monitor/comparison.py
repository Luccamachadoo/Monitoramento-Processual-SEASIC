from __future__ import annotations

from dataclasses import dataclass
import hashlib

from .domain import (
    Observation,
    local_date,
    normalized_text,
    normalized_units,
    parse_movement_date,
)


@dataclass(frozen=True)
class CandidateOccurrence:
    kind: str
    description: str
    dedupe_key: str


def _key(*parts: str) -> str:
    raw = "\x1f".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def compare_observation(
    previous: dict[str, object] | None,
    current: Observation,
    current_snapshot_id: int,
    stagnant_after_days: int,
) -> list[CandidateOccurrence]:
    if not current.is_valid:
        return []

    occurrences: list[CandidateOccurrence] = []
    if previous is not None:
        previous_units = normalized_units(tuple(previous["units"] or ()))
        current_units = current.units
        if tuple(unit.casefold() for unit in previous_units) != tuple(
            unit.casefold() for unit in current_units
        ):
            old_label = ", ".join(previous_units) or "nenhuma unidade aberta"
            new_label = ", ".join(current_units) or "nenhuma unidade aberta"
            occurrences.append(
                CandidateOccurrence(
                    kind="MUDANCA_DE_UNIDADE",
                    description=f"Unidades abertas: {old_label} → {new_label}.",
                    dedupe_key=_key(
                        "MUDANCA_DE_UNIDADE",
                        str(previous["id"]),
                        str(current_snapshot_id),
                    ),
                )
            )

        movement_changed = (
            normalized_text(str(previous["last_movement"]))
            != normalized_text(current.last_movement)
            or str(previous["movement_date"])[:10] != current.movement_date[:10]
        )
        if movement_changed:
            occurrences.append(
                CandidateOccurrence(
                    kind="NOVO_ANDAMENTO",
                    description=(
                        f"Último andamento atualizado em {current.movement_date[:10]}."
                    ),
                    dedupe_key=_key(
                        "NOVO_ANDAMENTO",
                        str(previous["id"]),
                        str(current_snapshot_id),
                    ),
                )
            )

    days_without_movement = (
        local_date(current.collected_at).toordinal()
        - parse_movement_date(current.movement_date).toordinal()
    )
    if stagnant_after_days > 0 and days_without_movement >= stagnant_after_days:
        occurrences.append(
            CandidateOccurrence(
                kind="PARADO",
                description=(
                    f"Sem movimentação há {days_without_movement} dias "
                    f"(limite: {stagnant_after_days})."
                ),
                dedupe_key=_key(
                    "PARADO",
                    current.system,
                    current.number,
                    current.movement_date[:10],
                    normalized_text(current.last_movement),
                ),
            )
        )
    return occurrences


def failure_occurrence(
    current: Observation,
    previous_attempt_id: int | None,
    previous_attempt_status: str | None,
    current_snapshot_id: int,
) -> CandidateOccurrence | None:
    if current.is_valid:
        return None
    if previous_attempt_status == current.status.value:
        return None
    reason = current.error_message.strip() or current.error_code or current.status.value
    safe_reason = " ".join(reason.split())[:240]
    return CandidateOccurrence(
        kind="FALHA",
        description=f"{current.status.value}: {safe_reason}",
        dedupe_key=_key(
            "FALHA",
            current.system,
            current.number,
            str(previous_attempt_id or "primeira-tentativa"),
            current.status.value,
            str(current_snapshot_id),
        ),
    )

