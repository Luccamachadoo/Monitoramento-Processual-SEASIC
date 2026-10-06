from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import StrEnum
import hashlib
import json
import re
from zoneinfo import ZoneInfo


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


BRAZIL_TZ = ZoneInfo("America/Sao_Paulo")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_movement_date(value: str) -> date:
    cleaned = value.strip()
    try:
        return date.fromisoformat(cleaned[:10])
    except ValueError as exc:
        raise ValueError("A data do último trâmite precisa estar no formato ISO.") from exc


def local_date(value: str) -> date:
    """Data civil no fuso de Brasília para um instante ISO (com ou sem fuso)."""
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(BRAZIL_TZ).date()


def normalized_text(value: str | None) -> str:
    return " ".join((value or "").split()).casefold()


def normalized_units(units: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    by_key: dict[str, str] = {}
    for unit in units:
        display = " ".join(unit.split())
        if display:
            by_key.setdefault(display.casefold(), display)
    return tuple(by_key[key] for key in sorted(by_key))


_SHORT_NUMBER = re.compile(r"\d+/\d{4}")


def number_matches(registered: str, displayed: str) -> bool:
    """Diz se o número exibido pelo sistema corresponde ao número cadastrado.

    O e-DOC exibe números com sufixo de classificação (ex.: 2439/2026-COMPR-SEASIC).
    Aceita igualdade exata ou, quando o cadastro traz só NNNN/AAAA, esse prefixo
    seguido de hífen. Nunca aceita prefixo parcial como 243/2026 para 2439/2026.
    """
    expected = " ".join(registered.split()).casefold()
    actual = " ".join(displayed.split()).casefold()
    if not expected or not actual:
        return False
    if actual == expected:
        return True
    return bool(_SHORT_NUMBER.fullmatch(expected)) and actual.startswith(expected + "-")


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
    display_number: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "system", canonical_system(self.system))
        object.__setattr__(self, "number", self.number.strip())
        object.__setattr__(self, "display_number", " ".join(self.display_number.split()))
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

