"""SQLite implementation of the SDK ``BaseDatabase`` persistence port."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any
from uuid import uuid4

from shared.services.database.base_db import BaseDatabase


_JSON_COLUMNS = frozenset({"clauses", "complaint_flags"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS travel_policy (
    policy_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    version        TEXT NOT NULL,
    org_unit       TEXT NOT NULL,
    effective_date TEXT NOT NULL,
    clauses        TEXT NOT NULL,
    UNIQUE (org_unit, version)
);
CREATE INDEX IF NOT EXISTS idx_travel_policy_effective
    ON travel_policy (org_unit, effective_date DESC);

CREATE TABLE IF NOT EXISTS trip_records (
    plan_id             TEXT PRIMARY KEY,
    destination_area    TEXT NOT NULL,
    season              TEXT NOT NULL,
    party_size          INTEGER NOT NULL,
    budget_band         TEXT NOT NULL,
    lodging             TEXT NOT NULL,
    actual_cost         REAL,
    satisfaction_score  REAL,
    complaint_flags     TEXT NOT NULL DEFAULT '[]',
    organizer_edited    INTEGER,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trip_records_destination
    ON trip_records (destination_area, season, budget_band);
"""


class SQLiteDatabase(BaseDatabase):
    """SQLite adapter for the three-method ``BaseDatabase`` contract.

    A shared-cache keeper connection makes ``:memory:`` behave consistently
    across the short-lived connections used by each operation. File-backed
    deployments open one connection per call, allowing safe use from FastAPI
    worker threads without sharing sqlite connection objects between threads.
    """

    def __init__(self, path: str) -> None:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("SQLite database path must be a non-empty string")

        configured_path = path.strip()
        self._keeper: sqlite3.Connection | None = None
        self._write_lock = threading.RLock()
        if configured_path == ":memory:":
            self._database = f"file:cmn_c2_655_{uuid4().hex}?mode=memory&cache=shared"
            self._uri = True
            self._keeper = self._open_connection()
            schema_connection = self._keeper
        else:
            database_path = Path(configured_path).expanduser()
            if not database_path.parent.exists():
                raise ValueError(f"SQLite database parent directory does not exist: {database_path.parent}")
            self._database = str(database_path)
            self._uri = False
            schema_connection = self._open_connection()

        try:
            schema_connection.executescript(_SCHEMA)
            schema_connection.commit()
        finally:
            if schema_connection is not self._keeper:
                schema_connection.close()

    def _open_connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database, timeout=30.0, uri=self._uri)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _adapt_params(params: dict | None) -> dict[str, Any]:
        adapted: dict[str, Any] = {}
        for key, value in (params or {}).items():
            if isinstance(value, (dict, list)):
                adapted[key] = json.dumps(value, ensure_ascii=False, default=str)
            elif isinstance(value, bool):
                adapted[key] = int(value)
            else:
                adapted[key] = value
        return adapted

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        for column in _JSON_COLUMNS.intersection(result):
            value = result[column]
            if isinstance(value, str):
                result[column] = json.loads(value)
        if result.get("organizer_edited") is not None:
            result["organizer_edited"] = bool(result["organizer_edited"])
        return result

    def fetch_one(self, query: str, params: dict | None = None) -> dict | None:
        connection = self._open_connection()
        try:
            row = connection.execute(query, self._adapt_params(params)).fetchone()
            return self._row_to_dict(row) if row is not None else None
        finally:
            connection.close()

    def fetch_many(self, query: str, params: dict | None = None) -> list[dict]:
        connection = self._open_connection()
        try:
            rows = connection.execute(query, self._adapt_params(params)).fetchall()
            return [self._row_to_dict(row) for row in rows]
        finally:
            connection.close()

    def execute(self, query: str, params: dict | None = None) -> Any:
        with self._write_lock:
            connection = self._open_connection()
            try:
                cursor = connection.execute(query, self._adapt_params(params))
                connection.commit()
                return cursor.rowcount
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def close(self) -> None:
        """Release the keeper connection used by an in-memory database."""

        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None
