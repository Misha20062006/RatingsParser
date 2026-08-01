from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlencode, urljoin, urlparse

from patchright.async_api import Locator, Page

from .browser import BrowserController
from .constants import BASE_URL
from .constants import DEFAULT_APPEALS_URL as DEFAULT_APPEALS_URL
from .constants import DEFAULT_FORUM_URL as DEFAULT_FORUM_URL
from .crawler import (
    CrawlControl,
    CrawlSummary,
    ProgressCallback,
    crawl_items,
)
from .database import ParserDatabase, SqliteStore
from .html_parsers import (
    parse_forum_index_html,
    parse_forum_sections_html,
    parse_punishment_detail_html,
    parse_punishment_index_html,
)
from .storage import (
    JsonlStore,
    RecordStore,
    write_jsonl,
    write_text_lines,
)

PUNISHMENT_PATHS = {
    "ban": "/banlist",
    "kick": "/banlist/kick",
    "mute": "/banlist/mute",
    "warn": "/banlist/warn",
}
PUNISHMENT_NAMES = {
    "ban": "баны",
    "kick": "кики",
    "mute": "муты",
    "warn": "предупреждения",
}
CLAN_INDEX_FORMAT = "clans-index-v3"
CLAN_DETAIL_FORMAT = "clans-details-v3"


def _open_store(
    *,
    database: ParserDatabase | None,
    namespace: str,
    jsonl_path: Path,
    key_field: str,
    refresh: bool,
    run_id: int | None,
) -> RecordStore:
    if database is not None:
        return SqliteStore(
            database,
            namespace,
            key_field,
            refresh=refresh,
            run_id=run_id,
        )
    return JsonlStore(jsonl_path, key_field)


def _export_store(store: RecordStore, path: Path) -> None:
    if isinstance(store, SqliteStore):
        write_jsonl(path, store.iter_records())


def _create_snapshot(
    database: ParserDatabase | None,
    namespace: str,
    snapshot_name: str | None,
    run_id: int | None,
) -> int | None:
    if database is None or snapshot_name is None:
        return None
    return database.create_snapshot(
        namespace,
        name=snapshot_name,
        run_id=run_id,
    )


def _clean_integer(value: str) -> int | None:
    match = re.search(r"[+-]?\s*\d[\d\s]*", value)
    if match is None:
        return None
    return int(match.group(0).replace(" ", ""))


async def _text_or_none(locator: Locator, timeout: int = 2_000) -> str | None:
    try:
        if await locator.count() == 0:
            return None
        return (await locator.first.inner_text(timeout=timeout)).strip()
    except Exception:
        return None


async def parse_member(page: Page, member_id: int, url: str) -> dict[str, Any]:
    username = None
    for selector in ("h3.username", ".username"):
        username = await _text_or_none(page.locator(selector))
        if username:
            break

    if not username:
        return {
            "id": member_id,
            "profile_url": url,
            "status": "missing",
            "username": None,
            "registered_at": None,
            "positive": None,
            "neutral": None,
            "negative": None,
        }

    stats_text = await _text_or_none(page.locator("div.userInfo dl.userStats, dl.userStats"))
    registered_at = None
    if stats_text:
        registration_match = re.search(
            r"На форуме с:\s*(.+?)(?:\r?\n|$)",
            stats_text,
            flags=re.IGNORECASE,
        )
        if registration_match:
            registered_at = registration_match.group(1).strip()

    rating_text = await _text_or_none(
        page.locator("div.userInfo > dl.userStats.pairsInline > dd:nth-child(8)")
    )
    if not rating_text and stats_text:
        rating_line = next(
            (line for line in stats_text.splitlines() if line.count("/") >= 2),
            None,
        )
        rating_text = rating_line

    ratings: list[int | None] = [None, None, None]
    if rating_text:
        parts = rating_text.split("/")
        if len(parts) >= 3:
            ratings = [_clean_integer(part) for part in parts[:3]]
    if any(rating is None for rating in ratings):
        raise ValueError(f"Не удалось разобрать рейтинги пользователя ID {member_id}")

    return {
        "id": member_id,
        "profile_url": url,
        "status": "ok",
        "username": username,
        "registered_at": registered_at,
        "positive": ratings[0],
        "neutral": ratings[1],
        "negative": ratings[2],
    }


