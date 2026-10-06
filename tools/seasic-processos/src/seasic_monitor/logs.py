"""Log técnico em arquivo (seções 4.8 e 9 da arquitetura).

Registra apenas identificador do processo, resultado e código de erro. Nunca
grava andamento, unidade, interessado ou conteúdo de documento.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import logging
import os
from pathlib import Path
import re
from typing import Any

from .domain import BRAZIL_TZ


LOGGER_NAME = "seasic_monitor"
_FILE_PATTERN = re.compile(r"seasic-(\d{4}-\d{2}-\d{2})\.log")

logger = logging.getLogger(LOGGER_NAME)
# Sem configure_logging (testes, uso como biblioteca), nada vai para a tela.
logger.addHandler(logging.NullHandler())


class _BrasiliaFormatter(logging.Formatter):
    """Horário de Brasília, o mesmo usado no nome do arquivo e nos relatórios."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        moment = datetime.fromtimestamp(record.created, BRAZIL_TZ)
        return moment.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


@dataclass(frozen=True)
class LogSettings:
    directory: Path
    retention_days: int

    @classmethod
    def from_config(cls, config: dict[str, Any], base_dir: str | Path) -> LogSettings:
        logs = config.get("logs", {})
        directory = Path(str(logs.get("dir", "logs"))).expanduser()
        if not directory.is_absolute():
            directory = Path(base_dir) / directory
        retention = int(logs.get("retention_days", 180))
        if retention < 1:
            raise ValueError("logs.retention_days precisa ser maior que zero.")
        return cls(directory=directory, retention_days=retention)


def _restrict(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def configure_logging(settings: LogSettings, today: date | None = None) -> Path:
    """Liga o log do dia (um arquivo por data, horário de Brasília)."""
    today = today or datetime.now(BRAZIL_TZ).date()
    settings.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _restrict(settings.directory, 0o700)
    path = settings.directory / f"seasic-{today.isoformat()}.log"
    for handler in list(logger.handlers):
        if getattr(handler, "_seasic_file", False):
            logger.removeHandler(handler)
            handler.close()
    handler = logging.FileHandler(path, encoding="utf-8")
    handler._seasic_file = True  # type: ignore[attr-defined]
    handler.setFormatter(_BrasiliaFormatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    _restrict(path, 0o600)
    return path


def purge_old_logs(settings: LogSettings, today: date | None = None) -> list[Path]:
    """Apaga logs com data anterior ao prazo de retenção; devolve os removidos."""
    today = today or datetime.now(BRAZIL_TZ).date()
    limit = today - timedelta(days=settings.retention_days)
    removed = []
    if not settings.directory.is_dir():
        return removed
    for path in sorted(settings.directory.iterdir()):
        match = _FILE_PATTERN.fullmatch(path.name)
        if not match or not path.is_file():
            continue
        if date.fromisoformat(match.group(1)) < limit:
            path.unlink()
            removed.append(path)
    return removed
