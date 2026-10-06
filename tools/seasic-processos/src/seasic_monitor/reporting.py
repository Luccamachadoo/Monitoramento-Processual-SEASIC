from __future__ import annotations

import csv
from datetime import date, datetime
import io
import json
from zoneinfo import ZoneInfo

from .domain import BRAZIL_TZ, display_system, parse_movement_date


def _local_time(value: str | None) -> str:
    if not value:
        return "—"
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed.astimezone(BRAZIL_TZ).strftime("%d/%m/%Y %H:%M")


def _process_label(row: dict) -> str:
    return f"{display_system(row['system'])} · {row['number']}"


def render_markdown(report: dict | None) -> str:
    if report is None:
        return (
            "# Monitoramento processual — SEASIC\n\n"
            "**Sem execução registrada.** A rotina ainda não rodou; isso não significa "
            "que os processos estejam sem movimentação.\n"
        )

    run = report["execution"]
    snapshots = report["snapshots"]
    occurrences = report["occurrences"]
    successful = [row for row in snapshots if row["valid"]]
    failed = [row for row in snapshots if not row["valid"]]
    unchanged = [row for row in successful if row["comparison_status"] == "SEM_MUDANCA"]
    baseline = [row for row in successful if row["comparison_status"] == "BASELINE"]
    changed = [row for row in successful if row["comparison_status"] == "ALTERACAO"]
    not_consulted = max(report["active_count"] - len(snapshots), 0)
    last_successful = report["last_successful_execution"]

    lines = [
        "# Monitoramento processual — SEASIC",
        "",
        f"- **Execução:** {run['id']} · {run['status']}",
        f"- **Início:** {_local_time(run['started_at'])}",
        f"- **Fim:** {_local_time(run['finished_at'])}",
        f"- **Última execução bem-sucedida:** "
        f"{_local_time(last_successful['finished_at']) if last_successful else 'nenhuma'}",
        f"- **Cadastro ativo:** {report['active_count']} processo(s)",
        f"- **Consultados:** {len(snapshots)} · **Êxito:** {len(successful)} · "
        f"**Falha:** {len(failed)} · **Não consultados:** {not_consulted}",
        "",
        "## Resultado da comparação",
        "",
        f"- Novos registros-base: {len(baseline)}",
        f"- Sem mudança nos campos monitorados: {len(unchanged)}",
        f"- Com alteração nos campos monitorados: {len(changed)}",
        "",
    ]
    if occurrences:
        lines.extend(["## Ocorrências", ""])
        for occurrence in occurrences:
            lines.append(
                f"- **{occurrence['type']}** · "
                f"{display_system(occurrence['system'])} {occurrence['number']} — "
                f"{occurrence['description']}"
            )
        lines.append("")
    else:
        lines.extend(["## Ocorrências", "", "Nenhuma ocorrência detectada nesta execução.", ""])

    if failed:
        lines.extend(["## Exceções de consulta", ""])
        for row in failed:
            details = row["error_message"] or row["status"]
            lines.append(f"- **{_process_label(row)}** — {details}")
        lines.append("")
    if not_consulted:
        lines.extend(
            [
                "## Atenção",
                "",
                f"{not_consulted} processo(s) ativo(s) não foram consultados nesta execução. "
                "Não interprete a ausência de coleta como ausência de movimentação.",
                "",
            ]
        )
    if run["notes"]:
        lines.extend(["## Observações técnicas", "", run["notes"], ""])
    return "\n".join(lines)


def render_status(report: dict | None) -> str:
    if report is None:
        return (
            "A rotina ainda não executou. Ausência de coleta não significa "
            "ausência de movimentação processual."
        )
    run = report["execution"]
    last_successful = report["last_successful_execution"]
    latest_failures = report["current_failures"]
    return "\n".join(
        [
            f"Última execução: {run['id']} · {run['status']} · "
            f"{_local_time(run['finished_at'])}",
            f"Última execução bem-sucedida: "
            f"{_local_time(last_successful['finished_at']) if last_successful else 'nenhuma'}",
            f"Processos ativos no cadastro: {report['active_count']}",
            f"Falhas mais recentes ainda não resolvidas: {len(latest_failures)}",
        ]
    )


def render_json(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)



OCCURRENCE_LABELS = {
    "MUDANCA_DE_UNIDADE": "Mudou de unidade",
    "NOVO_ANDAMENTO": "Novo andamento",
    "PARADO": "Parado",
    "FALHA": "Falha de consulta",
}
OCCURRENCE_ORDER = list(OCCURRENCE_LABELS)

CURRENT_VIEW_COLUMNS = (
    "Sistema",
    "Processo",
    "Área",
    "Programa",
    "Unidade atual",
    "Último trâmite",
    "Dias sem movimento",
    "Andamento",
    "Situação da consulta",
    "Consultado em",
    "Nota",
)