def write_member_reports(output_dir: Path, store: RecordStore) -> None:
    members = [
        record
        for record in store.iter_records()
        if record.get("status") == "ok" and record.get("username")
    ]
    members.sort(key=lambda record: int(record["id"]))

    write_text_lines(
        output_dir / "users.txt",
        (
            f"{record['username']} "
            f"{record.get('positive') if record.get('positive') is not None else 0} "
            f"{record.get('neutral') if record.get('neutral') is not None else 0} "
            f"{record.get('negative') if record.get('negative') is not None else 0}"
            for record in members
        ),
    )
    write_text_lines(
        output_dir / "users_registration.txt",
        (
            f"ID [{record['id']}]. {record['username']} "
            f"{record.get('registered_at') or 'дата неизвестна'}"
            for record in members
        ),
    )

    top_specs = (
        ("positive", "users_positive_sort.txt", True),
        ("neutral", "users_neutral_sort.txt", True),
        ("negative", "users_negative_sort.txt", False),
    )
    for field, filename, reverse in top_specs:
        ranked = sorted(
            members,
            key=lambda record: (
                record.get(field) is None,
                -int(record[field] or 0) if reverse else int(record[field] or 0),
                str(record["username"]).casefold(),
            ),
        )
        write_text_lines(
            output_dir / filename,
            (
                f"{number}. {record['username']}: "
                f"{record.get(field) if record.get(field) is not None else 0}"
                for number, record in enumerate(ranked, start=1)
            ),
        )


async def run_members(
    *,
    controller: BrowserController,
    output_dir: Path,
    start_id: int,
    end_id: int,
    concurrency: int,
    delay_seconds: float,
    database: ParserDatabase | None = None,
    refresh: bool = False,
    run_id: int | None = None,
    snapshot_name: str | None = None,
    control: CrawlControl | None = None,
    on_progress: ProgressCallback | None = None,
) -> CrawlSummary:
    if start_id < 1 or end_id < start_id:
        raise ValueError("Диапазон ID пользователей указан неверно")

    output_dir.mkdir(parents=True, exist_ok=True)
    namespace = "members"
    jsonl_path = output_dir / "members.jsonl"
    store = _open_store(
        database=database,
        namespace=namespace,
        jsonl_path=jsonl_path,
        key_field="id",
        refresh=refresh,
        run_id=run_id,
    )
    summary = await crawl_items(
        mode="members",
        controller=controller,
        items=range(start_id, end_id + 1),
        url_for=lambda member_id: f"{BASE_URL}/members/.{member_id}/card",
        parse=parse_member,
        store=store,
        error_path=output_dir / "members_errors.jsonl",
        concurrency=concurrency,
        delay_seconds=delay_seconds,
        control=control,
        on_progress=on_progress,
    )
    _export_store(store, jsonl_path)
    write_member_reports(output_dir, store)
    _create_snapshot(database, namespace, snapshot_name, run_id)
    return summary


async def parse_ban(page: Page, ban_id: int, url: str) -> dict[str, Any]:
    banlist = page.locator(".banlist")
    if await banlist.count() == 0:
        return {
            "id": ban_id,
            "url": url,
            "status": "missing",
            "data": {},
        }

    is_unbanned = await page.locator(".titleBar .label-success").count() > 0
    keys = await page.locator(".banlist .left-label, .banlist .center-label").all_inner_texts()
    values = await page.locator(".banlist .right-data, .banlist .center-data").all_inner_texts()
    data = {
        key.strip(): value.strip()
        for key, value in zip(keys, values, strict=False)
        if key.strip()
    }
    return {
        "id": ban_id,
        "url": url,
        "status": "unbanned" if is_unbanned else "active",
        "data": data,
    }


async def run_bans(
    *,
    controller: BrowserController,
    output_dir: Path,
    start_id: int,
    end_id: int,
    concurrency: int,
    delay_seconds: float,
    database: ParserDatabase | None = None,
    refresh: bool = False,
    run_id: int | None = None,
    snapshot_name: str | None = None,
    control: CrawlControl | None = None,
    on_progress: ProgressCallback | None = None,
) -> CrawlSummary:
    if start_id < 1 or end_id < start_id:
        raise ValueError("Диапазон ID банов указан неверно")

    output_dir.mkdir(parents=True, exist_ok=True)
    namespace = "bans:historical"
    jsonl_path = output_dir / "bans.jsonl"
    store = _open_store(
        database=database,
        namespace=namespace,
        jsonl_path=jsonl_path,
        key_field="id",
        refresh=refresh,
        run_id=run_id,
    )
    summary = await crawl_items(
        mode="bans",
        controller=controller,
        items=range(start_id, end_id + 1),
        url_for=lambda ban_id: f"{BASE_URL}/banlist/{ban_id}",
        parse=parse_ban,
        store=store,
        error_path=output_dir / "bans_errors.jsonl",
        concurrency=concurrency,
        delay_seconds=delay_seconds,
        control=control,
        on_progress=on_progress,
    )
    _export_store(store, jsonl_path)
    _create_snapshot(database, namespace, snapshot_name, run_id)
    return summary


async def _discover_max_id_from_links(
    controller: BrowserController,
    urls: list[str],
    pattern: re.Pattern[str],
) -> int:
    discovered: list[int] = []
    page = await controller.new_worker_page()
    try:
        for url in urls:
            await controller.navigate(page, url)
            hrefs = await page.locator("a[href]").evaluate_all(
                "(links) => links.map((link) => link.getAttribute('href') || '')"
            )
            for href in hrefs:
                match = pattern.search(str(href))
                if match:
                    discovered.append(int(match.group(1)))
    finally:
        await page.close()
    if not discovered:
        raise ValueError("Не удалось автоматически определить последний ID")
    return max(discovered)


