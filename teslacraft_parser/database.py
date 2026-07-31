from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


class _ClosingConnection(sqlite3.Connection):
    """Commit/rollback and close when used as a context manager."""

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _canonical_json(record: dict[str, Any]) -> str:
    return json.dumps(
        record,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _content_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _flatten_record(
    record: dict[str, Any],
    *,
    prefix: str = "",
) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, value in record.items():
        field = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            flattened.update(_flatten_record(value, prefix=field))
        elif isinstance(value, list):
            flattened[field] = json.dumps(value, ensure_ascii=False)
        elif value is None:
            flattened[field] = ""
        else:
            flattened[field] = value
    return flattened


class ParserDatabase:
    """Thread-safe-by-connection SQLite storage for current data and snapshots."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            factory=_ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    options_json TEXT NOT NULL,
                    summary_json TEXT,
                    error TEXT
                );

                CREATE TABLE IF NOT EXISTS records (
                    namespace TEXT NOT NULL,
                    record_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    changed_at TEXT NOT NULL,
                    run_id INTEGER,
                    PRIMARY KEY (namespace, record_key),
                    FOREIGN KEY (run_id) REFERENCES runs(id)
                        ON DELETE SET NULL
                );

                CREATE INDEX IF NOT EXISTS idx_records_namespace
                    ON records(namespace);
                CREATE INDEX IF NOT EXISTS idx_records_run
                    ON records(run_id);

                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    namespace TEXT NOT NULL,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    run_id INTEGER,
                    record_count INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(namespace, name),
                    FOREIGN KEY (run_id) REFERENCES runs(id)
                        ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS snapshot_records (
                    snapshot_id INTEGER NOT NULL,
                    record_key TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    PRIMARY KEY (snapshot_id, record_key),
                    FOREIGN KEY (snapshot_id) REFERENCES snapshots(id)
                        ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT INTO metadata(key, value) VALUES('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(SCHEMA_VERSION),),
            )

    def start_run(self, mode: str, options: dict[str, Any]) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO runs(mode, started_at, status, options_json)
                VALUES(?, ?, 'running', ?)
                """,
                (mode, utc_now(), _canonical_json(options)),
            )
            return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        summary: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE runs
                SET finished_at = ?, status = ?, summary_json = ?, error = ?
                WHERE id = ?
                """,
                (
                    utc_now(),
                    status,
                    _canonical_json(summary or {}),
                    error,
                    run_id,
                ),
            )

    def contains(self, namespace: str, record_key: object) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM records
                WHERE namespace = ? AND record_key = ?
                """,
                (namespace, str(record_key)),
            ).fetchone()
            return row is not None

    def upsert_many(
        self,
        namespace: str,
        records: Iterable[tuple[str, dict[str, Any]]],
        *,
        run_id: int | None = None,
    ) -> dict[str, int]:
        now = utc_now()
        counts = {"new": 0, "changed": 0, "unchanged": 0}
        with self._connect() as connection:
            for record_key, record in records:
                payload = _canonical_json(record)
                digest = _content_hash(payload)
                existing = connection.execute(
                    """
                    SELECT content_hash FROM records
                    WHERE namespace = ? AND record_key = ?
                    """,
                    (namespace, str(record_key)),
                ).fetchone()
                if existing is None:
                    counts["new"] += 1
                    connection.execute(
                        """
                        INSERT INTO records(
                            namespace, record_key, payload_json, content_hash,
                            first_seen_at, last_seen_at, changed_at, run_id
                        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            namespace,
                            str(record_key),
                            payload,
                            digest,
                            now,
                            now,
                            now,
                            run_id,
                        ),
                    )
                    continue

                changed = str(existing["content_hash"]) != digest
                counts["changed" if changed else "unchanged"] += 1
                connection.execute(
                    """
                    UPDATE records
                    SET payload_json = ?, content_hash = ?, last_seen_at = ?,
                        changed_at = CASE WHEN content_hash != ?
                            THEN ? ELSE changed_at END,
                        run_id = ?
                    WHERE namespace = ? AND record_key = ?
                    """,
                    (
                        payload,
                        digest,
                        now,
                        digest,
                        now,
                        run_id,
                        namespace,
                        str(record_key),
                    ),
                )
        return counts

    def iter_records(self, namespace: str) -> Iterator[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json FROM records
                WHERE namespace = ?
                ORDER BY record_key COLLATE NOCASE
                """,
                (namespace,),
            ).fetchall()
        for row in rows:
            value = json.loads(str(row["payload_json"]))
            if isinstance(value, dict):
                yield value

    def record_count(self, namespace: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM records WHERE namespace = ?",
                (namespace,),
            ).fetchone()
            return int(row["count"] if row else 0)

    def namespaces(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT namespace, COUNT(*) AS record_count,
                       MAX(last_seen_at) AS updated_at
                FROM records
                GROUP BY namespace
                ORDER BY namespace COLLATE NOCASE
                """
            ).fetchall()
            return [dict(row) for row in rows]

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, mode, started_at, finished_at, status,
                       options_json, summary_json, error
                FROM runs
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["options"] = json.loads(item.pop("options_json") or "{}")
            item["summary"] = json.loads(item.pop("summary_json") or "{}")
            result.append(item)
        return result

    def create_snapshot(
        self,
        namespace: str,
        *,
        name: str | None = None,
        run_id: int | None = None,
    ) -> int:
        created_at = utc_now()
        snapshot_name = name or created_at.replace(":", "-")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO snapshots(namespace, name, created_at, run_id)
                VALUES(?, ?, ?, ?)
                """,
                (namespace, snapshot_name, created_at, run_id),
            )
            snapshot_id = int(cursor.lastrowid)
            connection.execute(
                """
                INSERT INTO snapshot_records(
                    snapshot_id, record_key, payload_json, content_hash
                )
                SELECT ?, record_key, payload_json, content_hash
                FROM records WHERE namespace = ?
                """,
                (snapshot_id, namespace),
            )
            connection.execute(
                """
                UPDATE snapshots
                SET record_count = (
                    SELECT COUNT(*) FROM snapshot_records
                    WHERE snapshot_id = ?
                )
                WHERE id = ?
                """,
                (snapshot_id, snapshot_id),
            )
        return snapshot_id

    def list_snapshots(
        self,
        namespace: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT id, namespace, name, created_at, run_id, record_count FROM snapshots"
        parameters: tuple[Any, ...] = ()
        if namespace:
            query += " WHERE namespace = ?"
            parameters = (namespace,)
        query += " ORDER BY id DESC"
        with self._connect() as connection:
            return [dict(row) for row in connection.execute(query, parameters).fetchall()]

    def compare_snapshots(
        self,
        older_id: int,
        newer_id: int,
    ) -> dict[str, list[dict[str, Any]]]:
        if older_id == newer_id:
            return {"added": [], "removed": [], "changed": []}
        with self._connect() as connection:
            snapshot_rows = connection.execute(
                "SELECT id, namespace FROM snapshots WHERE id IN (?, ?)",
                (older_id, newer_id),
            ).fetchall()
            if len(snapshot_rows) != 2:
                raise ValueError("Один из выбранных снимков не найден")
            if len({str(row["namespace"]) for row in snapshot_rows}) != 1:
                raise ValueError("Сравнивать можно снимки одного набора данных")
            older_rows = connection.execute(
                """
                SELECT record_key, payload_json, content_hash
                FROM snapshot_records WHERE snapshot_id = ?
                """,
                (older_id,),
            ).fetchall()
            newer_rows = connection.execute(
                """
                SELECT record_key, payload_json, content_hash
                FROM snapshot_records WHERE snapshot_id = ?
                """,
                (newer_id,),
            ).fetchall()

        older = {str(row["record_key"]): row for row in older_rows}
        newer = {str(row["record_key"]): row for row in newer_rows}
        added = []
        removed = []
        changed = []
        for key in sorted(newer.keys() - older.keys()):
            added.append({"key": key, "value": json.loads(newer[key]["payload_json"])})
        for key in sorted(older.keys() - newer.keys()):
            removed.append({"key": key, "value": json.loads(older[key]["payload_json"])})
        for key in sorted(older.keys() & newer.keys()):
            if older[key]["content_hash"] != newer[key]["content_hash"]:
                changed.append(
                    {
                        "key": key,
                        "before": json.loads(older[key]["payload_json"]),
                        "after": json.loads(newer[key]["payload_json"]),
                    }
                )
        return {"added": added, "removed": removed, "changed": changed}

    def export_csv(self, namespace: str, path: Path) -> int:
        records = [_flatten_record(record) for record in self.iter_records(namespace)]
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = sorted({key for record in records for key in record})
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=fieldnames or ["record"])
            writer.writeheader()
            writer.writerows(records)
        temporary.replace(path)
        return len(records)

    def import_jsonl(
        self,
        namespace: str,
        path: Path,
        key_field: str,
        *,
        run_id: int | None = None,
    ) -> dict[str, int]:
        """Import a legacy JSONL file into the current-record table."""

        prepared: list[tuple[str, dict[str, Any]]] = []
        invalid = 0
        with path.open("r", encoding="utf-8-sig") as file:
            for line in file:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    invalid += 1
                    continue
                if not isinstance(record, dict) or key_field not in record:
                    invalid += 1
                    continue
                prepared.append((str(record[key_field]), record))
        result = self.upsert_many(namespace, prepared, run_id=run_id)
        result["invalid"] = invalid
        result["read"] = len(prepared)
        return result

    def get_setting(self, key: str, default: Any = None) -> Any:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value_json FROM settings WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            return default
        return json.loads(str(row["value_json"]))

    def set_setting(self, key: str, value: Any) -> None:
        payload = json.dumps(value, ensure_ascii=False)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO settings(key, value_json, updated_at)
                VALUES(?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at = excluded.updated_at
                """,
                (key, payload, utc_now()),
            )


class SqliteStore:
    """Adapter exposing the crawler's store protocol on top of SQLite."""

    def __init__(
        self,
        database: ParserDatabase,
        namespace: str,
        key_field: str,
        *,
        refresh: bool = False,
        run_id: int | None = None,
    ) -> None:
        self.database = database
        self.namespace = namespace
        self.key_field = key_field
        self.refresh = refresh
        self.run_id = run_id

    def __contains__(self, key: object) -> bool:
        if self.refresh:
            return False
        return self.database.contains(self.namespace, key)

    def append(self, record: dict[str, Any]) -> bool:
        return self.append_many([record]) == 1

    def append_many(self, records: Iterable[dict[str, Any]]) -> int:
        prepared: list[tuple[str, dict[str, Any]]] = []
        for record in records:
            if self.key_field not in record:
                raise KeyError(f"В записи нет ключа {self.key_field!r}")
            prepared.append((str(record[self.key_field]), record))
        if not prepared:
            return 0
        self.database.upsert_many(
            self.namespace,
            prepared,
            run_id=self.run_id,
        )
        return len(prepared)

    def iter_records(self) -> Iterator[dict[str, Any]]:
        return self.database.iter_records(self.namespace)

    def records(self) -> list[dict[str, Any]]:
        return list(self.iter_records())
