from __future__ import annotations

from datetime import datetime
import json
from zoneinfo import ZoneInfo

from .domain import display_system


BRAZIL_TZ = ZoneInfo("America/Sao_Paulo")


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

