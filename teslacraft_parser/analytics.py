from __future__ import annotations

from collections import Counter
from typing import Any

from .database import ParserDatabase


def dashboard_summary(database: ParserDatabase) -> dict[str, Any]:
    namespaces = database.namespaces()
    runs = database.list_runs(limit=10)
    snapshots = database.list_snapshots()
    return {
        "records": sum(int(item["record_count"]) for item in namespaces),
        "namespaces": len(namespaces),
        "runs": len(runs),
        "snapshots": len(snapshots),
        "namespace_rows": namespaces,
        "recent_runs": runs,
    }


def _counter_rows(counter: Counter[str], limit: int = 20) -> list[dict[str, Any]]:
    return [
        {"label": label or "Не указано", "value": value}
        for label, value in counter.most_common(limit)
    ]


def namespace_statistics(
    database: ParserDatabase,
    namespace: str,
) -> dict[str, Any]:
    records = list(database.iter_records(namespace))
    result: dict[str, Any] = {
        "namespace": namespace,
        "record_count": len(records),
        "metrics": [],
        "series": [],
        "top": [],
    }
    if namespace == "members":
        valid = [item for item in records if item.get("status") == "ok"]
        result["metrics"] = [
            {"label": "Карточек", "value": len(records)},
            {"label": "Найдено игроков", "value": len(valid)},
            {"label": "Отсутствуют", "value": len(records) - len(valid)},
        ]
        result["top"] = [
            {
                "label": str(item.get("username") or item.get("id")),
                "value": int(item.get("positive") or 0),
            }
            for item in sorted(
                valid,
                key=lambda value: int(value.get("positive") or 0),
                reverse=True,
            )[:20]
        ]
        return result

    if namespace.startswith("punishments:") and namespace.endswith(":entries"):
        statuses = Counter(str(item.get("status") or "Не указано") for item in records)
        moderators = Counter(str(item.get("moderator") or "Не указано") for item in records)
        result["metrics"] = [{"label": "Наказаний", "value": len(records)}]
        result["series"] = _counter_rows(statuses)
        result["top"] = _counter_rows(moderators)
        return result

    if namespace.startswith("punishments:") and namespace.endswith(":details"):
        statuses = Counter(str(item.get("status") or "Не указано") for item in records)
        reasons: Counter[str] = Counter()
        for item in records:
            data = item.get("data") or {}
            reason = next(
                (
                    value
                    for key, value in data.items()
                    if "причин" in str(key).casefold() or "reason" in str(key).casefold()
                ),
                None,
            )
            reasons[str(reason or "Не указано")] += 1
        result["metrics"] = [{"label": "Карточек", "value": len(records)}]
        result["series"] = _counter_rows(statuses)
        result["top"] = _counter_rows(reasons)
        return result

    if namespace.startswith("forum:") and namespace.endswith(":index"):
        threads = [thread for record in records for thread in record.get("threads", [])]
        prefixes = Counter(
            str(prefix.get("name") or "Без префикса")
            for thread in threads
            for prefix in (thread.get("prefixes") or [{"name": "Без префикса"}])
        )
        result["metrics"] = [
            {"label": "Страниц раздела", "value": len(records)},
            {"label": "Тем", "value": len(threads)},
            {
                "label": "Ответов",
                "value": sum(int(thread.get("replies") or 0) for thread in threads),
            },
            {
                "label": "Просмотров",
                "value": sum(int(thread.get("views") or 0) for thread in threads),
            },
        ]
        result["series"] = _counter_rows(prefixes)
        result["top"] = [
            {
                "label": str(thread.get("title") or thread.get("url")),
                "value": int(thread.get("views") or 0),
            }
            for thread in sorted(
                threads,
                key=lambda value: int(value.get("views") or 0),
                reverse=True,
            )[:20]
        ]
        return result

    if namespace == "clans:details":
        members = [member for clan in records for member in clan.get("members", [])]
        result["metrics"] = [
            {"label": "Кланов", "value": len(records)},
            {"label": "Участников", "value": len(members)},
        ]
        result["top"] = [
            {
                "label": str(member.get("username") or "Неизвестно"),
                "value": int(member.get("score") or 0),
            }
            for member in sorted(
                members,
                key=lambda value: int(value.get("score") or 0),
                reverse=True,
            )[:20]
        ]
        return result

    if namespace == "bans:historical":
        statuses = Counter(str(item.get("status") or "Не указано") for item in records)
        result["metrics"] = [{"label": "Проверено ID", "value": len(records)}]
        result["series"] = _counter_rows(statuses)
        return result

    if namespace == "forum:sections":
        categories = Counter(
            str(section.get("category") or "Без категории")
            for record in records
            for section in record.get("sections", [])
        )
        result["metrics"] = [
            {"label": "Разделов", "value": sum(categories.values())},
            {"label": "Категорий", "value": len(categories)},
        ]
        result["series"] = _counter_rows(categories)
        return result

    statuses = Counter(str(item.get("status") or "Запись") for item in records)
    result["metrics"] = [{"label": "Записей", "value": len(records)}]
    result["series"] = _counter_rows(statuses)
    return result
