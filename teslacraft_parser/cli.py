from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from contextlib import suppress
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .browser import BrowserController
from .constants import (
    DEFAULT_APPEALS_URL,
    DEFAULT_FORUM_SCAN_END_ID,
    DEFAULT_FORUM_URL,
)
from .crawler import (
    CrawlCancelled,
    CrawlControl,
    CrawlSummary,
    ProgressCallback,
)
from .database import ParserDatabase
from .modes import (
    PUNISHMENT_PATHS,
    discover_latest_ban_id,
    discover_latest_member_id,
    run_bans,
    run_clans,
    run_forum,
    run_forum_discovery,
    run_forum_sections,
    run_members,
    run_punishments,
)


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("значение должно быть больше нуля")
    return number


def _non_negative_float(value: str) -> float:
    number = float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("значение не может быть отрицательным")
    return number


def _iso_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise argparse.ArgumentTypeError("дата должна быть в формате ГГГГ-ММ-ДД") from error


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results"),
        help="папка результатов (по умолчанию: results)",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="SQLite-база (по умолчанию: OUTPUT-DIR/teslacraft.db)",
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=Path("user-data"),
        help="постоянный профиль Chrome (по умолчанию: user-data)",
    )
    parser.add_argument(
        "--concurrency",
        type=_positive_int,
        default=5,
        help="число одновременных страниц; безопасное значение — 3–5",
    )
    parser.add_argument(
        "--delay",
        type=_non_negative_float,
        default=0.25,
        help="пауза между пакетами в секундах",
    )
    parser.add_argument(
        "--retries",
        type=_positive_int,
        default=3,
        help="попыток загрузки каждой страницы",
    )
    parser.add_argument(
        "--request-timeout",
        type=_positive_int,
        default=60,
        help="таймаут страницы в секундах",
    )
    parser.add_argument(
        "--captcha-timeout",
        type=_positive_int,
        default=300,
        help="сколько секунд ждать ручного прохождения Cloudflare",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="повторно открыть уже сохранённые страницы и обновить данные",
    )
    parser.add_argument(
        "--snapshot",
        nargs="?",
        const="auto",
        metavar="NAME",
        help="создать снимок результата; NAME можно не указывать",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="показать план без открытия сайта и записи данных",
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="TeslaCraftParser",
        description=(
            "Универсальный сборщик открытой статистики форума TeslaCraft. "
            "Повторный запуск продолжает работу, --refresh обновляет данные."
        ),
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    members = subparsers.add_parser(
        "members", help="ники, даты регистрации и все виды рейтинга"
    )
    _add_common_options(members)
    members.add_argument("--start-id", type=_positive_int, default=1)
    members.add_argument(
        "--end-id",
        type=_positive_int,
        help="последний ID; если не указан, определяется автоматически",
    )

    bans = subparsers.add_parser("bans", help="исторический бан-лист")
    _add_common_options(bans)
    bans.add_argument("--start-id", type=_positive_int, default=1)
    bans.add_argument(
        "--end-id",
        type=_positive_int,
        help="последний ID; если не указан, определяется автоматически",
    )

    punishments = subparsers.add_parser(
        "punishments",
        help="индексы и карточки банов, киков, мутов и предупреждений",
    )
    _add_common_options(punishments)
    punishments.add_argument(
        "--type",
        dest="punishment_types",
        action="append",
        choices=[*PUNISHMENT_PATHS, "all"],
        help="тип наказания; можно повторить (по умолчанию: all)",
    )
    punishments.add_argument("--start-page", type=_positive_int, default=1)
    punishments.add_argument("--end-page", type=_positive_int)
    punishments.add_argument("--index-only", action="store_true")
    punishments.add_argument("--max-details", type=_positive_int)
    punishments.add_argument("--nick", help="поиск по нику игрока")
    punishments.add_argument("--moderator", help="поиск по нику блюстителя")
    punishments.add_argument("--since", type=_iso_date, help="дата от YYYY-MM-DD")
    punishments.add_argument("--until", type=_iso_date, help="дата до YYYY-MM-DD")
    punishments.add_argument(
        "--include-historical",
        action="store_true",
        help="дополнительно собрать исторический архив банов по ID",
    )
    punishments.add_argument(
        "--historical-only",
        action="store_true",
        help="собрать только исторический архив банов",
    )
    punishments.add_argument("--historical-start-id", type=_positive_int, default=1)
    punishments.add_argument("--historical-end-id", type=_positive_int)
    punishments.add_argument(
        "--status",
        dest="statuses",
        action="append",
        help="статус (например: Активен или Снят); можно повторить",
    )

    clans = subparsers.add_parser("clans", help="список кланов, участники и клановые очки")
    _add_common_options(clans)
    clans.add_argument("--start-page", type=_positive_int, default=1)
    clans.add_argument("--end-page", type=_positive_int)
    clans.add_argument("--index-only", action="store_true")
    clans.add_argument("--max-clans", type=_positive_int)

    forum = subparsers.add_parser("forum", help="темы раздела, префиксы и статистика авторов")
    _add_common_options(forum)
    forum.add_argument("--forum-url", default=DEFAULT_FORUM_URL)
    forum.add_argument("--start-page", type=_positive_int, default=1)
    forum.add_argument("--end-page", type=_positive_int)
    forum.add_argument("--index-only", action="store_true")
    forum.add_argument("--max-thread-pages", type=_positive_int)
    forum.add_argument(
        "--all-sections",
        action="store_true",
        help="найти существующие разделы перебором ID и обработать каждый",
    )
    forum.add_argument("--section-start-id", type=_positive_int, default=1)
    forum.add_argument(
        "--section-end-id",
        type=_positive_int,
        default=DEFAULT_FORUM_SCAN_END_ID,
    )
    forum.add_argument(
        "--prefix",
        dest="prefix_filters",
        action="append",
        help="точное название или ID префикса; можно повторить",
    )
    forum.add_argument(
        "--without-prefix",
        action="store_true",
        help="добавить темы без префикса к выбранным фильтрам",
    )
    forum.add_argument(
        "--prefixes-only",
        action="store_true",
        help="прочитать список префиксов только с первой страницы",
    )

    sections = subparsers.add_parser(
        "sections", help="карта всех разделов и подразделов форума"
    )
    _add_common_options(sections)

    appeals = subparsers.add_parser("appeals", help="темы апелляций без префикса")
    _add_common_options(appeals)
    appeals.add_argument("--forum-url", default=DEFAULT_APPEALS_URL)
    appeals.add_argument("--start-page", type=_positive_int, default=1)
    appeals.add_argument("--end-page", type=_positive_int)

    all_modes = subparsers.add_parser("all", help="последовательно запустить основные режимы")
    _add_common_options(all_modes)
    all_modes.add_argument(
        "--members-end-id",
        type=_positive_int,
        help="последний ID пользователя; иначе определяется автоматически",
    )
    all_modes.add_argument(
        "--bans-end-id",
        type=_positive_int,
        help="последний ID бана; --include-bans включает автоопределение",
    )
    all_modes.add_argument("--include-bans", action="store_true")
    all_modes.add_argument("--skip-clans", action="store_true")
    all_modes.add_argument("--forum-url")
    all_modes.add_argument("--forum-end-page", type=_positive_int)
    all_modes.add_argument("--forum-max-thread-pages", type=_positive_int)

    export = subparsers.add_parser("export", help="экспорт SQLite в CSV")
    export.add_argument("--database", type=Path, default=Path("results/teslacraft.db"))
    export.add_argument("--namespace", required=True)
    export.add_argument("--csv", type=Path, required=True)

    snapshots = subparsers.add_parser("snapshots", help="список или сравнение снимков")
    snapshots.add_argument("--database", type=Path, default=Path("results/teslacraft.db"))
    snapshots.add_argument("--namespace")
    snapshots.add_argument(
        "--compare",
        nargs=2,
        type=_positive_int,
        metavar=("OLDER_ID", "NEWER_ID"),
    )
    snapshots.add_argument("--json", type=Path, help="сохранить сравнение в JSON")

    migrate = subparsers.add_parser("migrate", help="перенести прежний JSONL-файл в SQLite")
    migrate.add_argument("--database", type=Path, default=Path("results/teslacraft.db"))
    migrate.add_argument("--namespace", required=True)
    migrate.add_argument("--jsonl", type=Path, required=True)
    migrate.add_argument("--key-field", required=True)
    return parser


def _ask_positive(prompt: str, default: int | None = None) -> str:
    suffix = f" [{default}]" if default is not None else ""
    while True:
        value = input(f"{prompt}{suffix}: ").strip()
        if not value and default is not None:
            return str(default)
        if value.isdigit() and int(value) > 0:
            return value
        print("Введите целое число больше нуля.")


def interactive_arguments() -> list[str]:
    print(
        "\nTeslaCraft Parser\n"
        "1. Пользователи: ники, даты регистрации и рейтинги\n"
        "2. Все наказания: баны, кики, муты, предупреждения\n"
        "3. Исторические баны по диапазону ID\n"
        "4. Кланы и клановые очки\n"
        "5. Раздел форума, префиксы и топ авторов\n"
        "6. Карта разделов форума\n"
        "7. Апелляции без префикса\n"
        "8. Основные режимы последовательно\n"
    )
    choices = {
        "1": "members",
        "2": "punishments",
        "3": "bans",
        "4": "clans",
        "5": "forum",
        "6": "sections",
        "7": "appeals",
        "8": "all",
    }
    while True:
        choice = input("Выберите режим [1–8]: ").strip()
        if choice in choices:
            break
        print("Неизвестный пункт меню.")

    mode = choices[choice]
    arguments = [mode]
    if mode in {"members", "bans"}:
        end_id = input("Последний ID (Enter — определить автоматически): ").strip()
        if end_id:
            arguments.extend(["--end-id", end_id])
    elif mode == "punishments":
        punishment_type = input("Тип: ban/kick/mute/warn/all [all]: ").strip().casefold()
        if punishment_type and punishment_type != "all":
            arguments.extend(["--type", punishment_type])
        if input("Открывать подробные карточки? [y/N]: ").strip().casefold() not in {
            "y",
            "yes",
            "д",
            "да",
        }:
            arguments.append("--index-only")
    elif mode == "forum":
        url = input(f"URL раздела [{DEFAULT_FORUM_URL}]: ").strip()
        if url:
            arguments.extend(["--forum-url", url])
        if input("Обходить страницы сообщений? [y/N]: ").strip().casefold() not in {
            "y",
            "yes",
            "д",
            "да",
        }:
            arguments.append("--index-only")
        prefix = input("Префикс (название/ID, Enter — все): ").strip()
        if prefix:
            arguments.extend(["--prefix", prefix])
    elif mode == "clans":
        if input("Собирать участников кланов? [y/N]: ").strip().casefold() not in {
            "y",
            "yes",
            "д",
            "да",
        }:
            arguments.append("--index-only")
    elif mode == "all":
        end_id = input("Последний ID пользователя (Enter — авто): ").strip()
        if end_id:
            arguments.extend(["--members-end-id", end_id])
        if input("Включить исторические баны? [y/N]: ").strip().casefold() in {
            "y",
            "yes",
            "д",
            "да",
        }:
            arguments.append("--include-bans")
        if input("Собрать кланы и участников? [y/N]: ").strip().casefold() not in {
            "y",
            "yes",
            "д",
            "да",
        }:
            arguments.append("--skip-clans")
    arguments.extend(["--concurrency", _ask_positive("Одновременных страниц", 5)])
    return arguments


def _configure_logging(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        filename=output_dir / "last_log.log",
        filemode="a",
        format="%(asctime)s - %(levelname)s - %(message)s",
        encoding="utf-8",
    )


def _print_summary(summary: CrawlSummary | None) -> None:
    if summary is not None:
        print(
            f"[{summary.mode}] завершено: сохранено {summary.saved}, "
            f"пропущено {summary.skipped}, ошибок {summary.failed}, "
            f"время {summary.elapsed_seconds:.1f} с."
        )


def _snapshot_name(value: str | None) -> str | None:
    if value == "auto":
        return datetime.now().astimezone().isoformat(timespec="microseconds").replace(":", "-")
    return value


def _jsonable_options(arguments: argparse.Namespace) -> dict[str, Any]:
    return {
        key: str(value) if isinstance(value, Path) else value
        for key, value in vars(arguments).items()
    }


def describe_plan(arguments: argparse.Namespace) -> list[str]:
    mode = arguments.mode
    if mode == "members":
        end = arguments.end_id or "авто"
        return [f"Пользователи: ID {arguments.start_id}…{end}"]
    if mode == "bans":
        end = arguments.end_id or "авто"
        return [f"Исторические баны: ID {arguments.start_id}…{end}"]
    if mode == "punishments":
        if arguments.historical_only:
            end_id = arguments.historical_end_id or "последний"
            return [
                f"Исторический архив наказаний: ID "
                f"{arguments.historical_start_id}…{end_id}"
            ]
        types = arguments.punishment_types or ["all"]
        end = arguments.end_page or "последняя"
        detail = "индекс + карточки" if not arguments.index_only else "только индекс"
        plan = [
            f"Наказания {', '.join(types)}: страницы {arguments.start_page}…{end}, {detail}"
        ]
        if arguments.include_historical:
            end_id = arguments.historical_end_id or "последний"
            plan.append(
                f"Исторический архив: ID {arguments.historical_start_id}…{end_id}"
            )
        return plan
    if mode == "clans":
        return [f"Кланы: страницы {arguments.start_page}…{arguments.end_page or 'последняя'}"]
    if mode == "forum" and arguments.all_sections:
        return [
            f"Все разделы форума: поиск ID "
            f"{arguments.section_start_id}…{arguments.section_end_id}"
        ]
    if mode in {"forum", "appeals"}:
        return [
            f"Форум: {arguments.forum_url}, страницы {arguments.start_page}…{arguments.end_page or 'последняя'}"
        ]
    if mode == "sections":
        return ["Карта разделов форума"]
    if mode == "all":
        return [
            "Пользователи",
            "Исторические баны"
            if arguments.include_bans or arguments.bans_end_id
            else "Баны: пропуск",
            "Кланы" if not arguments.skip_clans else "Кланы: пропуск",
            "Дополнительный форум" if arguments.forum_url else "Форум: пропуск",
        ]
    return [mode]


def _summary_payload(summaries: list[CrawlSummary]) -> dict[str, Any]:
    return {
        "summaries": [asdict(summary) for summary in summaries],
        "saved": sum(summary.saved for summary in summaries),
        "failed": sum(summary.failed for summary in summaries),
        "skipped": sum(summary.skipped for summary in summaries),
    }


async def run_from_arguments(
    arguments: argparse.Namespace,
    *,
    control: CrawlControl | None = None,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    if arguments.mode == "export":
        database = ParserDatabase(arguments.database)
        count = database.export_csv(arguments.namespace, arguments.csv)
        result = {"exported": count, "path": str(arguments.csv.resolve())}
        print(f"Экспортировано записей: {count}. Файл: {result['path']}")
        return result
    if arguments.mode == "snapshots":
        database = ParserDatabase(arguments.database)
        if arguments.compare:
            result = database.compare_snapshots(*arguments.compare)
            if arguments.json:
                arguments.json.parent.mkdir(parents=True, exist_ok=True)
                arguments.json.write_text(
                    json.dumps(result, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return result
        result = {"snapshots": database.list_snapshots(arguments.namespace)}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result
    if arguments.mode == "migrate":
        database = ParserDatabase(arguments.database)
        result = database.import_jsonl(
            arguments.namespace,
            arguments.jsonl,
            arguments.key_field,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return result

    output_dir: Path = arguments.output_dir.resolve()
    _configure_logging(output_dir)
    if arguments.dry_run:
        plan = describe_plan(arguments)
        print("План запуска:\n- " + "\n- ".join(plan))
        return {"dry_run": True, "plan": plan}

    database_path = (arguments.database or output_dir / "teslacraft.db").resolve()
    database = ParserDatabase(database_path)
    run_id = database.start_run(arguments.mode, _jsonable_options(arguments))
    snapshot_name = _snapshot_name(arguments.snapshot)
    started = time.monotonic()
    summaries: list[CrawlSummary] = []
    control = control or CrawlControl()

    try:
        async with BrowserController(
            profile_dir=arguments.profile_dir,
            request_timeout_seconds=arguments.request_timeout,
            captcha_timeout_seconds=arguments.captcha_timeout,
            retries=arguments.retries,
            checkpoint=control.checkpoint,
        ) as controller:
            common: dict[str, Any] = {
                "controller": controller,
                "concurrency": arguments.concurrency,
                "delay_seconds": arguments.delay,
                "database": database,
                "refresh": arguments.refresh,
                "run_id": run_id,
                "snapshot_name": snapshot_name,
                "control": control,
                "on_progress": on_progress,
            }

            if arguments.mode == "members":
                end_id = arguments.end_id or await discover_latest_member_id(controller)
                summaries.append(
                    await run_members(
                        **common,
                        output_dir=output_dir,
                        start_id=arguments.start_id,
                        end_id=end_id,
                    )
                )
            elif arguments.mode == "bans":
                end_id = arguments.end_id or await discover_latest_ban_id(controller)
                summaries.append(
                    await run_bans(
                        **common,
                        output_dir=output_dir,
                        start_id=arguments.start_id,
                        end_id=end_id,
                    )
                )
            elif arguments.mode == "punishments":
                if not arguments.historical_only:
                    requested = arguments.punishment_types or ["all"]
                    types = (
                        list(PUNISHMENT_PATHS)
                        if "all" in requested
                        else list(dict.fromkeys(requested))
                    )
                    summaries.extend(
                        await run_punishments(
                            **common,
                            output_root=output_dir,
                            punishment_types=types,
                            start_page=arguments.start_page,
                            end_page=arguments.end_page,
                            collect_details=not arguments.index_only,
                            max_details=arguments.max_details,
                            nick=arguments.nick,
                            moderator=arguments.moderator,
                            since=arguments.since,
                            until=arguments.until,
                            statuses=arguments.statuses,
                        )
                    )
                if arguments.include_historical or arguments.historical_only:
                    historical_end = (
                        arguments.historical_end_id
                        or await discover_latest_ban_id(controller)
                    )
                    summaries.append(
                        await run_bans(
                            **common,
                            output_dir=output_dir / "bans",
                            start_id=arguments.historical_start_id,
                            end_id=historical_end,
                        )
                    )
            elif arguments.mode == "clans":
                pair = await run_clans(
                    **common,
                    output_dir=output_dir,
                    start_page=arguments.start_page,
                    end_page=arguments.end_page,
                    collect_members=not arguments.index_only,
                    max_clans=arguments.max_clans,
                )
                summaries.extend(summary for summary in pair if summary is not None)
            elif arguments.mode in {"forum", "appeals"}:
                forum_urls = [arguments.forum_url]
                if arguments.mode == "forum":
                    summaries.append(
                        await run_forum_sections(
                            controller=controller,
                            output_dir=output_dir,
                            database=database,
                            refresh=True,
                            run_id=run_id,
                            snapshot_name=snapshot_name,
                            control=control,
                            on_progress=on_progress,
                        )
                    )
                if arguments.mode == "forum" and arguments.all_sections:
                    discovery_summary, forum_urls = await run_forum_discovery(
                        controller=controller,
                        output_dir=output_dir,
                        start_id=arguments.section_start_id,
                        end_id=arguments.section_end_id,
                        concurrency=arguments.concurrency,
                        delay_seconds=arguments.delay,
                        database=database,
                        refresh=arguments.refresh,
                        run_id=run_id,
                        control=control,
                        on_progress=on_progress,
                    )
                    summaries.append(discovery_summary)

                for forum_url in forum_urls:
                    await control.checkpoint()
                    pair = await run_forum(
                        **common,
                        output_root=output_dir,
                        forum_url=forum_url,
                        start_page=arguments.start_page,
                        end_page=(
                            1
                            if arguments.mode == "forum" and arguments.prefixes_only
                            else arguments.end_page
                        ),
                        collect_usernames=(
                            arguments.mode == "forum"
                            and not arguments.index_only
                            and not arguments.prefixes_only
                        ),
                        without_prefix=(
                            arguments.mode == "appeals" or arguments.without_prefix
                        ),
                        prefix_filters=(
                            arguments.prefix_filters
                            if arguments.mode == "forum"
                            else None
                        ),
                        max_thread_pages=(
                            arguments.max_thread_pages
                            if arguments.mode == "forum"
                            else None
                        ),
                    )
                    summaries.extend(summary for summary in pair if summary is not None)
            elif arguments.mode == "sections":
                summaries.append(
                    await run_forum_sections(
                        controller=controller,
                        output_dir=output_dir,
                        database=database,
                        refresh=arguments.refresh,
                        run_id=run_id,
                        snapshot_name=snapshot_name,
                        control=control,
                        on_progress=on_progress,
                    )
                )
            elif arguments.mode == "all":
                member_end = arguments.members_end_id or await discover_latest_member_id(
                    controller
                )
                summaries.append(
                    await run_members(
                        **common,
                        output_dir=output_dir / "members",
                        start_id=1,
                        end_id=member_end,
                    )
                )
                if arguments.include_bans or arguments.bans_end_id:
                    ban_end = arguments.bans_end_id or await discover_latest_ban_id(controller)
                    summaries.append(
                        await run_bans(
                            **common,
                            output_dir=output_dir / "bans",
                            start_id=1,
                            end_id=ban_end,
                        )
                    )
                if not arguments.skip_clans:
                    pair = await run_clans(
                        **common,
                        output_dir=output_dir / "clans",
                        start_page=1,
                        end_page=None,
                        collect_members=True,
                    )
                    summaries.extend(summary for summary in pair if summary is not None)
                if arguments.forum_url:
                    pair = await run_forum(
                        **common,
                        output_root=output_dir,
                        forum_url=arguments.forum_url,
                        start_page=1,
                        end_page=arguments.forum_end_page,
                        collect_usernames=True,
                        max_thread_pages=arguments.forum_max_thread_pages,
                    )
                    summaries.extend(summary for summary in pair if summary is not None)

        payload = _summary_payload(summaries)
        payload["elapsed_seconds"] = time.monotonic() - started
        payload["database"] = str(database_path)
        database.finish_run(run_id, status="completed", summary=payload)
        for summary in summaries:
            _print_summary(summary)
        print(f"Общее время: {payload['elapsed_seconds']:.1f} с.")
        print(f"Результаты: {output_dir}")
        return payload
    except CrawlCancelled as error:
        payload = _summary_payload(summaries)
        database.finish_run(run_id, status="cancelled", summary=payload, error=str(error))
        raise
    except Exception as error:
        payload = _summary_payload(summaries)
        database.finish_run(run_id, status="failed", summary=payload, error=str(error))
        raise


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")
    argv = list(sys.argv[1:] if argv is None else argv)
    interactive = not argv
    if interactive:
        try:
            argv = interactive_arguments()
        except (EOFError, KeyboardInterrupt):
            print("\nЗапуск отменён.")
            return 130
    arguments = build_argument_parser().parse_args(argv)
    try:
        asyncio.run(run_from_arguments(arguments))
    except (KeyboardInterrupt, CrawlCancelled):
        print("\nОстановлено пользователем. Уже записанные результаты сохранены.")
        exit_code = 130
    except Exception as error:
        logging.exception("Критическая ошибка")
        print(f"Критическая ошибка: {error}")
        print("Успешные записи сохранены. Повторите команду, чтобы продолжить.")
        exit_code = 1
    else:
        exit_code = 0
    if interactive:
        with suppress(EOFError):
            input("\nНажмите Enter, чтобы закрыть программу…")
    return exit_code