async def discover_latest_member_id(controller: BrowserController) -> int:
    return await _discover_max_id_from_links(
        controller,
        [f"{BASE_URL}/members/?type=recent", f"{BASE_URL}/forums/"],
        re.compile(r"/members/(?:[^/?#]*\.)?(\d+)(?:/|$)"),
    )


async def discover_latest_ban_id(controller: BrowserController) -> int:
    return await _discover_max_id_from_links(
        controller,
        [f"{BASE_URL}/banlist"],
        re.compile(r"/banlist/(\d+)(?:/|$)"),
    )


def _punishment_index_url(
    punishment_type: str,
    page_number: int,
    *,
    nick: str | None = None,
    moderator: str | None = None,
) -> str:
    path = PUNISHMENT_PATHS[punishment_type]
    query: dict[str, str | int] = {"page": page_number}
    if nick:
        query["searchByNick"] = nick
    if moderator:
        query["searchByBlys"] = moderator
    return f"{BASE_URL}{path}/?{urlencode(query)}"


def _punishment_entry_from_cells(
    punishment_type: str,
    cells: list[str],
    detail_href: str,
) -> dict[str, Any] | None:
    detail_match = re.search(r"/(\d+)/?$", detail_href)
    if not detail_match or not cells:
        return None

    punishment_id = int(detail_match.group(1))
    entry: dict[str, Any] = {
        "key": f"{punishment_type}:{punishment_id}",
        "type": punishment_type,
        "id": punishment_id,
        "nickname": cells[0] or None,
        "ip": None,
        "occurred_at": None,
        "moderator": None,
        "status": None,
        "detail_url": urljoin(BASE_URL + "/", detail_href),
    }
    if punishment_type in {"ban", "kick"}:
        entry["ip"] = (cells[1] or None) if len(cells) > 1 else None
        entry["occurred_at"] = (cells[2] or None) if len(cells) > 2 else None
        entry["moderator"] = (cells[3] or None) if len(cells) > 3 else None
        if punishment_type == "ban" and len(cells) > 4:
            entry["status"] = cells[4] or None
    else:
        entry["occurred_at"] = (cells[1] or None) if len(cells) > 1 else None
        entry["moderator"] = (cells[2] or None) if len(cells) > 2 else None
        if punishment_type == "mute" and len(cells) > 3:
            entry["status"] = cells[3] or None
    return entry


async def parse_punishment_index(
    page: Page,
    item: str,
    url: str,
) -> dict[str, Any]:
    punishment_type, raw_page = item.split(":", 1)
    return parse_punishment_index_html(
        await page.content(),
        punishment_type,
        int(raw_page),
        url,
    )


async def parse_punishment_detail(
    page: Page,
    key: str,
    url: str,
) -> dict[str, Any]:
    return parse_punishment_detail_html(await page.content(), key, url)


def _punishment_filter_slug(
    nick: str | None,
    moderator: str | None,
    since: str | None = None,
    until: str | None = None,
    statuses: list[str] | None = None,
) -> str:
    parts = []
    for label, value in (
        ("nick", nick),
        ("moderator", moderator),
        ("since", since),
        ("until", until),
    ):
        if value:
            cleaned = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE)
            parts.append(f"{label}_{cleaned[:40]}")
    if statuses:
        cleaned_statuses = "_".join(
            re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE) for value in statuses
        )
        parts.append(f"status_{cleaned_statuses[:60]}")
    return "__".join(parts) or "all"


def _entry_matches_punishment_filters(
    entry: dict[str, Any],
    *,
    since: str | None,
    until: str | None,
    statuses: list[str] | None,
) -> bool:
    occurred_at = str(entry.get("occurred_at") or "").strip()
    occurred_date: date | None = None
    for pattern in ("%Y-%m-%d", "%d.%m.%Y", "%d-%m-%Y"):
        try:
            occurred_date = datetime.strptime(occurred_at[:10], pattern).date()
            break
        except ValueError:
            continue
    since_date = date.fromisoformat(since) if since else None
    until_date = date.fromisoformat(until) if until else None
    if since_date and (occurred_date is None or occurred_date < since_date):
        return False
    if until_date and (occurred_date is None or occurred_date > until_date):
        return False
    if statuses:
        aliases = {
            "active": "active",
            "активен": "active",
            "активный": "active",
            "lifted": "lifted",
            "снят": "lifted",
            "снято": "lifted",
            "recorded": "recorded",
        }
        wanted = {
            aliases.get(value.strip().casefold(), value.strip().casefold())
            for value in statuses
            if value.strip()
        }
        raw_status = str(entry.get("status") or "").strip().casefold()
        current = aliases.get(raw_status, raw_status)
        if current not in wanted:
            return False
    return True


