"""Publicação da visão atual numa aba do Google Sheets (Fase 5).

O SQLite continua sendo a fonte do histórico; a planilha é só a camada de
visualização do Gabinete. O robô reescreve apenas a aba que gerencia e nunca
toca nas demais abas da planilha.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
from typing import Any, Protocol

from .database import MonitorDatabase
from .domain import BRAZIL_TZ, StagnationRule
from .reporting import current_view_columns, current_view_rows, stamp_entries


DEFAULT_WORKSHEET = "Visão atual — robô"
CREDENTIALS_ENV = "SEASIC_SHEETS_CREDENTIALS"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
HEADER_ROW = 6  # linha (1-based) do cabeçalho da tabela; o carimbo fica sempre em A2


class SheetsPublishError(RuntimeError):
    """Falha de configuração ou da API, com mensagem segura para o operador."""


class SheetWriter(Protocol):
    def replace(self, worksheet: str, values: list[list[str]]) -> None: ...


@dataclass(frozen=True)
class SheetsSettings:
    enabled: bool
    spreadsheet_id: str
    worksheet: str
    credentials_path: Path | None

    @classmethod
    def from_config(cls, config: dict[str, Any], base_dir: str | Path) -> SheetsSettings:
        sheets = config.get("sheets", {})
        raw_path = os.environ.get(CREDENTIALS_ENV) or str(sheets.get("credentials_path", "")).strip()
        path = Path(raw_path).expanduser() if raw_path else None
        if path is not None and not path.is_absolute():
            path = Path(base_dir) / path
        return cls(
            enabled=bool(sheets.get("enabled", False)),
            spreadsheet_id=str(sheets.get("spreadsheet_id", "")).strip(),
            worksheet=str(sheets.get("worksheet", "")).strip() or DEFAULT_WORKSHEET,
            credentials_path=path,
        )

    def validate(self) -> None:
        if not self.enabled:
            raise SheetsPublishError("Publicação no Google Sheets desativada ([sheets] enabled).")
        missing = []
        if not self.spreadsheet_id:
            missing.append("spreadsheet_id")
        if self.credentials_path is None:
            missing.append(f"credentials_path ou {CREDENTIALS_ENV}")
        if missing:
            raise SheetsPublishError(
                "Configuração incompleta para o Google Sheets: " + ", ".join(missing) + "."
            )
        if not self.credentials_path.is_file():
            raise SheetsPublishError(
                "Arquivo de credencial da conta de serviço não encontrado no caminho configurado."
            )


def build_sheet_values(
    view: dict,
    rule: StagnationRule | None = None,
    now: datetime | None = None,
) -> list[list[str]]:
    """Monta a aba: título, carimbo em posição fixa, alerta, cabeçalho e processos."""
    now = (now or datetime.now(BRAZIL_TZ)).astimezone(BRAZIL_TZ)
    columns = current_view_columns(rule)
    stamp = stamp_entries(view)
    alert = next((f"{label}: {text}" for label, text in stamp[1:]), "")
    values: list[list[str]] = [
        ["Monitoramento processual SEASIC — visão atual (aba gerada pelo robô; não editar)"],
        [f"{stamp[0][0]}: {stamp[0][1]}"],
        [alert],
        [f"Publicado em: {now.strftime('%d/%m/%Y %H:%M')}"],
        [""],
        list(columns),
    ]
    rows = current_view_rows(view, now.date(), rule)
    if rows:
        values.extend([row[column] for column in columns] for row in rows)
    else:
        values.append(["Nenhum processo ativo no Cadastro Mestre."])
    width = len(columns)
    return [row + [""] * (width - len(row)) for row in values]


class GspreadWriter:
    """Escreve com uma conta de serviço; exige o extra opcional `sheets` (gspread)."""

    def __init__(self, settings: SheetsSettings) -> None:
        try:
            import gspread
        except ImportError as exc:
            raise SheetsPublishError(
                "gspread não está instalado. Instale o extra sheets no ambiente aprovado: "
                "python3 -m pip install -e '.[sheets]'"
            ) from exc
        self._gspread = gspread
        try:
            # Só o escopo de planilhas: a conta de serviço não precisa ver o Drive.
            client = gspread.service_account(
                filename=str(settings.credentials_path), scopes=[SHEETS_SCOPE]
            )
            self._spreadsheet = client.open_by_key(settings.spreadsheet_id)
        except Exception as exc:
            raise SheetsPublishError(_safe_error("abrir a planilha", exc)) from exc

    def replace(self, worksheet: str, values: list[list[str]]) -> None:
        width = max(len(row) for row in values)
        try:
            try:
                sheet = self._spreadsheet.worksheet(worksheet)
            except self._gspread.exceptions.WorksheetNotFound:
                sheet = self._spreadsheet.add_worksheet(
                    title=worksheet, rows=len(values), cols=width
                )
            sheet.resize(rows=len(values), cols=width)
            sheet.clear()
            # RAW: texto vindo dos sistemas nunca é interpretado como fórmula.
            sheet.update(range_name="A1", values=values, value_input_option="RAW")
            sheet.freeze(rows=HEADER_ROW)
        except Exception as exc:
            raise SheetsPublishError(_safe_error("atualizar a aba", exc)) from exc


def _safe_error(action: str, exc: Exception) -> str:
    # Não repassa o corpo da resposta da API, que pode conter dados da planilha.
    status = getattr(getattr(exc, "response", None), "status_code", None)
    detail = f"HTTP {status}" if status else type(exc).__name__
    return f"Não foi possível {action} no Google Sheets ({detail})."


def publish(
    database: MonitorDatabase,
    writer: SheetWriter,
    worksheet: str,
    rule: StagnationRule | None = None,
    now: datetime | None = None,
) -> int:
    """Publica a visão atual e devolve quantos processos foram escritos."""
    view = database.current_view()
    writer.replace(worksheet, build_sheet_values(view, rule, now))
    return len(view["rows"])
