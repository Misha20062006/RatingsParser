from __future__ import annotations

import json
import sqlite3
from contextlib import closing, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .database import ParserDatabase


@dataclass(frozen=True)
class SavedJsonl:
    path: Path
    namespace: str
    key_field: str


@dataclass(frozen=True)
class GuiStoragePreparation:
    active_root: Path
    migrated_from: Path | None = None
    warning: str | None = None


def load_gui_settings(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_gui_settings(path: Path, settings: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(settings, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)


def _rebased_path(value: Any, old_root: Path, new_root: Path) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        path = Path(value).expanduser().resolve(strict=False)
        relative = path.relative_to(old_root.resolve(strict=False))
    except (OSError, ValueError):
        return None
    return new_root.resolve(strict=False) / relative


def _rebase_gui_settings(
    settings_path: Path,
    old_root: Path,
    new_root: Path,
    *,
    force: bool,
) -> bool:
    settings = load_gui_settings(settings_path)
    if not settings:
        return False
    changed = False
    for key in ("output_dir", "database_path", "profile_dir"):
        candidate = _rebased_path(settings.get(key), old_root, new_root)
        if candidate is None:
            continue
        original = Path(str(settings[key])).expanduser()
        if force or (not original.exists() and candidate.exists()):
            settings[key] = str(candidate)
            changed = True
    if changed:
        save_gui_settings(settings_path, settings)
    return changed


def prepare_gui_storage(
    preferred_root: Path,
    legacy_roots: list[Path],
) -> GuiStoragePreparation:
    """Atomically adopt an older application directory when that is safe.

    Existing non-empty directories are never merged. If an atomic rename fails,
    the old directory remains active so databases and the Chrome profile stay usable.
    """

    preferred_root = preferred_root.resolve(strict=False)
    legacy_roots = [root.resolve(strict=False) for root in legacy_roots]
    legacy_root = next((root for root in legacy_roots if root.is_dir()), None)

    if preferred_root.exists():
        if not preferred_root.is_dir():
            if legacy_root is not None:
                return GuiStoragePreparation(
                    legacy_root,
                    warning=f"Новый путь данных занят файлом: {preferred_root}",
                )
            return GuiStoragePreparation(
                preferred_root,
                warning=f"Путь данных не является папкой: {preferred_root}",
            )
        try:
            preferred_is_empty = next(preferred_root.iterdir(), None) is None
        except OSError:
            preferred_is_empty = False
        if legacy_root is not None and preferred_is_empty:
            try:
                preferred_root.rmdir()
            except OSError as error:
                return GuiStoragePreparation(
                    legacy_root,
                    warning=f"Не удалось подготовить новую папку данных: {error}",
                )
        else:
            for old_root in legacy_roots:
                if old_root.exists():
                    continue
                with suppress(OSError):
                    _rebase_gui_settings(
                        preferred_root / "gui-settings.json",
                        old_root,
                        preferred_root,
                        force=False,
                    )
            return GuiStoragePreparation(preferred_root)

    if legacy_root is None:
        return GuiStoragePreparation(preferred_root)

    try:
        legacy_root.rename(preferred_root)
    except OSError as error:
        return GuiStoragePreparation(
            legacy_root,
            warning=f"Не удалось перенести данные в {preferred_root}: {error}",
        )

    warning = None
    try:
        _rebase_gui_settings(
            preferred_root / "gui-settings.json",
            legacy_root,
            preferred_root,
            force=True,
        )
    except OSError as error:
        warning = f"Данные перенесены, но пути в настройках обновятся позже: {error}"
    return GuiStoragePreparation(preferred_root, migrated_from=legacy_root, warning=warning)


def discover_saved_jsonl(output_dir: Path) -> list[SavedJsonl]:
    """Find JSONL files generated by the parser, excluding error journals."""

    if not output_dir.is_dir():
        return []

    found: dict[Path, SavedJsonl] = {}

    def add(filename: str, namespace: str, key_field: str) -> None:
        path = output_dir / filename
        if path.is_file():
            resolved = path.resolve()
            found[resolved] = SavedJsonl(resolved, namespace, key_field)

    add("members.jsonl", "members", "id")
    add("bans.jsonl", "bans:historical", "id")
    add("forum_sections.jsonl", "forum:sections", "url")
    add("clan_index.jsonl", "clans:index", "page")
    add("clans.jsonl", "clans:details", "url")

    # The combined mode keeps these same datasets in named subdirectories.
    add("members/members.jsonl", "members", "id")
    add("bans/bans.jsonl", "bans:historical", "id")
    add("clans/clan_index.jsonl", "clans:index", "page")
    add("clans/clans.jsonl", "clans:details", "url")

    punishments_root = output_dir / "punishments"
    if punishments_root.is_dir():
        for path in punishments_root.glob("*/*/*.jsonl"):
            if path.stem not in {"index", "entries", "details"}:
                continue
            relative = path.relative_to(punishments_root)
            punishment_type, filter_slug = relative.parts[:2]
            resolved = path.resolve()
            found[resolved] = SavedJsonl(
                resolved,
                f"punishments:{punishment_type}:{filter_slug}:{path.stem}",
                "key",
            )

    for directory in output_dir.glob("forum_*"):
        if not directory.is_dir():
            continue
        slug = directory.name.removeprefix("forum_")
        for filename, kind, key_field in (
            ("forum_index.jsonl", "index", "page"),
            ("forum_pages.jsonl", "pages", "url"),
        ):
            path = directory / filename
            if path.is_file():
                resolved = path.resolve()
                found[resolved] = SavedJsonl(
                    resolved,
                    f"forum:{slug}:{kind}",
                    key_field,
                )

    return sorted(found.values(), key=lambda item: str(item.path).casefold())


def find_saved_results(candidates: list[Path]) -> Path | None:
    """Prefer a populated database, then JSONL, then an empty valid database."""

    best: tuple[tuple[int, int], Path] | None = None
    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        jsonl_bytes = 0
        for source in discover_saved_jsonl(resolved):
            try:
                jsonl_bytes += source.path.stat().st_size
            except OSError:
                continue
        rank = (2, jsonl_bytes) if jsonl_bytes else (0, 0)
        database_path = resolved / "teslacraft.db"
        if database_path.is_file():
            try:
                with closing(
                    sqlite3.connect(
                        database_path.as_uri() + "?mode=ro",
                        uri=True,
                        timeout=1,
                    )
                ) as connection:
                    table = connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'records'"
                    ).fetchone()
                    record_count = (
                        int(connection.execute("SELECT COUNT(*) FROM records").fetchone()[0])
                        if table
                        else 0
                    )
            except (OSError, sqlite3.Error):
                record_count = -1
            if record_count > 0:
                rank = (3, record_count)
            elif record_count == 0:
                rank = max(rank, (1, 0))
        if rank[0] and (best is None or rank > best[0]):
            best = (rank, resolved)
    return best[1] if best else None


def import_saved_jsonl(
    database: ParserDatabase,
    output_dir: Path,
    *,
    only_missing_namespaces: bool = False,
    known_signatures: dict[str, str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "files": 0,
        "read": 0,
        "new": 0,
        "changed": 0,
        "invalid": 0,
        "errors": [],
        "signatures": {},
    }
    existing_namespaces = (
        {str(item["namespace"]) for item in database.namespaces()}
        if only_missing_namespaces
        else set()
    )
    for source in discover_saved_jsonl(output_dir):
        try:
            stat = source.path.stat()
        except OSError as error:
            result["errors"].append(f"{source.path}: {error}")
            continue
        signature = f"{stat.st_size}:{stat.st_mtime_ns}"
        source_key = str(source.path)
        if known_signatures and known_signatures.get(source_key) == signature:
            continue
        if source.namespace in existing_namespaces:
            continue
        try:
            imported = database.import_jsonl(
                source.namespace,
                source.path,
                source.key_field,
            )
        except (OSError, ValueError) as error:
            result["errors"].append(f"{source.path}: {error}")
            continue
        result["files"] += 1
        result["signatures"][source_key] = signature
        existing_namespaces.add(source.namespace)
        for key in ("read", "new", "changed", "invalid"):
            result[key] += int(imported[key])
    return result