def write_punishment_reports(
    output_dir: Path,
    entry_store: RecordStore,
) -> None:
    entries = sorted(
        entry_store.iter_records(),
        key=lambda entry: int(entry["id"]),
        reverse=True,
    )
    write_text_lines(
        output_dir / "entries.txt",
        (
            "\t".join(
                [
                    str(entry.get("id", "")),
                    str(entry.get("nickname") or ""),
                    str(entry.get("occurred_at") or ""),
                    str(entry.get("moderator") or ""),
                    str(entry.get("status") or ""),
                    str(entry.get("detail_url") or ""),
                ]
            )
            for entry in entries
        ),
    )


async def run_punishments(
    *,
    controller: BrowserController,
    output_root: Path,
    punishment_types: list[str],
    start_page: int,
    end_page: int | None,
    concurrency: int,
    delay_seconds: float,
    collect_details: bool,
    max_details: int | None = None,
    nick: str | None = None,
    moderator: str | None = None,
    since: str | None = None,
    until: str | None = None,
    statuses: list[str] | None = None,
    database: ParserDatabase | None = None,
    refresh: bool = False,
    run_id: int | None = None,
    snapshot_name: str | None = None,
    control: CrawlControl | None = None,
    on_progress: ProgressCallback | None = None,
) -> list[CrawlSummary]:
    if start_page < 1 or (end_page is not None and end_page < start_page):
        raise ValueError("Диапазон страниц наказаний указан неверно")

    summaries: list[CrawlSummary] = []
    for punishment_type in punishment_types:
        if punishment_type not in PUNISHMENT_PATHS:
            raise ValueError(f"Неизвестный тип наказания: {punishment_type}")

        base_url = _punishment_index_url(
            punishment_type,
            1,
            nick=nick,
            moderator=moderator,
        )
        last_page = end_page or await discover_page_count(controller, base_url)
        if last_page < start_page:
            raise ValueError(f"Для типа {punishment_type} доступно страниц: {last_page}")
        output_dir = (
            output_root
            / "punishments"
            / punishment_type
            / _punishment_filter_slug(
                nick,
                moderator,
                since,
                until,
                statuses,
            )
        )
        output_dir.mkdir(parents=True, exist_ok=True)

        filter_slug = _punishment_filter_slug(
            nick,
            moderator,
            since,
            until,
            statuses,
        )
        index_namespace = f"punishments:{punishment_type}:{filter_slug}:index"
        index_path = output_dir / "index.jsonl"
        index_store = _open_store(
            database=database,
            namespace=index_namespace,
            jsonl_path=index_path,
            key_field="key",
            refresh=refresh,
            run_id=run_id,
        )
        index_summary = await crawl_items(
            mode=f"{punishment_type}-index",
            controller=controller,
            items=[
                f"{punishment_type}:{page_number}"
                for page_number in range(start_page, last_page + 1)
            ],
            url_for=lambda item: _punishment_index_url(
                item.split(":", 1)[0],
                int(item.split(":", 1)[1]),
                nick=nick,
                moderator=moderator,
            ),
            parse=parse_punishment_index,
            store=index_store,
            error_path=output_dir / "index_errors.jsonl",
            concurrency=concurrency,
            delay_seconds=delay_seconds,
            control=control,
            on_progress=on_progress,
        )
        summaries.append(index_summary)
        _export_store(index_store, index_path)

        entry_namespace = f"punishments:{punishment_type}:{filter_slug}:entries"
        entry_path = output_dir / "entries.jsonl"
        entry_store = _open_store(
            database=database,
            namespace=entry_namespace,
            jsonl_path=entry_path,
            key_field="key",
            refresh=refresh,
            run_id=run_id,
        )
        entry_store.append_many(
            entry
            for record in index_store.iter_records()
            for entry in record.get("entries", [])
            if _entry_matches_punishment_filters(
                entry,
                since=since,
                until=until,
                statuses=statuses,
            )
        )
        _export_store(entry_store, entry_path)
        write_punishment_reports(output_dir, entry_store)
        _create_snapshot(
            database,
            entry_namespace,
            (f"{snapshot_name}-{punishment_type}" if snapshot_name else None),
            run_id,
        )
        if not collect_details:
            continue

        entries = sorted(
            entry_store.iter_records(),
            key=lambda entry: int(entry["id"]),
            reverse=True,
        )
        if max_details is not None:
            entries = entries[:max_details]
        entry_by_key = {str(entry["key"]): entry for entry in entries}
        detail_namespace = f"punishments:{punishment_type}:{filter_slug}:details"
        detail_path = output_dir / "details.jsonl"
        detail_store = _open_store(
            database=database,
            namespace=detail_namespace,
            jsonl_path=detail_path,
            key_field="key",
            refresh=refresh,
            run_id=run_id,
        )
        detail_summary = await crawl_items(
            mode=f"{punishment_type}-details",
            controller=controller,
            items=list(entry_by_key),
            url_for=lambda key, entries_by_key=entry_by_key: str(
                entries_by_key[key]["detail_url"]
            ),
            parse=parse_punishment_detail,
            store=detail_store,
            error_path=output_dir / "details_errors.jsonl",
            concurrency=concurrency,
            delay_seconds=delay_seconds,
            control=control,
            on_progress=on_progress,
        )
        summaries.append(detail_summary)
        _export_store(detail_store, detail_path)

    return summaries


