from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from .comparison import compare_observation, failure_occurrence
from .domain import (
    ComparisonStatus,
    Observation,
    ProcessRecord,
    canonical_system,
    utc_now,
)


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS processes (
    system TEXT NOT NULL CHECK (system IN ('SEI', 'EDOC')),
    number TEXT NOT NULL,
    area TEXT NOT NULL DEFAULT '',
    program TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    inactivation_reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (system, number)
);

CREATE TABLE IF NOT EXISTS executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    mode TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('EM_ANDAMENTO', 'OK', 'PARCIAL', 'FALHOU')),
    total_processes INTEGER NOT NULL DEFAULT 0,
    succeeded INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES executions(id),
    system TEXT NOT NULL,
    number TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    status TEXT NOT NULL,
    units_json TEXT NOT NULL DEFAULT '[]',
    executive_sector TEXT NOT NULL DEFAULT '',
    last_movement TEXT NOT NULL DEFAULT '',
    movement_date TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL DEFAULT '',
    valid INTEGER NOT NULL CHECK (valid IN (0, 1)),
    comparison_status TEXT NOT NULL CHECK (
        comparison_status IN ('BASELINE', 'SEM_MUDANCA', 'ALTERACAO', 'INVALIDA')
    ),
    error_code TEXT NOT NULL DEFAULT '',
    error_message TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (system, number) REFERENCES processes(system, number),
    UNIQUE (run_id, system, number)
);

CREATE TABLE IF NOT EXISTS occurrences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    run_id INTEGER NOT NULL REFERENCES executions(id),
    system TEXT NOT NULL,
    number TEXT NOT NULL,
    type TEXT NOT NULL,
    description TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    communicated INTEGER NOT NULL DEFAULT 0 CHECK (communicated IN (0, 1)),
    dedupe_key TEXT NOT NULL UNIQUE,
    FOREIGN KEY (system, number) REFERENCES processes(system, number)
);

