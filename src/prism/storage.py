"""Store snapshots, checkpoints, and list identities in SQLite."""

import fcntl
import gzip
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models.collection import (
    CollectedPage,
    CollectionKind,
    TaskState,
    validate_page,
    validate_task_state,
)
from .models.publication import ManagedList, validate_managed_lists
from .models.runs import RunRecord, validate_run_record


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (
                snapshot TEXT, actor TEXT, kind TEXT, data TEXT NOT NULL,
                PRIMARY KEY (snapshot, actor, kind));
            CREATE TABLE IF NOT EXISTS pages (
                snapshot TEXT, actor TEXT, kind TEXT, number INTEGER, data TEXT NOT NULL,
                PRIMARY KEY (snapshot, actor, kind, number));
            CREATE TABLE IF NOT EXISTS registry (
                actor TEXT, uri TEXT, data TEXT NOT NULL, PRIMARY KEY (actor, uri));
        """)

    def close(self):
        self.connection.close()

    @contextmanager
    def locked(self):
        with self.path.with_suffix(".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("Another command is using this database") from error
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def create_snapshot(self, actor: str, *, dry_run: bool, options: dict[str, Any]) -> RunRecord:
        now = datetime.now(UTC)
        snapshot: RunRecord = {
            "id": now.strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:8],
            "actor": {"did": actor},
            "created_at": now.isoformat(),
            "cutoff": (now - timedelta(days=90)).isoformat(),
            "days": 90,
            "dry_run": dry_run,
            "stage": "collecting",
            "options": options,
        }
        self.save_snapshot(snapshot)
        return snapshot

    def save_snapshot(self, snapshot: RunRecord) -> None:
        validate_run_record(snapshot)
        with self.connection:
            self.connection.execute(
                "INSERT OR REPLACE INTO snapshots VALUES (?, ?)",
                (snapshot["id"], json.dumps(snapshot)),
            )

    def snapshot(self, identifier: str) -> RunRecord:
        row = self.connection.execute(
            "SELECT data FROM snapshots WHERE id=?", (identifier,)
        ).fetchone()
        if not row:
            raise ValueError(f"Unknown snapshot: {identifier}")
        return validate_run_record(json.loads(row[0]), "stored run")

    def pending(self) -> RunRecord | None:
        rows = self.connection.execute("SELECT data FROM snapshots ORDER BY id DESC")
        for row in rows:
            run = validate_run_record(json.loads(row[0]), "stored run")
            if run["stage"] != "complete":
                return run
        return None

    def has_completed_dry_run(self, actor: str) -> bool:
        for row in self.connection.execute("SELECT data FROM snapshots"):
            snapshot = validate_run_record(json.loads(row[0]), "stored run")
            if (
                snapshot["actor"]["did"] == actor
                and snapshot["dry_run"]
                and snapshot["stage"] == "complete"
            ):
                return True
        return False

    def complete_snapshot(self, snapshot: RunRecord) -> None:
        completed = snapshot.copy()
        completed["stage"] = "complete"
        completed.pop("result", None)
        completed.pop("publication", None)
        self.save_snapshot(completed)

    def task(self, snapshot: str, actor: str, kind: CollectionKind) -> TaskState:
        row = self.connection.execute(
            "SELECT data FROM tasks WHERE snapshot=? AND actor=? AND kind=?",
            (snapshot, actor, kind),
        ).fetchone()
        if row:
            return validate_task_state(json.loads(row[0]), "stored task")
        return {
            "status": "pending",
            "cursor": None,
            "pages": 0,
            "cursors": [],
            "restarts": 0,
        }

    def save_task(self, snapshot: str, actor: str, kind: CollectionKind, state: TaskState) -> None:
        validate_task_state(state)
        with self.connection:
            self._save_task(snapshot, actor, kind, state)

    def _save_task(self, snapshot: str, actor: str, kind: CollectionKind, state: TaskState) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO tasks VALUES (?, ?, ?, ?)",
            (snapshot, actor, kind, json.dumps(state)),
        )

    def save_page(
        self,
        snapshot: str,
        actor: str,
        kind: CollectionKind,
        state: TaskState,
        page: CollectedPage,
    ) -> None:
        validate_page(page, kind)
        # Commit the page and cursor together so a crash cannot leave them out of sync.
        with self.connection:
            self.connection.execute(
                "INSERT INTO pages VALUES (?, ?, ?, ?, ?)",
                (snapshot, actor, kind, state["pages"], json.dumps(page)),
            )
            self._save_task(snapshot, actor, kind, state)

    def restart_task(
        self, snapshot: str, actor: str, kind: CollectionKind, state: TaskState
    ) -> None:
        state.update(
            cursor=None,
            pages=0,
            cursors=[],
            status="pending",
            error=None,
            restarts=state["restarts"] + 1,
        )
        with self.connection:
            self.connection.execute(
                "DELETE FROM pages WHERE snapshot=? AND actor=? AND kind=?", (snapshot, actor, kind)
            )
            self._save_task(snapshot, actor, kind, state)

    def pages(self, snapshot: str, actor: str, kind: CollectionKind) -> list[CollectedPage]:
        rows = self.connection.execute(
            "SELECT data FROM pages WHERE snapshot=? AND actor=? AND kind=? ORDER BY number",
            (snapshot, actor, kind),
        )
        return [validate_page(json.loads(row[0]), kind, "stored page") for row in rows]

    def registered_lists(self, actor: str) -> list[ManagedList]:
        values = [
            json.loads(row[0])
            for row in self.connection.execute(
                "SELECT data FROM registry WHERE actor=? ORDER BY uri", (actor,)
            )
        ]
        return validate_managed_lists(values, "stored registry")

    def register_lists(self, actor: str, lists: list[ManagedList]) -> None:
        validate_managed_lists(lists)
        with self.connection:
            self.connection.executemany(
                "INSERT OR REPLACE INTO registry VALUES (?, ?, ?)",
                [(actor, item["uri"], json.dumps(item)) for item in lists],
            )


def read_gzipped_json(path: Path) -> object:
    with gzip.open(path, "rt", encoding="utf-8") as source:
        return json.load(source)


def write_json(path: Path, value: object, *, compress: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    serialized = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if compress:
        with gzip.open(temporary, "wt", encoding="utf-8") as destination:
            destination.write(serialized)
    else:
        temporary.write_text(serialized, encoding="utf-8")
    temporary.replace(path)