async def discover_page_count(
    controller: BrowserController,
    url: str,
) -> int:
    page = await controller.new_worker_page()
    try:
        await controller.navigate(page, url)
        candidates: list[str] = []
        for selector in (
            ".pageNavHeader",
            "#content .PageNav",
            "#content > div:nth-child(2) span",
        ):
            candidates.extend(await page.locator(selector).all_inner_texts())
        numbers: list[int] = []
        for text in candidates:
            numbers.extend(int(value) for value in re.findall(r"\d+", text))
        return max(numbers, default=1)
    finally:
        await page.close()


async def parse_clan_index(
    page: Page,
    page_number: int,
    url: str,
) -> dict[str, Any]:
    hrefs = await page.locator("a.minigame-link").evaluate_all(
        "(elements) => elements.map((element) => element.getAttribute('href'))"
    )
    links = sorted(
        {
            absolute
            for href in hrefs
            if isinstance(href, str) and href
            for absolute in [urljoin(BASE_URL + "/", href)]
            if urlparse(absolute).path.rstrip("/").casefold() != "/clan"
            and urlparse(absolute).path.casefold().startswith("/clan/")
        }
    )
    if not links:
        raise RuntimeError(
            "Страница списка кланов не содержит ссылок. "
            "Возможно, она не успела загрузиться; пустой результат не сохранён."
        )
    return {
        "page": page_number,
        "url": url,
        "clan_links": links,
        "_complete": CLAN_INDEX_FORMAT,
    }


async def parse_clan(page: Page, clan_url: str, url: str) -> dict[str, Any]:
    title = await _text_or_none(page.locator("h1, .titleBar h1"))
    rows = page.locator("#content table tbody tr")
    members: list[dict[str, Any]] = []
    row_count = await rows.count()
    for index in range(row_count):
        row = rows.nth(index)
        cells = [value.strip() for value in await row.locator("td").all_inner_texts()]
        raw = (await row.inner_text()).strip()
        rank = _clean_integer(cells[0]) if len(cells) >= 4 else None
        username = cells[1] if len(cells) >= 4 else None
        role = cells[2] if len(cells) >= 4 else None
        score = _clean_integer(cells[3]) if len(cells) >= 4 else None
        if rank is not None and username and score is not None:
            members.append(
                {
                    "rank": rank,
                    "username": username,
                    "role": role,
                    "score": score,
                    "raw": raw,
                }
            )
    if not title:
        raise RuntimeError(
            "Карточка клана загрузилась без названия; неполная запись не сохранена."
        )
    if not members:
        raise RuntimeError(
            "Карточка клана не содержит распознанных участников; "
            "неполная запись не сохранена."
        )
    return {
        "url": clan_url,
        "title": title,
        "members": members,
        "_complete": CLAN_DETAIL_FORMAT,
    }


def _discard_incomplete_clan_records(
    store: RecordStore,
    *,
    expected_format: str,
) -> int:
    """Remove legacy/partial clan rows so resume cannot mistake them for complete work."""

    invalid: list[Any] = []
    for record in store.iter_records():
        key = record.get(store.key_field)
        if key is None:
            continue
        if record.get("_complete") != expected_format:
            invalid.append(key)
            continue
        if expected_format == CLAN_INDEX_FORMAT:
            links = record.get("clan_links")
            if not isinstance(links, list) or not links:
                invalid.append(key)
        else:
            title = record.get("title")
            members = record.get("members")
            if (
                not isinstance(title, str)
                or not title.strip()
                or not isinstance(members, list)
                or not members
            ):
                invalid.append(key)
    removed = store.discard_many(invalid)
    if removed:
        print(f"[clans] Повторно проверяем неполные старые записи: {removed}.")
    return removed


def write_clan_reports(output_dir: Path, store: RecordStore) -> None:
    scores: dict[str, int] = {}
    for clan in store.iter_records():
        for member in clan.get("members", []):
            username = member.get("username")
            score = member.get("score")
            if username and isinstance(score, int):
                scores[username] = score
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    write_text_lines(
        output_dir / "users_sort_clan_score.txt",
        (
            f"{number}. {username}: {score}"
            for number, (username, score) in enumerate(ranked, start=1)
        ),
    )


