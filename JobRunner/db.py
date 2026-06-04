from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent
DB_PATH = PROJECT_DIR / 'data' / 'jobs.sqlite3'


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    with _get_connection() as connection:
        connection.execute(
            '''
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                container_id TEXT,
                container_name TEXT,
                job_dir TEXT NOT NULL,
                data_dir TEXT,
                kill_switch_path TEXT,
                lifecycle_log_path TEXT,
                log_path TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT,
                exit_code INTEGER,
                error_message TEXT
            )
            '''
        )


def create_job(job_id: str, job_dir: str, status: str) -> None:
    now = _utc_now()
    with _get_connection() as connection:
        connection.execute(
            '''
            INSERT INTO jobs (job_id, status, job_dir, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ''',
            (job_id, status, job_dir, now, now),
        )


def upsert_job(job_id: str, job_dir: str, status: str) -> None:
    now = _utc_now()
    with _get_connection() as connection:
        connection.execute(
            '''
            INSERT INTO jobs (job_id, status, job_dir, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                status = excluded.status,
                job_dir = excluded.job_dir,
                updated_at = excluded.updated_at
            ''',
            (job_id, status, job_dir, now, now),
        )


def update_job(job_id: str, **fields: object) -> None:
    if not fields:
        return

    fields['updated_at'] = _utc_now()
    assignments = ', '.join(f'{column} = ?' for column in fields)
    values = list(fields.values())
    values.append(job_id)

    with _get_connection() as connection:
        connection.execute(
            f'UPDATE jobs SET {assignments} WHERE job_id = ?',
            values,
        )


def mark_job_completed(job_id: str, status: str, exit_code: int | None, error_message: str | None = None) -> None:
    now = _utc_now()
    with _get_connection() as connection:
        connection.execute(
            '''
            UPDATE jobs
            SET status = ?, exit_code = ?, error_message = ?, updated_at = ?, completed_at = ?
            WHERE job_id = ?
            ''',
            (status, exit_code, error_message, now, now, job_id),
        )


def get_job(job_id: str) -> dict[str, object] | None:
    with _get_connection() as connection:
        row = connection.execute(
            'SELECT * FROM jobs WHERE job_id = ?',
            (job_id,),
        ).fetchone()

    if row is None:
        return None

    return dict(row)


def list_jobs() -> list[dict[str, object]]:
    with _get_connection() as connection:
        rows = connection.execute(
            'SELECT * FROM jobs ORDER BY created_at DESC'
        ).fetchall()

    return [dict(row) for row in rows]


init_db()