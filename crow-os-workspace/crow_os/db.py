"""SQLite store plus an append-only event log.

Coordination state lives here. Git snapshots are replaceable. Events are not.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from .util import utc_now


SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS branches (
    name TEXT PRIMARY KEY,
    sha TEXT,
    subject TEXT,
    committer_date TEXT,
    is_remote INTEGER,
    ahead INTEGER,
    behind INTEGER
);

CREATE TABLE IF NOT EXISTS commits (
    sha TEXT NOT NULL,
    branch TEXT NOT NULL,
    subject TEXT,
    author TEXT,
    committed_at TEXT,
    PRIMARY KEY (sha, branch)
);

CREATE TABLE IF NOT EXISTS pulls (
    number INTEGER PRIMARY KEY,
    title TEXT,
    state TEXT,
    draft INTEGER,
    merged INTEGER,
    head_ref TEXT,
    head_sha TEXT,
    base_ref TEXT,
    html_url TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS checks (
    id TEXT PRIMARY KEY,
    name TEXT,
    status TEXT,
    conclusion TEXT,
    sha TEXT,
    html_url TEXT,
    pr_number INTEGER
);

CREATE TABLE IF NOT EXISTS test_files (
    ref TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT,
    size INTEGER,
    case_count INTEGER,
    syntax_ok INTEGER,
    syntax_error TEXT,
    suite TEXT,
    components TEXT,
    PRIMARY KEY (ref, path)
);

CREATE TABLE IF NOT EXISTS artifacts (
    ref TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT,
    size INTEGER,
    kind TEXT,
    PRIMARY KEY (ref, path)
);

CREATE TABLE IF NOT EXISTS changes (
    id TEXT PRIMARY KEY,
    title TEXT,
    ref TEXT,
    sha TEXT,
    pr_number INTEGER,
    source TEXT,
    required_source TEXT,
    required_files TEXT,
    contained_in_main INTEGER,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS gates (
    change_id TEXT NOT NULL,
    gate_id TEXT NOT NULL,
    name TEXT,
    kind TEXT,
    passed INTEGER,
    detail TEXT,
    PRIMARY KEY (change_id, gate_id)
);

CREATE TABLE IF NOT EXISTS rounds (
    id TEXT PRIMARY KEY,
    title TEXT,
    ref TEXT,
    commit_sha TEXT,
    runner TEXT,
    status TEXT,
    created_at TEXT,
    started_at TEXT,
    finished_at TEXT,
    env_json TEXT,
    inputs_json TEXT,
    exit_code INTEGER,
    log_sha256 TEXT,
    log_path TEXT,
    change_id TEXT,
    acceptance INTEGER,
    sealed INTEGER
);

CREATE TABLE IF NOT EXISTS round_files (
    round_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    path TEXT,
    status TEXT,
    exit_code INTEGER,
    stdout TEXT,
    stderr TEXT,
    trace TEXT,
    started_at TEXT,
    finished_at TEXT,
    log_sha256 TEXT,
    log_path TEXT,
    PRIMARY KEY (round_id, ord)
);

CREATE TABLE IF NOT EXISTS round_cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT,
    file_path TEXT,
    name TEXT,
    outcome TEXT,
    detail TEXT,
    trace TEXT,
    stdout TEXT,
    stderr TEXT
);

CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id TEXT,
    ts TEXT,
    cursor_index INTEGER,
    completed_json TEXT,
    note TEXT
);

CREATE TABLE IF NOT EXISTS fixes (
    id TEXT PRIMARY KEY,
    change_id TEXT,
    round_id TEXT,
    file_path TEXT,
    case_name TEXT,
    trace TEXT,
    status TEXT,
    fix_commit TEXT,
    fix_pr INTEGER,
    reverify_round_id TEXT,
    created_at TEXT,
    cleared_at TEXT
);

CREATE TABLE IF NOT EXISTS assignments (
    id TEXT PRIMARY KEY,
    lane TEXT,
    agent_kind TEXT,
    agent_ref TEXT,
    note TEXT,
    change_id TEXT,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS anomalies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    round_id TEXT,
    message TEXT,
    trace TEXT
);
"""

SNAPSHOT_TABLES = ("branches", "commits", "pulls", "checks", "test_files", "artifacts")


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def event(self, kind: str, payload: dict[str, Any] | None = None, ts: str | None = None) -> int:
        body = json.dumps(payload or {}, sort_keys=True, default=str)
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO events (ts, kind, payload) VALUES (?, ?, ?)",
                (ts or utc_now(), kind, body),
            )
            self.conn.commit()
            return int(cur.lastrowid)

    def events(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, ts, kind, payload FROM events ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        found = []
        for row in rows:
            item = _row(row)
            item["payload"] = json.loads(item["payload"])
            found.append(item)
        return found

    def meta_set(self, key: str, value: Any) -> None:
        encoded = value if isinstance(value, str) else json.dumps(value)
        with self._lock:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, encoded),
            )
            self.conn.commit()

    def meta_get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row is None:
            return default
        raw = row["value"]
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def replace_snapshot(self, table: str, rows: Iterable[dict[str, Any]]) -> None:
        if table not in SNAPSHOT_TABLES:
            raise ValueError(f"not a snapshot table: {table}")
        payload = list(rows)
        with self._lock:
            self.conn.execute(f"DELETE FROM {table}")
            if payload:
                columns = list(payload[0].keys())
                placeholders = ", ".join("?" for _ in columns)
                sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
                self.conn.executemany(sql, [tuple(item[col] for col in columns) for item in payload])
            self.conn.commit()

    def query(self, sql: str, args: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(sql, args).fetchall()
        return [_row(row) for row in rows]

    def query_one(self, sql: str, args: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self.conn.execute(sql, args).fetchone()
        return _row(row)

    def execute(self, sql: str, args: tuple[Any, ...] = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return int(cur.lastrowid or 0)

    def executemany(self, sql: str, seq: Iterable[tuple[Any, ...]]) -> None:
        with self._lock:
            self.conn.executemany(sql, list(seq))
            self.conn.commit()
