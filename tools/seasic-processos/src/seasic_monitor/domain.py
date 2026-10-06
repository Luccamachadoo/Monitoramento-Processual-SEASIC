from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
import hashlib
import json
import re


class CollectionStatus(StrEnum):
    OK = "OK"
    NOT_FOUND = "NAO_LOCALIZADO"
    SESSION_EXPIRED = "SESSAO_EXPIRADA"
    EXTRACTION_ERROR = "ERRO_EXTRACAO"
    UNAVAILABLE = "INDISPONIVEL"


class ComparisonStatus(StrEnum):
    BASELINE = "BASELINE"
    UNCHANGED = "SEM_MUDANCA"
    CHANGED = "ALTERACAO"
    INVALID = "INVALIDA"


def canonical_system(value: str) -> str:
    normalized = re.sub(r"[\s_-]+", "", value).upper()
    if normalized == "SEI":
        return "SEI"
    if normalized == "EDOC":
        return "EDOC"
    raise ValueError("Sistema deve ser SEI ou e-DOC.")


def display_system(value: str) -> str:
    return "e-DOC" if canonical_system(value) == "EDOC" else "SEI"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_movement_date(value: str) -> date:
    cleaned = value.strip()
    try:
        return date.fromisoformat(cleaned[:10])
    except ValueError as exc:
        raise ValueError("A data do último trâmite precisa estar no formato ISO.") from exc


def normalized_text(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def normalized_units(units: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    by_key: dict[str, str] = {}
    for unit in units:
        display = " ".join(unit.split())
        if display:
            by_key.setdefault(display.casefold(), display)
    return tuple(by_key[key] for key in sorted(by_key))


@dataclass(frozen=True)
class ProcessRecord:
    system: str
    number: str
    area: str = ""
    program: str = ""
    description: str = ""
    active: bool = True
    inactivation_reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "system", canonical_system(self.system))
        object.__setattr__(self, "number", self.number.strip())
        if not self.number:
            raise ValueError("O número do processo é obrigatório.")


@dataclass(frozen=True)
class Observation:
    system: str
    number: str
    collected_at: str
    status: CollectionStatus
    units: tuple[str, ...] = ()
    executive_sector: str = ""
    last_movement: str = ""
    movement_date: str = ""
    error_code: str = ""
    error_message: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "system", canonical_system(self.system))
        object.__setattr__(self, "number", self.number.strip())
        object.__setattr__(self, "units", normalized_units(self.units))
        if not self.number:
            raise ValueError("O número do processo é obrigatório.")
        try:
            parsed = datetime.fromisoformat(self.collected_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("O horário da coleta precisa estar no formato ISO.") from exc
        if parsed.tzinfo is None:
            raise ValueError("O horário da coleta precisa informar o fuso horário.")
        object.__setattr__(
            self,
            "collected_at",
            parsed.astimezone(timezone.utc).isoformat(timespec="microseconds"),
        )

    @property
    def is_valid(self) -> bool:
        if self.status != CollectionStatus.OK:
            return False
        if not self.last_movement.strip() or not self.movement_date.strip():
            return False
        try:
            parse_movement_date(self.movement_date)
        except ValueError:
            return False
        return True

    @property
    def content_hash(self) -> str:
        comparable = {
            "units": sorted(unit.casefold() for unit in self.units),
            "last_movement": normalized_text(self.last_movement),
            "movement_date": self.movement_date.strip()[:10],
        }
        encoded = json.dumps(comparable, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