async def run_clans(
    *,
    controller: BrowserController,
    output_dir: Path,
    start_page: int,
    end_page: int | None,
    concurrency: int,
    delay_seconds: float,
    collect_members: bool = True,
    max_clans: int | None = None,
    database: ParserDatabase | None = None,
    refresh: bool = False,
    run_id: int | None = None,
    snapshot_name: str | None = None,
    control: CrawlControl | None = None,
    on_progress: ProgressCallback | None = None,
) -> tuple[CrawlSummary, CrawlSummary | None]:
    output_dir.mkdir(parents=True, exist_ok=True)
    end_page = end_page or await discover_page_count(
        controller,
        f"{BASE_URL}/clan",
    )
    if start_page < 1 or end_page < start_page:
        raise ValueError("Диапазон страниц кланов указан неверно")

    index_namespace = "clans:index"
    index_path = output_dir / "clan_index.jsonl"
    index_store = _open_store(
        database=database,
        namespace=index_namespace,
        jsonl_path=index_path,
        key_field="page",
        refresh=refresh,
        run_id=run_id,
    )
    _discard_incomplete_clan_records(
        index_store,
        expected_format=CLAN_INDEX_FORMAT,
    )
    try:
        index_summary = await crawl_items(
            mode="clan-index",
            controller=controller,
            items=range(start_page, end_page + 1),
            url_for=lambda page_number: f"{BASE_URL}/clan/?page={page_number}",
            parse=parse_clan_index,
            store=index_store,
            error_path=output_dir / "clan_index_errors.jsonl",
            concurrency=concurrency,
            delay_seconds=delay_seconds,
            control=control,
            on_progress=on_progress,
        )
    finally:
        _export_store(index_store, index_path)

    clan_links = sorted(
        {link for record in index_store.iter_records() for link in record.get("clan_links", [])}
    )
    if max_clans is not None:
        clan_links = clan_links[:max_clans]
    write_text_lines(output_dir / "clan_links.txt", clan_links)
    if not collect_members:
        return index_summary, None

    clan_namespace = "clans:details"
    clan_path = output_dir / "clans.jsonl"
    clan_store = _open_store(
        database=database,
        namespace=clan_namespace,
        jsonl_path=clan_path,
        key_field="url",
        refresh=refresh,
        run_id=run_id,
    )
    _discard_incomplete_clan_records(
        clan_store,
        expected_format=CLAN_DETAIL_FORMAT,
    )
    try:
        clan_summary = await crawl_items(
            mode="clans",
            controller=controller,
            items=clan_links,
            url_for=lambda clan_url: clan_url,
            parse=parse_clan,
            store=clan_store,
            error_path=output_dir / "clans_errors.jsonl",
            concurrency=concurrency,
            delay_seconds=delay_seconds,
            control=control,
            on_progress=on_progress,
        )
    finally:
        _export_store(clan_store, clan_path)
    write_clan_reports(output_dir, clan_store)
    _create_snapshot(database, clan_namespace, snapshot_name, run_id)
    return index_summary, clan_summary


def _forum_slug(forum_url: str) -> str:
    path = unquote(urlparse(forum_url).path).strip("/")
    raw = path.split("/")[-1] if path else "forum"
    return re.sub(r"[^\w.-]+", "_", raw, flags=re.UNICODE)[:80] or "forum"


def _page_url(base_url: str, page_number: int) -> str:
    if page_number == 1:
        return base_url
    return base_url.rstrip("/") + f"/page-{page_number}"


async def parse_forum_sections(
    page: Page,
    item: str,
    url: str,
) -> dict[str, Any]:
    record = parse_forum_sections_html(await page.content(), url)
    if not record.get("sections"):
        raise RuntimeError(
            "Карта форума загрузилась без разделов; пустой результат не сохранён."
        )
    return record


def write_forum_section_reports(
    output_dir: Path,
    store: RecordStore,
) -> None:
    sections = [
        section for record in store.iter_records() for section in record.get("sections", [])
    ]
    write_text_lines(
        output_dir / "forum_sections.txt",
        (
            (
                f"{section.get('category', '')} > "
                + (f"{section.get('parent')} > " if section.get("parent") else "")
                + f"{section.get('title', '')} > {section.get('url', '')}"
            )
            for section in sections
        ),
    )


async def run_forum_sections(
    *,
    controller: BrowserController,
    output_dir: Path,
    database: ParserDatabase | None = None,
    refresh: bool = False,
    run_id: int | None = None,
    snapshot_name: str | None = None,
    control: CrawlControl | None = None,
    on_progress: ProgressCallback | None = None,
) -> CrawlSummary:
    output_dir.mkdir(parents=True, exist_ok=True)
    namespace = "forum:sections"
    jsonl_path = output_dir / "forum_sections.jsonl"
    store = _open_store(
        database=database,
        namespace=namespace,
        jsonl_path=jsonl_path,
        key_field="url",
        refresh=True,
        run_id=run_id,
    )
    summary = await crawl_items(
        mode="forum-sections",
        controller=controller,
        items=[f"{BASE_URL}/forums/"],
        url_for=lambda url: url,
        parse=parse_forum_sections,
        store=store,
        error_path=output_dir / "forum_sections_errors.jsonl",
        concurrency=1,
        delay_seconds=0,
        control=control,
        on_progress=on_progress,
    )
    _export_store(store, jsonl_path)
    write_forum_section_reports(output_dir, store)
    _create_snapshot(database, namespace, snapshot_name, run_id)
    return summary


