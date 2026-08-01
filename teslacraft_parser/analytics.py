from __future__ import annotations

from collections import Counter
from typing import Any
from urllib.parse import unquote, urlparse

from .database import ParserDatabase


def _counter_rows(counter: Counter[str], limit: int = 20) -> list[dict[str, Any]]:
    return [
        {"label": label or "Не указано", "value": value}
        for label, value in counter.most_common(limit)
    ]


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _push_top(
    rows: list[dict[str, Any]],
    *,
    label: Any,
    value: Any,
    limit: int = 20,
) -> None:
    rows.append({"label": str(label or "Не указано"), "value": _safe_int(value)})
    rows.sort(key=lambda item: int(item["value"]), reverse=True)
    del rows[limit:]


def _link_title(url: Any) -> str:
    path = unquote(urlparse(str(url or "")).path).strip("/")
    slug = path.rsplit("/", 1)[-1] if path else ""
    return slug.rsplit(".", 1)[0].replace("-", " ") or "Без названия"


def _is_clan_index_url(url: Any) -> bool:
    return urlparse(str(url or "")).path.rstrip("/").casefold() == "/clan"


def _clan_members(clan: dict[str, Any]) -> list[dict[str, Any]]:
    valid: list[dict[str, Any]] = []
    for member in clan.get("members") or []:
        if not isinstance(member, dict) or not isinstance(member.get("score"), int):
            continue
        rank = member.get("rank")
        if not isinstance(rank, int):
            raw_columns = str(member.get("raw") or "").split("\t")
            try:
                rank = int(raw_columns[0].strip())
            except (IndexError, ValueError):
                continue
        if rank < 1 or not str(member.get("username") or "").strip():
            continue
        valid.append(member)
    return valid


def _table(
    title: str,
    rows: list[dict[str, Any]],
    columns: list[str],
    total_count: int,
    *,
    description: str = "",
) -> dict[str, Any]:
    return {
        "title": title,
        "description": description,
        "rows": rows,
        "columns": columns,
        "total_count": total_count,
    }