CREATE INDEX IF NOT EXISTS idx_snapshots_process_time
    ON snapshots(system, number, collected_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_snapshots_run ON snapshots(run_id);
CREATE INDEX IF NOT EXISTS idx_occurrences_run ON occurrences(run_id, detected_at);
CREATE INDEX IF NOT EXISTS idx_occurrences_process ON occurrences(system, number, detected_at);
"""


class MonitorDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()

    def _connect(self) -> sqlite3.Connection:
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                os.chmod(self.path.parent, 0o700)
            except OSError:
                pass
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if str(self.path) != ":memory:":
            connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connection() as connection:
            connection.executescript(SCHEMA)
            self._migrate(connection)
        if str(self.path) != ":memory:" and self.path.exists():
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """Acrescenta colunas novas em bancos criados por versões anteriores."""
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(snapshots)").fetchall()
        }
        if "display_number" not in columns:
            connection.execute(
                "ALTER TABLE snapshots ADD COLUMN display_number TEXT NOT NULL DEFAULT ''"
            )
        execution_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(executions)").fetchall()
        }
        if "scope" not in execution_columns:
            # Sistema consultado pela execução; vazio = todos os sistemas.
            connection.execute(
                "ALTER TABLE executions ADD COLUMN scope TEXT NOT NULL DEFAULT ''"
            )

    def upsert_process(self, process: ProcessRecord) -> None:
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO processes (
                    system, number, area, program, description, active,
                    inactivation_reason, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(system, number) DO UPDATE SET
                    area = excluded.area,
                    program = excluded.program,
                    description = excluded.description,
                    active = excluded.active,
                    inactivation_reason = excluded.inactivation_reason,
                    updated_at = excluded.updated_at
                """,
                (
                    process.system,
                    process.number,
                    process.area,
                    process.program,
                    process.description,
                    int(process.active),
                    process.inactivation_reason,
                    utc_now(),
                ),
            )

    def active_processes(self) -> list[ProcessRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT system, number, area, program, description, active,
                       inactivation_reason
                FROM processes
                WHERE active = 1
                ORDER BY system, number
                """
            ).fetchall()
        return [
            ProcessRecord(
                system=row["system"],
                number=row["number"],
                area=row["area"],
                program=row["program"],
                description=row["description"],
                active=bool(row["active"]),
                inactivation_reason=row["inactivation_reason"],
            )
            for row in rows
        ]

    def process_count(self, active_only: bool = True) -> int:
        query = "SELECT COUNT(*) FROM processes"
        if active_only:
            query += " WHERE active = 1"
        with self._connection() as connection:
            return int(connection.execute(query).fetchone()[0])

    def create_execution(
        self,
        mode: str,
        started_at: str | None = None,
        scope: str = "",
    ) -> int:
        with self._connection() as connection:
            # A rotina é serial: qualquer execução ainda aberta foi interrompida
            # (queda, Ctrl+C) e não pode continuar parecendo em andamento.
            connection.execute(
                """
                UPDATE executions
                SET finished_at = ?, status = 'FALHOU',
                    succeeded = (
                        SELECT COUNT(*) FROM snapshots
                        WHERE run_id = executions.id AND valid = 1
                    ),
                    failed = (
                        SELECT COUNT(*) FROM snapshots
                        WHERE run_id = executions.id AND valid = 0
                    ),
                    total_processes = (
                        SELECT COUNT(*) FROM snapshots WHERE run_id = executions.id
                    ),
                    notes = 'Execução interrompida antes de ser finalizada.'
                WHERE status = 'EM_ANDAMENTO'
                """,
                (utc_now(),),
            )
            cursor = connection.execute(
                """
                INSERT INTO executions (started_at, mode, status, scope)
                VALUES (?, ?, 'EM_ANDAMENTO', ?)
                """,
                (started_at or utc_now(), mode, canonical_system(scope) if scope else ""),
            )
            return int(cursor.lastrowid)

    def finish_execution(
        self,
        run_id: int,
        total: int,
        succeeded: int,
        failed: int,
        notes: str = "",
        finished_at: str | None = None,
        planned: int | None = None,
    ) -> None:
        if succeeded + failed != total:
            raise ValueError("O total deve ser igual a sucessos mais falhas.")
        planned = total if planned is None else planned
        if planned < total:
            raise ValueError("O total consultado não pode superar o planejado.")
        if planned > 0 and succeeded == 0:
            status = "FALHOU"
        elif failed or notes or total < planned:
            status = "PARCIAL"
        else:
            status = "OK"
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE executions
                SET finished_at = ?, status = ?, total_processes = ?,
                    succeeded = ?, failed = ?, notes = ?
                WHERE id = ? AND status = 'EM_ANDAMENTO'
                """,
                (
                    finished_at or utc_now(),
                    status,
                    total,
                    succeeded,
                    failed,
                    notes[:500],
                    run_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Execução {run_id} não encontrada ou já finalizada.")

    def count_snapshots_since(self, system: str, since_utc: str) -> int:
        """Consultas já feitas a um sistema desde o instante informado (UTC ISO)."""
        with self._connection() as connection:
            return int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM snapshots
                    WHERE system = ? AND collected_at >= ?
                    """,
                    (canonical_system(system), since_utc),
                ).fetchone()[0]
            )

    def latest_valid_snapshot(self, system: str, number: str) -> dict[str, Any] | None:
        system = canonical_system(system)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM snapshots
                WHERE system = ? AND number = ? AND valid = 1
                ORDER BY collected_at DESC, id DESC
                LIMIT 1
                """,
                (system, number),
            ).fetchone()
        return self._snapshot_dict(row) if row else None

    def latest_attempt(self, system: str, number: str) -> dict[str, Any] | None:
        system = canonical_system(system)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM snapshots
                WHERE system = ? AND number = ?
                ORDER BY collected_at DESC, id DESC
                LIMIT 1
                """,
                (system, number),
            ).fetchone()
        return self._snapshot_dict(row) if row else None

    def record_observation(
        self,
        run_id: int,
        observation: Observation,
        stagnant_after_days: int,
    ) -> dict[str, Any]:
        if stagnant_after_days < 0:
            raise ValueError("O limite de dias sem movimentação não pode ser negativo.")

        current = observation
        if current.status.value == "OK" and not current.is_valid:
            current = replace(
                current,
                status=type(current.status).EXTRACTION_ERROR,
                units=(),
                executive_sector="",
                last_movement="",
                movement_date="",
                error_code="DADOS_INCOMPLETOS",
                error_message="A coleta não trouxe os campos mínimos confiáveis.",
            )
        if not current.is_valid:
            current = replace(
                current,
                units=(),
                executive_sector="",
                last_movement="",
                movement_date="",
            )
        error_message = " ".join(current.error_message.split())[:500]

        with self._connection() as connection:
            process_exists = connection.execute(
                "SELECT 1 FROM processes WHERE system = ? AND number = ?",
                (current.system, current.number),
            ).fetchone()
            if not process_exists:
                raise ValueError(
                    f"Processo fora do Cadastro Mestre: {current.system} {current.number}."
                )
            previous_attempt_row = connection.execute(
                """
                SELECT id, status, valid FROM snapshots
                WHERE system = ? AND number = ?
                ORDER BY collected_at DESC, id DESC
                LIMIT 1
                """,
                (current.system, current.number),
            ).fetchone()
            previous_valid_row = connection.execute(
                """
                SELECT * FROM snapshots
                WHERE system = ? AND number = ? AND valid = 1
                ORDER BY collected_at DESC, id DESC
                LIMIT 1
                """,
                (current.system, current.number),
            ).fetchone()
            previous_valid = (
                self._snapshot_dict(previous_valid_row) if previous_valid_row else None
            )
            valid = current.is_valid
            comparison = ComparisonStatus.INVALID
            if valid:
                if previous_valid is None:
                    comparison = ComparisonStatus.BASELINE
                elif previous_valid["content_hash"] == current.content_hash:
                    comparison = ComparisonStatus.UNCHANGED
                else:
                    comparison = ComparisonStatus.CHANGED

            cursor = connection.execute(
                """
                INSERT INTO snapshots (
                    run_id, system, number, collected_at, status, units_json,
                    executive_sector, last_movement, movement_date, content_hash,
                    valid, comparison_status, error_code, error_message,
                    display_number
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    current.system,
                    current.number,
                    current.collected_at,
                    current.status.value,
                    json.dumps(current.units, ensure_ascii=False),
                    current.executive_sector if valid else "",
                    current.last_movement if valid else "",
                    current.movement_date if valid else "",
                    current.content_hash if valid else "",
                    int(valid),
                    comparison.value,
                    current.error_code[:100],
                    error_message,
                    current.display_number[:200],
                ),
            )
            snapshot_id = int(cursor.lastrowid)
            candidates = (
                compare_observation(
                    previous_valid,
                    current,
                    snapshot_id,
                    stagnant_after_days,
                )
                if valid
                else []
            )
            if not valid:
                failure = failure_occurrence(
                    current,
                    int(previous_attempt_row["id"]) if previous_attempt_row else None,
                    str(previous_attempt_row["status"]) if previous_attempt_row else None,
                    snapshot_id,
                )
                if failure:
                    candidates.append(failure)
            added = 0
            for candidate in candidates:
                event_cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO occurrences (
                        snapshot_id, run_id, system, number, type, description,
                        detected_at, dedupe_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        run_id,
                        current.system,
                        current.number,
                        candidate.kind,
                        candidate.description[:500],
                        current.collected_at,
                        candidate.dedupe_key,
                    ),
                )
                added += max(event_cursor.rowcount, 0)

        return {
            "snapshot_id": snapshot_id,
            "valid": valid,
            "status": current.status.value,
            "comparison_status": comparison.value,
            "occurrences_added": added,
        }

    def run_report(self, run_id: int | None = None) -> dict[str, Any] | None:
        with self._connection() as connection:
            selected_run = (
                connection.execute(
                    "SELECT * FROM executions WHERE id = ?", (run_id,)
                ).fetchone()
                if run_id is not None
                else connection.execute(
                    "SELECT * FROM executions ORDER BY id DESC LIMIT 1"
                ).fetchone()
            )
            if selected_run is None:
                return None
            snapshots = connection.execute(
                "SELECT * FROM snapshots WHERE run_id = ? ORDER BY system, number",
                (selected_run["id"],),
            ).fetchall()
            occurrences = connection.execute(
                "SELECT * FROM occurrences WHERE run_id = ? ORDER BY detected_at, id",
                (selected_run["id"],),
            ).fetchall()
            scope = selected_run["scope"]
            active_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM processes WHERE active = 1"
                    + (" AND system = ?" if scope else ""),
                    (scope,) if scope else (),
                ).fetchone()[0]
            )
            last_successful = connection.execute(
                """
                SELECT * FROM executions
                WHERE succeeded > 0 AND status IN ('OK', 'PARCIAL')
                ORDER BY id DESC LIMIT 1
                """
            ).fetchone()
            latest_failures = connection.execute(
                """
                SELECT s.* FROM snapshots s
                JOIN (
                    SELECT system, number, MAX(id) AS latest_id
                    FROM snapshots GROUP BY system, number
                ) latest ON latest.latest_id = s.id
                WHERE s.valid = 0
                ORDER BY s.system, s.number
                """
            ).fetchall()
            latest_valid = connection.execute(
                """
                SELECT s.* FROM snapshots s
                JOIN (
                    SELECT system, number, MAX(id) AS latest_id
                    FROM snapshots WHERE valid = 1 GROUP BY system, number
                ) latest ON latest.latest_id = s.id
                ORDER BY s.system, s.number
                """
            ).fetchall()
        return {
            "execution": dict(selected_run),
            "snapshots": [self._snapshot_dict(row) for row in snapshots],
            "occurrences": [dict(row) for row in occurrences],
            "active_count": active_count,
            "last_successful_execution": (
                dict(last_successful) if last_successful else None
            ),
            "current_failures": [self._snapshot_dict(row) for row in latest_failures],
            "current_valid": [self._snapshot_dict(row) for row in latest_valid],
        }

    def process_history(self, system: str, number: str) -> dict[str, Any]:
        system = canonical_system(system)
        with self._connection() as connection:
            process = connection.execute(
                "SELECT * FROM processes WHERE system = ? AND number = ?",
                (system, number.strip()),
            ).fetchone()
            if not process:
                raise ValueError(f"Processo não cadastrado: {system} {number}.")
            snapshots = connection.execute(
                """
                SELECT * FROM snapshots WHERE system = ? AND number = ?
                ORDER BY collected_at DESC, id DESC
                """,
                (system, number.strip()),
            ).fetchall()
            occurrences = connection.execute(
                """
                SELECT * FROM occurrences WHERE system = ? AND number = ?
                ORDER BY detected_at DESC, id DESC
                """,
                (system, number.strip()),
            ).fetchall()
        return {
            "process": dict(process),
            "snapshots": [self._snapshot_dict(row) for row in snapshots],
            "occurrences": [dict(row) for row in occurrences],
        }

    def demo_processes(self) -> list[ProcessRecord]:
        demo = [
            ProcessRecord(
                system="SEI",
                number="DEMO-001",
                area="DEMO",
                program="Exemplo fictício",
                description="Registro sintético para validar o fluxo.",
            ),
            ProcessRecord(
                system="SEI",
                number="DEMO-002",
                area="DEMO",
                program="Exemplo fictício",
                description="Registro sintético parado.",
            ),
            ProcessRecord(
                system="EDOC",
                number="DEMO-003",
                area="DEMO",
                program="Exemplo fictício",
                description="Registro sintético para demonstrar falha.",
            ),
        ]
        for process in demo:
            self.upsert_process(process)
        return demo

    def backup(self, destination: str | Path) -> Path:
        if str(self.path) == ":memory:":
            raise ValueError("Não é possível criar backup de banco em memória.")
        target = Path(destination).expanduser()
        if target.resolve() == self.path.resolve():
            raise ValueError("O destino do backup precisa ser diferente do banco ativo.")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        source_conn = sqlite3.connect(self.path)
        target_conn = sqlite3.connect(target)
        try:
            source_conn.backup(target_conn)
        finally:
            target_conn.close()
            source_conn.close()
        try:
            os.chmod(target.parent, 0o700)
            os.chmod(target, 0o600)
        except OSError:
            pass
        return target

    @staticmethod
    def _snapshot_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["units"] = tuple(json.loads(result.pop("units_json")))
        result["valid"] = bool(result["valid"])
        return result

