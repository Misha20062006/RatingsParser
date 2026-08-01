from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any, Protocol


class RecordStore(Protocol):
    key_field: str

    def __contains__(self, key: object) -> bool: ...

    def partition_pending(self, items: Iterable[Any]) -> tuple[list[Any], int]: ...

    def append(self, record: dict[str, Any]) -> bool: ...

    def append_many(self, records: Iterable[dict[str, Any]]) -> int: ...

    def discard_many(self, keys: Iterable[Any]) -> int: ...

    def iter_records(self) -> Iterator[dict[str, Any]]: ...

    def records(self) -> list[dict[str, Any]]: ...


class JsonlStore:
    """Append-only JSONL storage with idempotent keys.

    A completed item is written as one line. On the next launch existing keys
    are loaded, so an interrupted crawl can continue without duplicating data.
    """

    def __init__(self, path: Path, key_field: str) -> None:
        self.path = path
        self.key_field = key_field
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._keys: set[str] = set()
        self._load_keys()

    def _load_keys(self) -> None:
        if not self.path.exists():
            return
        for record in self.iter_records():
            if self.key_field in record:
                self._keys.add(str(record[self.key_field]))

    def __contains__(self, key: object) -> bool:
        return str(key) in self._keys

    def partition_pending(self, items: Iterable[Any]) -> tuple[list[Any], int]:
        pending: list[Any] = []
        skipped = 0
        for item in items:
            if str(item) in self._keys:
                skipped += 1
            else:
                pending.append(item)
        return pending, skipped

    def append(self, record: dict[str, Any]) -> bool:
        return self.append_many([record]) == 1

    def append_many(self, records: Iterable[dict[str, Any]]) -> int:
        pending: list[tuple[str, str]] = []
        seen_in_batch: set[str] = set()
        for record in records:
            if self.key_field not in record:
                raise KeyError(f"В записи нет ключа {self.key_field!r}")

            key = str(record[self.key_field])
            if key in self._keys or key in seen_in_batch:
                continue
            payload = json.dumps(
                record,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            pending.append((key, payload))
            seen_in_batch.add(key)

        if not pending:
            return 0

        with self.path.open("a", encoding="utf-8", newline="\n") as file:
            for _, payload in pending:
                file.write(payload + "\n")
            file.flush()
        self._keys.update(key for key, _ in pending)
        return len(pending)

    def discard_many(self, keys: Iterable[Any]) -> int:
        removed = {str(key) for key in keys} & self._keys
        if not removed:
            return 0
        records = [
            record
            for record in self.iter_records()
            if str(record.get(self.key_field)) not in removed
        ]
        write_jsonl(self.path, records)
        self._keys.difference_update(removed)
        return len(removed)

    def iter_records(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as file:
            for line_number, line in enumerate(file, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    print(
                        f"Предупреждение: пропущена повреждённая строка "
                        f"{line_number} в {self.path}"
                    )
                    continue
                if isinstance(record, dict):
                    yield record

    def records(self) -> list[dict[str, Any]]:
        return list(self.iter_records())


def append_error(
    path: Path,
    *,
    mode: str,
    key: object,
    url: str,
    error: BaseException | str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "mode": mode,
        "key": key,
        "url": url,
        "error": str(error),
    }
    with path.open("a", encoding="utf-8", newline="\n") as file:
        file.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_text_lines(path: Path, lines: Iterable[str]) -> None:
    """Atomically replace a text report."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as file:
        for line in lines:
            file.write(line.rstrip("\n") + "\n")
    temporary.replace(path)


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    """Atomically export current records to a compatibility JSONL file."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as file:
        for record in records:
            file.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "\n"
            )
    temporary.replace(path)
