from __future__ import annotations

import argparse
import asyncio
import csv
from dataclasses import asdict
import os
from pathlib import Path
import sys
import tomllib

from .collectors import LiveCollectionDisabled
from .database import MonitorDatabase
from .domain import ProcessRecord, canonical_system
from .monitor import RunPolicy, consult_one, prepare_session, run_collection, run_demo
from .reporting import render_json, render_markdown, render_status


PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="seasic-monitor",
        description="Ferramenta local, somente leitura, para monitoramento processual.",
    )
    parser.add_argument(
        "--database",
        help="Caminho do SQLite. Deve ficar em armazenamento institucional restrito.",
    )
    parser.add_argument(
        "--config",
        help="Arquivo TOML de configuração (padrão: config.toml ao lado deste projeto).",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Cria as tabelas do banco local.")
    commands.add_parser("demo", help="Executa uma rodada apenas com dados sintéticos.")
    commands.add_parser("status", help="Mostra o carimbo da execução e falhas atuais.")
    report = commands.add_parser("report", help="Gera o resumo de uma execução.")
    report.add_argument("--run-id", type=int, help="ID da execução; padrão: a mais recente.")
    report.add_argument(
        "--format", choices=("markdown", "json"), default="markdown"
    )
    history = commands.add_parser("history", help="Mostra histórico de um processo cadastrado.")
    history.add_argument("system", help="SEI ou e-DOC")
    history.add_argument("number", help="Número cadastrado do processo")
    catalog = commands.add_parser("import-catalog", help="Importa ou atualiza um CSV de cadastro.")
    catalog.add_argument("csv_file", type=Path)
    backup = commands.add_parser("backup", help="Cria uma cópia SQLite consistente.")
    backup.add_argument("destination", type=Path)
    login = commands.add_parser(
        "login",
        help="Abre o perfil local do sistema para autenticação manual pelo operador.",
    )
    login.add_argument("--sistema", required=True, help="SEI ou e-DOC")
    run = commands.add_parser(
        "run",
        help="Consulta em série os processos ativos de um sistema (exige configuração institucional).",
    )
    run.add_argument("--sistema", required=True, help="SEI ou e-DOC")
    consult = commands.add_parser(
        "consultar",
        help="Consulta um único processo e mostra o resultado, sem gravar no banco.",
    )
    consult.add_argument("system", help="SEI ou e-DOC")
    consult.add_argument("number", help="Número do processo")
    commands.add_parser("login-edoc", help="Atalho para: login --sistema e-DOC.")
    commands.add_parser("run-edoc", help="Atalho para: run --sistema e-DOC.")
    return parser


def _load_config(config_argument: str | None) -> tuple[dict, Path]:
    config_path = (
        Path(config_argument).expanduser()
        if config_argument
        else PACKAGE_ROOT / "config.toml"
    )
    if not config_path.exists():
        return {}, PACKAGE_ROOT
    with config_path.open("rb") as file:
        return tomllib.load(file), config_path.resolve().parent


def _database_path(args: argparse.Namespace, config: dict, config_dir: Path) -> Path:
    override = args.database or os.environ.get("SEASIC_MONITOR_DB")
    if override:
        path = Path(override).expanduser()
        return path if path.is_absolute() or str(path) == ":memory:" else Path.cwd() / path
    requested = config.get("database", {}).get("path") or "data/processos.sqlite"
    path = Path(requested).expanduser()
    if not path.is_absolute() and str(path) != ":memory:":
        path = config_dir / path
    return path


def _import_catalog(database: MonitorDatabase, csv_path: Path) -> int:
    imported = 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        required = {"system", "numero"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(
                "CSV sem cabeçalhos obrigatórios: " + ", ".join(sorted(missing))
            )
        for line, row in enumerate(reader, start=2):
            active_text = (row.get("ativo") or "sim").strip().casefold()
            if active_text in {"sim", "s", "1", "true", "yes"}:
                active = True
            elif active_text in {"não", "nao", "n", "0", "false", "no"}:
                active = False
            else:
                raise ValueError(
                    f"Linha {line}: ativo deve ser sim/não, true/false ou 1/0."
                )
            try:
                process = ProcessRecord(
                    system=row["system"],
                    number=row["numero"],
                    area=row.get("area") or "",
                    program=row.get("programa") or "",
                    description=row.get("descricao") or "",
                    active=active,
                    inactivation_reason=row.get("motivo_inativacao") or "",
                )
            except ValueError as exc:
                raise ValueError(f"Linha {line}: {exc}") from exc
            database.upsert_process(process)
            imported += 1
    return imported


def _collector_settings(config: dict, system: str) -> dict:
    return config.get("collectors", {}).get(system, {})


def _run(args: argparse.Namespace) -> int:
    config, config_dir = _load_config(args.config)
    database = MonitorDatabase(_database_path(args, config, config_dir))
    stagnant_after_days = int(config.get("monitor", {}).get("stagnant_after_days", 30))

    if args.command == "init":
        database.initialize()
        print(f"Banco inicializado em: {database.path}")
        return 0
    if args.command == "demo":
        # Use a dedicated synthetic database unless the operator explicitly overrides it.
        if args.database is None and os.environ.get("SEASIC_MONITOR_DB") is None:
            demo_path = config_dir / "data" / "demo.sqlite"
            database = MonitorDatabase(demo_path)
        run_id = run_demo(database, stagnant_after_days)
        print(f"Execução sintética {run_id} gravada em: {database.path}")
        print(render_markdown(database.run_report(run_id)))
        return 0
    if args.command == "login-edoc":
        args.command, args.sistema = "login", "EDOC"
    if args.command == "run-edoc":
        args.command, args.sistema = "run", "EDOC"
    if args.command not in {"run", "login", "consultar"}:
        database.initialize()
    if args.command == "status":
        print(render_status(database.run_report()))
        return 0
    if args.command == "report":
        report_data = database.run_report(args.run_id)
        if args.format == "json":
            print(render_json(report_data or {}))
        else:
            print(render_markdown(report_data))
        return 0
    if args.command == "history":
        print(render_json(database.process_history(args.system, args.number)))
        return 0
    if args.command == "import-catalog":
        database.initialize()
        count = _import_catalog(database, args.csv_file)
        print(f"{count} registro(s) importado(s)/atualizado(s).")
        return 0
    if args.command == "backup":
        database.initialize()
        backup_path = database.backup(args.destination)
        print(f"Backup SQLite criado em: {backup_path}")
        return 0
    if args.command == "login":
        system = canonical_system(args.sistema)
        logged_in = asyncio.run(
            prepare_session(system, _collector_settings(config, system), config_dir)
        )
        if logged_in:
            print(
                "Login manual concluído; perfil local salvo. "
                "run ainda valida seletores e autorização antes de consultar."
            )
        else:
            print(
                "A tela de consulta já estava pronta; nenhuma nova autenticação "
                "foi solicitada."
            )
        return 0
    if args.command == "consultar":
        system = canonical_system(args.system)
        observation = asyncio.run(
            consult_one(
                system,
                args.number,
                _collector_settings(config, system),
                config_dir,
            )
        )
        print(render_json({**asdict(observation), "valid": observation.is_valid}))
        print("Consulta avulsa: nada foi gravado no banco.", file=sys.stderr)
        return 0 if observation.is_valid else 1
    if args.command == "run":
        system = canonical_system(args.sistema)
        run_id = asyncio.run(
            run_collection(
                database,
                system,
                _collector_settings(config, system),
                config_dir,
                RunPolicy.from_config(config, system),
            )
        )
        print(f"Execução {args.sistema} {run_id} gravada em: {database.path}")
        print(render_markdown(database.run_report(run_id)))
        return 0
    raise ValueError(f"Comando não reconhecido: {args.command}")


def main() -> None:
    args = _parser().parse_args()
    try:
        status = _run(args)
    except (OSError, ValueError, LiveCollectionDisabled) as exc:
        print(f"Erro: {exc}", file=sys.stderr)
        status = 2
    raise SystemExit(status)


if __name__ == "__main__":
    main()