async def parse_forum_probe(
    page: Page,
    section_id: int,
    url: str,
) -> dict[str, Any]:
    title = await _text_or_none(page.locator("#content h1, h1"))
    actual_url = page.url
    path = urlparse(actual_url).path.rstrip("/").casefold()
    normalized_title = (title or "").strip().casefold()
    error_markers = (
        "ошибка",
        "страница не найдена",
        "запрошенная страница не найдена",
        "requested page could not be found",
        "oops",
    )
    valid = (
        path.startswith("/forums/")
        and path != "/forums"
        and bool(normalized_title)
        and not any(marker in normalized_title for marker in error_markers)
    )
    return {
        "id": section_id,
        "requested_url": url,
        "url": actual_url if valid else url,
        "title": title,
        "status": "ok" if valid else "missing",
    }


async def run_forum_discovery(
    *,
    controller: BrowserController,
    output_dir: Path,
    start_id: int,
    end_id: int,
    concurrency: int,
    delay_seconds: float,
    database: ParserDatabase | None = None,
    refresh: bool = False,
    run_id: int | None = None,
    control: CrawlControl | None = None,
    on_progress: ProgressCallback | None = None,
) -> tuple[CrawlSummary, list[str]]:
    if start_id < 1 or end_id < start_id:
        raise ValueError("Диапазон ID разделов форума указан неверно")
    output_dir.mkdir(parents=True, exist_ok=True)
    namespace = "forum:discovery"
    jsonl_path = output_dir / "forum_discovery.jsonl"
    store = _open_store(
        database=database,
        namespace=namespace,
        jsonl_path=jsonl_path,
        key_field="id",
        refresh=refresh,
        run_id=run_id,
    )
    try:
        summary = await crawl_items(
            mode="forum-discovery",
            controller=controller,
            items=range(start_id, end_id + 1),
            url_for=lambda section_id: f"{BASE_URL}/forums/{section_id}/",
            parse=parse_forum_probe,
            store=store,
            error_path=output_dir / "forum_discovery_errors.jsonl",
            concurrency=concurrency,
            delay_seconds=delay_seconds,
            control=control,
            on_progress=on_progress,
        )
    finally:
        _export_store(store, jsonl_path)
    urls = sorted(
        {
            str(record["url"])
            for record in store.iter_records()
            if record.get("status") == "ok" and record.get("url")
        }
    )
    write_text_lines(output_dir / "forum_discovered_sections.txt", urls)
    return summary, urls


async def parse_forum_index(
    page: Page,
    page_number: int,
    url: str,
) -> dict[str, Any]:
    return parse_forum_index_html(await page.content(), page_number, url)


async def parse_forum_page(
    page: Page,
    page_url: str,
    url: str,
) -> dict[str, Any]:
    usernames = [
        username.strip()
        for username in await page.locator(".uix_userTextInner .username").all_inner_texts()
        if username.strip()
    ]
    return {"url": page_url, "usernames": usernames}


def write_forum_reports(
    output_dir: Path,
    *,
    index_store: RecordStore,
    page_store: RecordStore | None,
    without_prefix: bool,
    prefix_filters: list[str] | None,
) -> None:
    threads_by_url: dict[str, dict[str, Any]] = {}
    prefix_counts: Counter[tuple[str, str]] = Counter()
    available_prefixes: dict[str, str] = {}
    for record in index_store.iter_records():
        for prefix in record.get("available_prefixes", []):
            prefix_id = str(prefix.get("id", "")).strip()
            prefix_name = str(prefix.get("name", "")).strip()
            if prefix_id and prefix_name:
                available_prefixes[prefix_id] = prefix_name
        for thread in record.get("threads", []):
            for prefix in thread.get("prefixes", []):
                prefix_counts[
                    (
                        str(prefix.get("id") or ""),
                        str(prefix.get("name") or ""),
                    )
                ] += 1
            if not _thread_matches_prefixes(
                thread,
                prefix_filters=prefix_filters,
                without_prefix=without_prefix,
            ):
                continue
            threads_by_url[str(thread["url"])] = thread

    threads = sorted(
        threads_by_url.values(),
        key=lambda thread: (str(thread.get("title", "")).casefold(), thread["url"]),
    )
    write_text_lines(
        output_dir / "links.txt",
        (
            (
                (
                    "["
                    + " | ".join(
                        str(prefix.get("name", "")) for prefix in thread.get("prefixes", [])
                    )
                    + "] "
                )
                if thread.get("prefixes")
                else ""
            )
            + f"{thread.get('title', '')} > {thread['url']}"
            for thread in threads
        ),
    )
    all_prefixes = {
        (prefix_id, prefix_name) for prefix_id, prefix_name in available_prefixes.items()
    } | set(prefix_counts)
    write_text_lines(
        output_dir / "prefixes.txt",
        (
            f"{prefix_id}\t{prefix_name}\tтем на просмотренных страницах: "
            f"{prefix_counts[(prefix_id, prefix_name)]}"
            for prefix_id, prefix_name in sorted(
                all_prefixes,
                key=lambda value: (value[1].casefold(), value[0]),
            )
        ),
    )

    if page_store is None:
        return
    counts: Counter[str] = Counter()
    for record in page_store.iter_records():
        counts.update(record.get("usernames", []))
    write_text_lines(
        output_dir / "users_top.txt",
        (
            f"{number}. {username}: {messages}"
            for number, (username, messages) in enumerate(
                counts.most_common(),
                start=1,
            )
        ),
    )


