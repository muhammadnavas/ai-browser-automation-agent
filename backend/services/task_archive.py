"""Durable, bounded storage for terminal task history and generated reports."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from models.schemas import TaskResponse, ResearchTaskResponse

DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "task_archive.sqlite3"


class TaskArchive:
    """SQLite archive for terminal task metadata and PDF bytes."""

    def __init__(self, db_path: Optional[str] = None, max_tasks: Optional[int] = None):
        self.db_path = Path(db_path or os.getenv("TASK_ARCHIVE_DB", str(DEFAULT_DB_PATH)))
        try:
            self.max_tasks = max(1, int(max_tasks or os.getenv("TASK_ARCHIVE_MAX_TASKS", "500")))
        except (TypeError, ValueError):
            self.max_tasks = 500
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS archived_tasks (
                task_id TEXT PRIMARY KEY, updated_at REAL NOT NULL,
                task_json TEXT NOT NULL, report_pdf BLOB)""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_archived_tasks_updated ON archived_tasks(updated_at DESC)")
            db.execute("""CREATE TABLE IF NOT EXISTS archived_research_exports (
                task_id TEXT NOT NULL, format TEXT NOT NULL, report_bytes BLOB NOT NULL,
                PRIMARY KEY(task_id, format))""")
            db.execute("""CREATE TABLE IF NOT EXISTS archived_research_tasks (
                task_id TEXT PRIMARY KEY, updated_at REAL NOT NULL, task_json TEXT NOT NULL)""")

    @contextmanager
    def _connect(self):
        """Open a short-lived SQLite connection and always close it."""
        db = sqlite3.connect(str(self.db_path), timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def save_task(self, task: TaskResponse, report_pdf: Optional[bytes] = None) -> None:
        with self._connect() as db:
            db.execute("""INSERT INTO archived_tasks(task_id, updated_at, task_json, report_pdf)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                updated_at=excluded.updated_at, task_json=excluded.task_json,
                report_pdf=COALESCE(excluded.report_pdf, archived_tasks.report_pdf)""",
                (task.task_id, float(task.updated_at), task.model_dump_json(), report_pdf))
            db.execute("""DELETE FROM archived_tasks WHERE task_id IN (
                SELECT task_id FROM archived_tasks ORDER BY updated_at DESC LIMIT -1 OFFSET ?)""",
                (self.max_tasks,))

    def list_tasks(self) -> list[TaskResponse]:
        with self._connect() as db:
            rows = db.execute("SELECT task_json FROM archived_tasks ORDER BY updated_at DESC LIMIT ?",
                              (self.max_tasks,)).fetchall()
        tasks = []
        for row in rows:
            try:
                tasks.append(TaskResponse.model_validate_json(row["task_json"]))
            except Exception:
                continue
        return tasks

    def get_task(self, task_id: str) -> Optional[TaskResponse]:
        with self._connect() as db:
            row = db.execute("SELECT task_json FROM archived_tasks WHERE task_id=?", (task_id,)).fetchone()
        if not row:
            return None
        try:
            return TaskResponse.model_validate_json(row["task_json"])
        except Exception:
            return None

    def get_report(self, task_id: str) -> Optional[bytes]:
        with self._connect() as db:
            row = db.execute("SELECT report_pdf FROM archived_tasks WHERE task_id=?", (task_id,)).fetchone()
        return bytes(row["report_pdf"]) if row and row["report_pdf"] is not None else None

    def save_research_task(self, task: ResearchTaskResponse) -> None:
        """Persist a research task, including its comparison result when available."""
        with self._connect() as db:
            db.execute(
                """INSERT INTO archived_research_tasks(task_id, updated_at, task_json)
                   VALUES (?, ?, ?)
                   ON CONFLICT(task_id) DO UPDATE SET
                   updated_at=excluded.updated_at, task_json=excluded.task_json""",
                (task.task_id, float(task.updated_at), task.model_dump_json()),
            )
            db.execute(
                """DELETE FROM archived_research_tasks WHERE task_id IN (
                   SELECT task_id FROM archived_research_tasks
                   ORDER BY updated_at DESC LIMIT -1 OFFSET ?)""",
                (self.max_tasks,),
            )

    def get_research_task(self, task_id: str) -> Optional[ResearchTaskResponse]:
        with self._connect() as db:
            row = db.execute(
                "SELECT task_json FROM archived_research_tasks WHERE task_id=?",
                (task_id,),
            ).fetchone()
        if not row:
            return None
        try:
            return ResearchTaskResponse.model_validate_json(row["task_json"])
        except Exception:
            return None

    def list_research_tasks(self) -> list[ResearchTaskResponse]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT task_json FROM archived_research_tasks ORDER BY updated_at DESC LIMIT ?",
                (self.max_tasks,),
            ).fetchall()
        tasks = []
        for row in rows:
            try:
                tasks.append(ResearchTaskResponse.model_validate_json(row["task_json"]))
            except Exception:
                continue
        return tasks

    def save_research_export(self, task_id: str, fmt: str, report_bytes: bytes) -> None:
        """Persist a research export so CSV/XLSX/PDF downloads survive restarts."""
        normalized_format = (fmt or "").lower()
        if normalized_format not in {"csv", "xlsx", "pdf"}:
            raise ValueError("Unsupported research export format")
        with self._connect() as db:
            db.execute(
                """INSERT INTO archived_research_exports(task_id, format, report_bytes)
                   VALUES (?, ?, ?)
                   ON CONFLICT(task_id, format) DO UPDATE SET report_bytes=excluded.report_bytes""",
                (task_id, normalized_format, sqlite3.Binary(report_bytes)),
            )

    def get_research_export(self, task_id: str, fmt: str) -> Optional[bytes]:
        normalized_format = (fmt or "").lower()
        if normalized_format not in {"csv", "xlsx", "pdf"}:
            return None
        with self._connect() as db:
            row = db.execute(
                "SELECT report_bytes FROM archived_research_exports WHERE task_id=? AND format=?",
                (task_id, normalized_format),
            ).fetchone()
        return bytes(row["report_bytes"]) if row and row["report_bytes"] is not None else None

    def delete_task(self, task_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM archived_tasks WHERE task_id=?", (task_id,))
            db.execute("DELETE FROM archived_research_exports WHERE task_id=?", (task_id,))
            db.execute("DELETE FROM archived_research_tasks WHERE task_id=?", (task_id,))


task_archive = TaskArchive()