def _stamp_lines(data: dict) -> list[str]:
    last = data["last_execution"]
    last_successful = data["last_successful_execution"]
    lines = [
        f"- **Última execução bem-sucedida:** "
        + (
            f"{_local_time(last_successful['finished_at'])} · "
            f"{last_successful['succeeded']} processo(s) consultado(s) com êxito"
            if last_successful
            else "nenhuma — não há coleta válida; ausência de dado não é ausência de movimentação"
        ),
    ]
    if last and (not last_successful or last["id"] != last_successful["id"]):
        lines.append(
            f"- **Atenção:** a execução mais recente ({_local_time(last['started_at'])}) "
            f"terminou como {last['status']}"
            + (f" — {last['notes']}" if last["notes"] else "")
            + ". Os dados abaixo podem estar desatualizados."
        )
    elif last and last["status"] != "OK":
        lines.append(
            f"- **Execução parcial:** {last['failed']} falha(s)"
            + (f" — {last['notes']}" if last["notes"] else "")
            + "."
        )
    return lines


def current_view_rows(data: dict, today: date | None = None) -> list[dict[str, str]]:
    today = today or datetime.now(BRAZIL_TZ).date()
    rows = []
    for item in data["rows"]:
        process = item["process"]
        valid = item["valid"]
        attempt = item["attempt"]
        if attempt is None:
            situation = "Nunca consultado"
        elif attempt["valid"]:
            situation = "OK"
        else:
            situation = f"Falha atual: {attempt['status']}"
        days = ""
        if valid:
            days = str(
                (today - parse_movement_date(valid["movement_date"])).days
            )
        rows.append(
            {
                "Sistema": display_system(process["system"]),
                "Processo": (valid or {}).get("display_number") or process["number"],
                "Área": process["area"],
                "Programa": process["program"],
                "Unidade atual": "; ".join(valid["units"]) if valid else "",
                "Último trâmite": (
                    parse_movement_date(valid["movement_date"]).strftime("%d/%m/%Y")
                    if valid
                    else ""
                ),
                "Dias sem movimento": days,
                "Andamento": valid["last_movement"] if valid else "",
                "Situação da consulta": situation,
                "Consultado em": _local_time(valid["collected_at"]) if valid else "",
                "Nota": process["description"],
            }
        )
    return rows


def _md_cell(value: str) -> str:
    return " ".join(value.split()).replace("|", "\\|") or "—"


def render_current_view(data: dict, fmt: str = "markdown", today: date | None = None) -> str:
    rows = current_view_rows(data, today)
    if fmt == "csv":
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=CURRENT_VIEW_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        return buffer.getvalue()
    lines = ["# Visão atual — SEASIC", "", *_stamp_lines(data), ""]
    if not rows:
        lines.append("Nenhum processo ativo no Cadastro Mestre.")
        return "\n".join(lines) + "\n"
    columns = (
        "Processo",
        "Unidade atual",
        "Último trâmite",
        "Dias sem movimento",
        "Situação da consulta",
        "Nota",
    )
    lines.append("| " + " | ".join(columns) + " |")
    lines.append("|" + "---|" * len(columns))
    for row in rows:
        row = {**row, "Processo": f"{row['Sistema']} {row['Processo']}"}
        lines.append("| " + " | ".join(_md_cell(row[column]) for column in columns) + " |")
    return "\n".join(lines) + "\n"


def render_executive_summary(data: dict) -> str:
    lines = ["# Resumo executivo — Monitoramento processual SEASIC", "", *_stamp_lines(data), ""]
    occurrences = data["occurrences"]
    if not occurrences:
        lines.append("Nenhuma novidade desde o último resumo.")
        return "\n".join(lines) + "\n"

    counts = {kind: 0 for kind in OCCURRENCE_ORDER}
    for occurrence in occurrences:
        counts[occurrence["type"]] = counts.get(occurrence["type"], 0) + 1
    lines.append(
        " · ".join(
            f"**{OCCURRENCE_LABELS.get(kind, kind)}:** {count}"
            for kind, count in counts.items()
        )
    )
    lines.append("")

    groups: dict[tuple[str, str], list[dict]] = {}
    for occurrence in occurrences:
        groups.setdefault((occurrence["area"], occurrence["program"]), []).append(occurrence)
    for (area, program), items in groups.items():
        title = " · ".join(part for part in (area, program) if part) or "Sem área definida"
        lines.extend([f"## {title}", ""])
        items.sort(
            key=lambda item: (
                OCCURRENCE_ORDER.index(item["type"])
                if item["type"] in OCCURRENCE_ORDER
                else len(OCCURRENCE_ORDER),
                item["system"],
                item["number"],
            )
        )
        for item in items:
            subject = f" ({item['process_description']})" if item["process_description"] else ""
            lines.append(
                f"- **{OCCURRENCE_LABELS.get(item['type'], item['type'])}** — "
                f"{display_system(item['system'])} {item['number']}{subject}: "
                f"{item['description']}"
            )
        lines.append("")
    return "\n".join(lines)