def _thread_matches_prefixes(
    thread: dict[str, Any],
    *,
    prefix_filters: list[str] | None,
    without_prefix: bool,
) -> bool:
    prefixes = thread.get("prefixes", [])
    has_prefix = bool(prefixes) or bool(thread.get("has_prefix"))
    if not prefix_filters and not without_prefix:
        return True
    if without_prefix and not has_prefix:
        return True
    if not prefix_filters:
        return False

    wanted = {value.strip().casefold() for value in prefix_filters if value.strip()}
    return any(
        str(prefix.get("id") or "").casefold() in wanted
        or str(prefix.get("name") or "").casefold() in wanted
        for prefix in prefixes
    )


async def run_forum(
    *,
    controller: BrowserController,
    output_root: Path,
    forum_url: str,
    start_page: int,
    end_page: int | None,
    concurrency: int,
    delay_seconds: float,
    collect_usernames: bool,
    without_prefix: bool = False,
    prefix_filters: list[str] | None = None,
    max_thread_pages: int | None = None,
    database: ParserDatabase | None = None,
    refresh: bool = False,
    run_id: int | None = None,
    snapshot_name: str | None = None,
    control: CrawlControl | None = None,
    on_progress: ProgressCallback | None = None,
) -> tuple[CrawlSummary, CrawlSummary | None]:
    output_dir = output_root / f"forum_{_forum_slug(forum_url)}"
    output_dir.mkdir(parents=True, exist_ok=True)
    end_page = end_page or await discover_page_count(controller, forum_url)
    if start_page < 1 or end_page < start_page:
        raise ValueError("Диапазон страниц форума указан неверно")

    slug = _forum_slug(forum_url)
    index_namespace = f"forum:{slug}:index"
    index_path = output_dir / "forum_index.jsonl"
    index_store = _open_store(
        database=database,
        namespace=index_namespace,
        jsonl_path=index_path,
        key_field="page",
        refresh=refresh,
        run_id=run_id,
    )
    index_summary = await crawl_items(
        mode="forum-index",
        controller=controller,
        items=range(start_page, end_page + 1),
        url_for=lambda page_number: _page_url(forum_url, page_number),
        parse=parse_forum_index,
        store=index_store,
        error_path=output_dir / "forum_index_errors.jsonl",
        concurrency=concurrency,
        delay_seconds=delay_seconds,
        control=control,
        on_progress=on_progress,
    )
    _export_store(index_store, index_path)

    page_summary = None
    page_store = None
    if collect_usernames:
        thread_pages: list[str] = []
        seen: set[str] = set()
        for record in index_store.iter_records():
            for thread in record.get("threads", []):
                if not _thread_matches_prefixes(
                    thread,
                    prefix_filters=prefix_filters,
                    without_prefix=without_prefix,
                ):
                    continue
                for page_number in range(1, int(thread.get("total_pages", 1)) + 1):
                    page_url = _page_url(str(thread["url"]), page_number)
                    if page_url not in seen:
                        seen.add(page_url)
                        thread_pages.append(page_url)
        if max_thread_pages is not None:
            thread_pages = thread_pages[:max_thread_pages]

        page_namespace = f"forum:{slug}:pages"
        page_path = output_dir / "forum_pages.jsonl"
        page_store = _open_store(
            database=database,
            namespace=page_namespace,
            jsonl_path=page_path,
            key_field="url",
            refresh=refresh,
            run_id=run_id,
        )
        page_summary = await crawl_items(
            mode="forum-pages",
            controller=controller,
            items=thread_pages,
            url_for=lambda page_url: page_url,
            parse=parse_forum_page,
            store=page_store,
            error_path=output_dir / "forum_pages_errors.jsonl",
            concurrency=concurrency,
            delay_seconds=delay_seconds,
            control=control,
            on_progress=on_progress,
        )
        _export_store(page_store, page_path)

    write_forum_reports(
        output_dir,
        index_store=index_store,
        page_store=page_store,
        without_prefix=without_prefix,
        prefix_filters=prefix_filters,
    )
    _create_snapshot(database, index_namespace, snapshot_name, run_id)
    return index_summary, page_summary