def namespace_tables(
    database: ParserDatabase,
    namespace: str,
    *,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Build user-facing table rows from stored records and nested datasets."""

    if limit < 1:
        raise ValueError("limit must be positive")

    if namespace == "forum:sections":
        sections: list[dict[str, Any]] = []
        for record in database.iter_records(namespace):
            for section in record.get("sections") or []:
                if isinstance(section, dict):
                    sections.append(section)
        categories = {
            str(section.get("category") or "").strip()
            for section in sections
            if section.get("category")
        }
        suspicious_categories = len(sections) > 5 and len(categories) <= 1
        rows: list[dict[str, Any]] = []
        for section in sections[:limit]:
            item = dict(section)
            if suspicious_categories:
                item["category"] = "Требуется обновить карту"
            item["level"] = (
                "Подраздел" if _safe_int(section.get("level")) >= 3 else "Раздел"
            )
            item["kind"] = (
                "Внешняя ссылка" if section.get("kind") == "link" else "Раздел форума"
            )
            rows.append(item)
        return [
            _table(
                "Разделы форума",
                rows,
                ["category", "title", "parent", "level", "kind", "node_id", "url"],
                len(sections),
                description="Каждая строка — отдельный раздел или подраздел форума.",
            )
        ]

    if namespace.startswith("forum:") and namespace.endswith(":index"):
        rows = []
        total = 0
        available_prefixes: dict[str, str] = {}
        prefix_counts: Counter[str] = Counter()
        for record in database.iter_records(namespace):
            for prefix in record.get("available_prefixes") or []:
                if isinstance(prefix, dict) and prefix.get("id"):
                    available_prefixes[str(prefix["id"])] = str(
                        prefix.get("name") or prefix["id"]
                    )
            for thread in record.get("threads") or []:
                if not isinstance(thread, dict):
                    continue
                for prefix in thread.get("prefixes") or []:
                    if isinstance(prefix, dict):
                        prefix_id = str(prefix.get("id") or "")
                        prefix_name = str(prefix.get("name") or prefix_id)
                        if prefix_id:
                            available_prefixes.setdefault(prefix_id, prefix_name)
                        prefix_counts[prefix_id or prefix_name] += 1
                total += 1
                if len(rows) < limit:
                    item = dict(thread)
                    item["page"] = record.get("page")
                    item["prefix"] = (
                        ", ".join(
                            str(prefix.get("name") or "")
                            for prefix in thread.get("prefixes") or []
                            if isinstance(prefix, dict)
                        )
                        or "Без префикса"
                    )
                    rows.append(item)
        tables = [
            _table(
                "Темы раздела",
                rows,
                [
                    "prefix",
                    "title",
                    "author",
                    "created_at",
                    "replies",
                    "views",
                    "last_poster",
                    "is_sticky",
                    "is_locked",
                    "url",
                ],
                total,
                description="Каждая строка — отдельная тема, а не страница выдачи.",
            )
        ]
        prefix_rows = [
            {
                "prefix_id": prefix_id,
                "prefix_name": name,
                "thread_count": prefix_counts[prefix_id],
            }
            for prefix_id, name in sorted(
                available_prefixes.items(),
                key=lambda item: item[1].casefold(),
            )
        ]
        if prefix_rows:
            tables.append(
                _table(
                    "Доступные префиксы",
                    prefix_rows,
                    ["prefix_name", "prefix_id", "thread_count"],
                    len(prefix_rows),
                    description=("Количество тем рассчитано по сохранённым страницам раздела."),
                )
            )
        return tables

    if namespace.startswith("forum:") and namespace.endswith(":pages"):
        pages: list[dict[str, Any]] = []
        total = 0
        for record in database.iter_records(namespace):
            usernames = [str(value) for value in record.get("usernames") or [] if value]
            total += 1
            if len(pages) < limit:
                pages.append(
                    {
                        "url": record.get("url"),
                        "messages": len(usernames),
                        "unique_authors": len(set(usernames)),
                        "authors": ", ".join(dict.fromkeys(usernames)) or "Нет данных",
                    }
                )
        return [
            _table(
                "Страницы тем",
                pages,
                ["messages", "unique_authors", "authors", "url"],
                total,
            )
        ]

    if namespace.startswith("punishments:") and namespace.endswith(":index"):
        rows = []
        total = 0
        for record in database.iter_records(namespace):
            for entry in record.get("entries") or []:
                if not isinstance(entry, dict):
                    continue
                total += 1
                if len(rows) < limit:
                    rows.append(dict(entry))
        return [
            _table(
                "Наказания",
                rows,
                [
                    "id",
                    "type",
                    "nickname",
                    "ip",
                    "occurred_at",
                    "moderator",
                    "status",
                    "detail_url",
                ],
                total,
            )
        ]

    if namespace == "clans:index":
        rows = []
        total = 0
        for record in database.iter_records(namespace):
            for url in record.get("clan_links") or []:
                if _is_clan_index_url(url):
                    continue
                total += 1
                if len(rows) < limit:
                    rows.append(
                        {
                            "page": record.get("page"),
                            "title": _link_title(url),
                            "url": url,
                        }
                    )
        return [_table("Найденные кланы", rows, ["title", "page", "url"], total)]

    if namespace == "clans:details":
        clans: list[dict[str, Any]] = []
        members: list[dict[str, Any]] = []
        clan_total = 0
        member_total = 0
        for clan in database.iter_records(namespace):
            if _is_clan_index_url(clan.get("url")):
                continue
            clan_total += 1
            clan_members = _clan_members(clan)
            if len(clans) < limit:
                clans.append(
                    {
                        "title": clan.get("title") or _link_title(clan.get("url")),
                        "member_count": len(clan_members),
                        "url": clan.get("url"),
                    }
                )
            for member in clan_members:
                if not isinstance(member, dict):
                    continue
                member_total += 1
                if len(members) < limit:
                    members.append(
                        {
                            "clan": clan.get("title") or _link_title(clan.get("url")),
                            "username": member.get("username"),
                            "score": member.get("score"),
                        }
                    )
        return [
            _table("Кланы", clans, ["title", "member_count", "url"], clan_total),
            _table(
                "Участники кланов",
                members,
                ["clan", "username", "score"],
                member_total,
            ),
        ]

    records = list(database.iter_records(namespace, limit=limit))
    total = database.record_count(namespace)
    if namespace == "members":
        columns = [
            "id",
            "username",
            "registered_at",
            "positive",
            "neutral",
            "negative",
            "status",
            "profile_url",
        ]
        title = "Игроки"
    elif namespace == "bans:historical":
        expanded = []
        for record in records:
            item = {key: value for key, value in record.items() if key != "data"}
            data = record.get("data")
            if isinstance(data, dict):
                item.update(data)
            expanded.append(item)
        records = expanded
        fields = {str(key) for record in records for key in record}
        columns = [
            "id",
            "status",
            *sorted(fields - {"id", "status", "url"})[:6],
            "url",
        ]
        title = "Исторические баны"
    elif namespace.startswith("punishments:") and namespace.endswith(":entries"):
        columns = [
            "id",
            "type",
            "nickname",
            "ip",
            "occurred_at",
            "moderator",
            "status",
            "detail_url",
        ]
        title = "Наказания"
    elif namespace.startswith("punishments:") and namespace.endswith(":details"):
        expanded = []
        for record in records:
            item = {key: value for key, value in record.items() if key != "data"}
            data = record.get("data")
            if isinstance(data, dict):
                item.update(data)
            expanded.append(item)
        records = expanded
        preferred = ["id", "type", "status", "status_text"]
        fields = {str(key) for record in records for key in record}
        columns = [
            *preferred,
            *sorted(fields - set(preferred) - {"key", "url"})[:5],
            "url",
        ]
        title = "Карточки наказаний"
    else:
        preferred = ["id", "key", "title", "username", "status", "occurred_at", "url"]
        fields = {str(key) for record in records for key in record}
        columns = [field for field in preferred if field in fields]
        columns.extend(sorted(fields - set(columns)))
        columns = columns[:8]
        title = "Записи"
    return [_table(title, records, columns, total)]


def namespace_statistics(
    database: ParserDatabase,
    namespace: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "namespace": namespace,
        "record_count": 0,
        "metrics": [],
        "series": [],
        "top": [],
    }
    if namespace == "members":
        found = 0
        top: list[dict[str, Any]] = []
        for item in database.iter_records(namespace):
            result["record_count"] += 1
            if item.get("status") != "ok":
                continue
            found += 1
            _push_top(
                top,
                label=item.get("username") or item.get("id"),
                value=item.get("positive"),
            )
        result["metrics"] = [
            {"label": "Карточек", "value": result["record_count"]},
            {"label": "Найдено игроков", "value": found},
            {"label": "Отсутствуют", "value": result["record_count"] - found},
        ]
        result["top"] = top
        return result

    if namespace.startswith("punishments:") and namespace.endswith(":entries"):
        statuses: Counter[str] = Counter()
        moderators: Counter[str] = Counter()
        for item in database.iter_records(namespace):
            result["record_count"] += 1
            statuses[str(item.get("status") or "Не указано")] += 1
            moderators[str(item.get("moderator") or "Не указано")] += 1
        result["metrics"] = [{"label": "Наказаний", "value": result["record_count"]}]
        result["series"] = _counter_rows(statuses)
        result["top"] = _counter_rows(moderators)
        return result

    if namespace.startswith("punishments:") and namespace.endswith(":index"):
        entries_count = 0
        for item in database.iter_records(namespace):
            result["record_count"] += 1
            entries_count += sum(
                1 for entry in item.get("entries") or [] if isinstance(entry, dict)
            )
        result["metrics"] = [
            {"label": "Страниц списка", "value": result["record_count"]},
            {"label": "Наказаний", "value": entries_count},
        ]
        return result

    if namespace.startswith("punishments:") and namespace.endswith(":details"):
        statuses: Counter[str] = Counter()
        reasons: Counter[str] = Counter()
        for item in database.iter_records(namespace):
            result["record_count"] += 1
            statuses[str(item.get("status") or "Не указано")] += 1
            data = item.get("data") or {}
            reason = (
                next(
                    (
                        value
                        for key, value in data.items()
                        if "причин" in str(key).casefold() or "reason" in str(key).casefold()
                    ),
                    None,
                )
                if isinstance(data, dict)
                else None
            )
            reasons[str(reason or "Не указано")] += 1
        result["metrics"] = [{"label": "Карточек", "value": result["record_count"]}]
        result["series"] = _counter_rows(statuses)
        result["top"] = _counter_rows(reasons)
        return result

    if namespace.startswith("forum:") and namespace.endswith(":index"):
        threads_count = 0
        replies = 0
        views = 0
        prefixes: Counter[str] = Counter()
        top: list[dict[str, Any]] = []
        for record in database.iter_records(namespace):
            result["record_count"] += 1
            for thread in record.get("threads") or []:
                if not isinstance(thread, dict):
                    continue
                threads_count += 1
                replies += _safe_int(thread.get("replies"))
                thread_views = _safe_int(thread.get("views"))
                views += thread_views
                thread_prefixes = thread.get("prefixes") or [{"name": "Без префикса"}]
                for prefix in thread_prefixes:
                    name = prefix.get("name") if isinstance(prefix, dict) else prefix
                    prefixes[str(name or "Без префикса")] += 1
                _push_top(
                    top,
                    label=thread.get("title") or thread.get("url"),
                    value=thread_views,
                )
        result["metrics"] = [
            {"label": "Страниц раздела", "value": result["record_count"]},
            {"label": "Тем", "value": threads_count},
            {"label": "Ответов", "value": replies},
            {"label": "Просмотров", "value": views},
        ]
        result["series"] = _counter_rows(prefixes)
        result["top"] = top
        return result

    if namespace == "clans:details":
        members_count = 0
        top: list[dict[str, Any]] = []
        for clan in database.iter_records(namespace):
            if _is_clan_index_url(clan.get("url")):
                continue
            result["record_count"] += 1
            for member in _clan_members(clan):
                members_count += 1
                _push_top(
                    top,
                    label=member.get("username") or "Неизвестно",
                    value=member.get("score"),
                )
        result["metrics"] = [
            {"label": "Кланов", "value": result["record_count"]},
            {"label": "Участников", "value": members_count},
        ]
        result["top"] = top
        return result

    if namespace == "clans:index":
        clan_links: set[str] = set()
        for record in database.iter_records(namespace):
            result["record_count"] += 1
            clan_links.update(
                str(link)
                for link in record.get("clan_links") or []
                if link and not _is_clan_index_url(link)
            )
        result["metrics"] = [
            {"label": "Страниц списка", "value": result["record_count"]},
            {"label": "Кланов", "value": len(clan_links)},
        ]
        return result

    if namespace == "bans:historical":
        statuses: Counter[str] = Counter()
        for item in database.iter_records(namespace):
            result["record_count"] += 1
            statuses[str(item.get("status") or "Не указано")] += 1
        result["metrics"] = [{"label": "Проверено ID", "value": result["record_count"]}]
        result["series"] = _counter_rows(statuses)
        return result

    if namespace == "forum:sections":
        categories: Counter[str] = Counter()
        sections: list[dict[str, Any]] = []
        for record in database.iter_records(namespace):
            result["record_count"] += 1
            for section in record.get("sections") or []:
                if isinstance(section, dict):
                    sections.append(section)
        unique_categories = {
            str(section.get("category") or "").strip()
            for section in sections
            if section.get("category")
        }
        if len(sections) > 5 and len(unique_categories) <= 1:
            categories["Требуется обновить карту"] = len(sections)
        else:
            for section in sections:
                categories[str(section.get("category") or "Без категории")] += 1
        result["metrics"] = [
            {"label": "Разделов", "value": sum(categories.values())},
            {"label": "Категорий", "value": len(categories)},
        ]
        result["series"] = _counter_rows(categories)
        return result

    if namespace.startswith("forum:") and namespace.endswith(":pages"):
        authors: Counter[str] = Counter()
        messages = 0
        for record in database.iter_records(namespace):
            result["record_count"] += 1
            usernames = [str(value) for value in record.get("usernames") or [] if value]
            messages += len(usernames)
            authors.update(usernames)
        result["metrics"] = [
            {"label": "Страниц тем", "value": result["record_count"]},
            {"label": "Сообщений", "value": messages},
            {"label": "Авторов", "value": len(authors)},
        ]
        result["top"] = _counter_rows(authors)
        return result

    statuses: Counter[str] = Counter()
    for item in database.iter_records(namespace):
        result["record_count"] += 1
        statuses[str(item.get("status") or "Запись")] += 1
    result["metrics"] = [{"label": "Записей", "value": result["record_count"]}]
    result["series"] = _counter_rows(statuses)
    return result
