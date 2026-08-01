from __future__ import annotations

import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import flet as ft
import msgpack
from flet.messaging.protocol import configure_encode_object_for_msgpack

import teslacraft_parser.gui as gui_module
from teslacraft_parser.analytics import namespace_statistics, namespace_tables
from teslacraft_parser.browser import BrowserController
from teslacraft_parser.cli import build_argument_parser
from teslacraft_parser.crawler import CrawlCancelled, CrawlControl, crawl_items
from teslacraft_parser.database import ParserDatabase, SqliteStore
from teslacraft_parser.gui import (
    _app_theme,
    _data_row_colors,
    _default_gui_root,
    _display_value,
    _namespace_label,
    _path_from_text,
    _theme_selector,
)
from teslacraft_parser.gui_state import (
    discover_saved_jsonl,
    find_saved_results,
    import_saved_jsonl,
    load_gui_settings,
    prepare_gui_storage,
    save_gui_settings,
)
from teslacraft_parser.html_parsers import (
    parse_forum_index_html,
    parse_forum_sections_html,
    parse_punishment_index_html,
)
from teslacraft_parser.modes import (
    CLAN_INDEX_FORMAT,
    _clean_integer,
    _discard_incomplete_clan_records,
    _entry_matches_punishment_filters,
    _forum_slug,
    _page_url,
    _punishment_entry_from_cells,
    _punishment_index_url,
    _thread_matches_prefixes,
    parse_clan_index,
)
from teslacraft_parser.storage import JsonlStore, write_text_lines


class JsonlStoreTests(unittest.TestCase):
    def test_resume_and_deduplicate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "members.jsonl"
            store = JsonlStore(path, "id")
            self.assertEqual(
                store.append_many(
                    [
                        {"id": 1, "username": "first"},
                        {"id": 2, "username": "second"},
                        {"id": 2, "username": "duplicate"},
                    ]
                ),
                2,
            )

            resumed = JsonlStore(path, "id")
            self.assertIn(1, resumed)
            self.assertIn("2", resumed)
            self.assertFalse(resumed.append({"id": 1, "username": "again"}))
            self.assertEqual(len(resumed.records()), 2)

    def test_atomic_text_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.txt"
            write_text_lines(path, ["one", "two"])
            self.assertEqual(path.read_text(encoding="utf-8"), "one\ntwo\n")


class DatabaseTests(unittest.TestCase):
    def test_record_iteration_can_be_limited(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = ParserDatabase(Path(directory) / "parser.db")
            database.upsert_many(
                "members",
                [(str(index), {"id": index}) for index in range(10)],
            )
            self.assertEqual(
                [record["id"] for record in database.iter_records("members", limit=3)],
                [0, 1, 2],
            )
            with self.assertRaises(ValueError):
                list(database.iter_records("members", limit=-1))

    def test_sqlite_store_partitions_a_range_with_one_key_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = ParserDatabase(Path(directory) / "parser.db")
            database.upsert_many(
                "members",
                [("1", {"id": 1}), ("3", {"id": 3})],
            )
            store = SqliteStore(database, "members", "id")
            with patch.object(
                database,
                "record_keys",
                wraps=database.record_keys,
            ) as record_keys:
                pending, skipped = store.partition_pending(range(1, 5))
            self.assertEqual(pending, [2, 4])
            self.assertEqual(skipped, 2)
            record_keys.assert_called_once_with("members")

            refreshed = SqliteStore(database, "members", "id", refresh=True)
            self.assertEqual(refreshed.partition_pending(range(1, 5)), ([1, 2, 3, 4], 0))

    def test_upsert_snapshots_compare_and_csv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = ParserDatabase(root / "parser.db")
            database.upsert_many("members", [("1", {"id": 1, "username": "One"})])
            first = database.create_snapshot("members", name="first")
            result = database.upsert_many(
                "members",
                [
                    ("1", {"id": 1, "username": "Changed"}),
                    ("2", {"id": 2, "username": "Two"}),
                ],
            )
            database.upsert_many(
                "forum:sections",
                [
                    (
                        "https://teslacraft.org/forums/",
                        {
                            "url": "https://teslacraft.org/forums/",
                            "sections": [
                                {
                                    "category": "Новости",
                                    "title": "Новости сервера",
                                    "node_id": 4,
                                    "kind": "forum",
                                    "level": 2,
                                    "url": "https://teslacraft.org/forums/news.4/",
                                },
                                {
                                    "category": "Общение",
                                    "title": "Флудильня",
                                    "node_id": 30,
                                    "kind": "forum",
                                    "level": 2,
                                    "url": "https://teslacraft.org/forums/chat.30/",
                                },
                            ],
                        },
                    )
                ],
            )
            second = database.create_snapshot("members", name="second")
            self.assertEqual(result["changed"], 1)
            self.assertEqual(result["new"], 1)
            difference = database.compare_snapshots(first, second)
            self.assertEqual([item["key"] for item in difference["added"]], ["2"])
            self.assertEqual([item["key"] for item in difference["changed"]], ["1"])
            self.assertEqual(database.export_csv("members", root / "members.csv"), 2)

    def test_import_legacy_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "old.jsonl"
            source.write_text('{"id":1,"username":"One"}\ninvalid\n', encoding="utf-8")
            result = ParserDatabase(root / "parser.db").import_jsonl("members", source, "id")
            self.assertEqual(result["read"], 1)
            self.assertEqual(result["invalid"], 1)

    def test_saved_jsonl_is_discovered_and_imported_into_empty_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "members.jsonl").write_text(
                '{"id":1,"username":"One"}\n{"id":2,"username":"Two"}\nbroken\n',
                encoding="utf-8",
            )
            database = ParserDatabase(root / "teslacraft.db")
            result = import_saved_jsonl(database, root)
            self.assertEqual(result["files"], 1)
            self.assertEqual(result["read"], 2)
            self.assertEqual(result["new"], 2)
            self.assertEqual(result["invalid"], 1)
            self.assertEqual(database.record_count("members"), 2)
            repeated = import_saved_jsonl(database, root)
            self.assertEqual(repeated["new"], 0)
            self.assertEqual(repeated["changed"], 0)
            self.assertEqual(database.record_count("members"), 2)

    def test_nested_saved_datasets_keep_their_namespaces(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            punishment = root / "punishments" / "ban" / "all" / "entries.jsonl"
            punishment.parent.mkdir(parents=True)
            punishment.write_text('{"key":"ban:1"}\n', encoding="utf-8")
            forum = root / "forum_test.1" / "forum_pages.jsonl"
            forum.parent.mkdir(parents=True)
            forum.write_text('{"url":"https://example.test/page"}\n', encoding="utf-8")
            sources = discover_saved_jsonl(root)
            self.assertEqual(
                {(item.namespace, item.key_field) for item in sources},
                {
                    ("punishments:ban:all:entries", "key"),
                    ("forum:test.1:pages", "url"),
                },
            )

    def test_combined_mode_jsonl_subdirectories_are_discovered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            expected = {
                (root / "members" / "members.jsonl").resolve(): ("members", "id"),
                (root / "bans" / "bans.jsonl").resolve(): (
                    "bans:historical",
                    "id",
                ),
                (root / "clans" / "clan_index.jsonl").resolve(): (
                    "clans:index",
                    "page",
                ),
                (root / "clans" / "clans.jsonl").resolve(): (
                    "clans:details",
                    "url",
                ),
            }
            for path in expected:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("{}\n", encoding="utf-8")

            discovered = {
                item.path: (item.namespace, item.key_field)
                for item in discover_saved_jsonl(root)
            }
            self.assertEqual(discovered, expected)

    def test_missing_jsonl_namespaces_are_added_to_nonempty_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = ParserDatabase(root / "teslacraft.db")
            database.upsert_many("members", [("1", {"id": 1, "username": "One"})])
            (root / "members.jsonl").write_text(
                '{"id":2,"username":"Two"}\n',
                encoding="utf-8",
            )
            (root / "bans.jsonl").write_text(
                '{"id":7,"status":"active"}\n',
                encoding="utf-8",
            )
            result = import_saved_jsonl(
                database,
                root,
                only_missing_namespaces=True,
            )
            self.assertEqual(result["files"], 1)
            self.assertEqual(database.record_count("members"), 1)
            self.assertEqual(database.record_count("bans:historical"), 1)

    def test_saved_results_discovery_prefers_jsonl_over_empty_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_only = root / "database-only"
            ParserDatabase(database_only / "teslacraft.db")
            (database_only / "forum_sections.jsonl").write_text("", encoding="utf-8")
            jsonl_results = root / "jsonl-results"
            jsonl_results.mkdir()
            (jsonl_results / "members.jsonl").write_text(
                '{"id":1}\n',
                encoding="utf-8",
            )
            self.assertEqual(
                find_saved_results([database_only, jsonl_results]),
                jsonl_results.resolve(),
            )

    def test_saved_results_discovery_prefers_populated_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_results = root / "database-results"
            database = ParserDatabase(database_results / "teslacraft.db")
            database.upsert_many("members", [("1", {"id": 1})])
            jsonl_results = root / "jsonl-results"
            jsonl_results.mkdir()
            (jsonl_results / "members.jsonl").write_text(
                '{"id":2}\n',
                encoding="utf-8",
            )
            self.assertEqual(
                find_saved_results([jsonl_results, database_results]),
                database_results.resolve(),
            )

    def test_saved_results_discovery_prefers_the_more_complete_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            small_results = root / "small"
            small = ParserDatabase(small_results / "teslacraft.db")
            small.upsert_many("forum:sections", [("map", {"url": "map"})])
            complete_results = root / "complete"
            complete = ParserDatabase(complete_results / "teslacraft.db")
            complete.upsert_many(
                "members",
                [(str(index), {"id": index}) for index in range(5)],
            )
            self.assertEqual(
                find_saved_results([small_results, complete_results]),
                complete_results.resolve(),
            )

    def test_jsonl_manifest_skips_an_unchanged_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "members.jsonl"
            source.write_text('{"id":1}\n', encoding="utf-8")
            database = ParserDatabase(root / "teslacraft.db")
            first = import_saved_jsonl(database, root)
            repeated = import_saved_jsonl(
                database,
                root,
                known_signatures=first["signatures"],
            )
            self.assertEqual(repeated["files"], 0)
            self.assertEqual(repeated["read"], 0)

    def test_member_statistics_preserve_zero_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = ParserDatabase(Path(directory) / "parser.db")
            database.upsert_many(
                "members",
                [
                    ("1", {"id": 1, "status": "ok", "username": "One", "positive": 0}),
                    ("2", {"id": 2, "status": "missing"}),
                ],
            )
            statistics = namespace_statistics(database, "members")
            self.assertEqual(statistics["record_count"], 2)
            self.assertEqual(statistics["metrics"][1]["value"], 1)
            self.assertEqual(statistics["top"][0]["value"], 0)

    def test_forum_sections_are_presented_as_individual_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = ParserDatabase(Path(directory) / "parser.db")
            database.upsert_many(
                "forum:sections",
                [
                    (
                        "https://teslacraft.org/forums/",
                        {
                            "url": "https://teslacraft.org/forums/",
                            "sections": [
                                {
                                    "category": "Общение",
                                    "title": "Флудильня",
                                    "level": 2,
                                    "kind": "forum",
                                    "node_id": 30,
                                    "url": "https://teslacraft.org/forums/test.30/",
                                },
                                {
                                    "category": "Общение",
                                    "parent": "Режимы",
                                    "title": "Выживание",
                                    "level": 3,
                                    "kind": "forum",
                                    "node_id": 32,
                                    "url": "https://teslacraft.org/forums/test.32/",
                                },
                            ],
                        },
                    )
                ],
            )
            table = namespace_tables(database, "forum:sections")[0]
            self.assertEqual(table["title"], "Разделы форума")
            self.assertEqual(table["total_count"], 2)
            self.assertEqual(
                [row["title"] for row in table["rows"]], ["Флудильня", "Выживание"]
            )
            self.assertEqual(table["rows"][1]["level"], "Подраздел")

    def test_suspicious_single_forum_category_is_not_presented_as_truth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = ParserDatabase(Path(directory) / "parser.db")
            database.upsert_many(
                "forum:sections",
                [
                    (
                        "map",
                        {
                            "url": "https://teslacraft.org/forums/",
                            "sections": [
                                {
                                    "category": "Новости TeslaCraft",
                                    "title": f"Раздел {node_id}",
                                    "level": 2,
                                    "kind": "forum",
                                    "node_id": node_id,
                                    "url": f"https://teslacraft.org/forums/{node_id}/",
                                }
                                for node_id in range(1, 8)
                            ],
                        },
                    )
                ],
            )
            table = namespace_tables(database, "forum:sections")[0]
            self.assertEqual(
                {row["category"] for row in table["rows"]},
                {"Требуется обновить карту"},
            )


class HelperTests(unittest.TestCase):
    def test_site_readiness_accepts_the_forum_layout_and_rejects_cloudflare(
        self,
    ) -> None:
        class FakeLocator:
            def __init__(self, count: int) -> None:
                self.value = count

            async def count(self) -> int:
                return self.value

        class FakePage:
            def __init__(
                self,
                *,
                url: str,
                title: str,
                selectors: dict[str, int] | None = None,
            ) -> None:
                self.url = url
                self._title = title
                self.selectors = selectors or {}

            async def title(self) -> str:
                return self._title

            def locator(self, selector: str) -> FakeLocator:
                return FakeLocator(self.selectors.get(selector, int(selector == "body")))

        async def scenario() -> None:
            forum = FakePage(
                url="https://teslacraft.org/forums/",
                title="TeslaCraft — форум",
            )
            challenge = FakePage(
                url="https://teslacraft.org/forums/",
                title="Just a moment...",
                selectors={"#challenge-running": 1},
            )
            external = FakePage(url="https://example.com/", title="Example")
            self.assertTrue(await BrowserController._site_page_ready(forum))
            self.assertFalse(await BrowserController._site_page_ready(challenge))
            self.assertFalse(await BrowserController._site_page_ready(external))

        asyncio.run(scenario())

    def test_crawl_delay_is_interrupted_by_cancellation(self) -> None:
        async def scenario() -> None:
            control = CrawlControl()
            control.cancel()
            with self.assertRaises(CrawlCancelled):
                await control.wait(60)

        asyncio.run(scenario())

    def test_cancelled_batch_is_not_saved_and_resume_retries_it(self) -> None:
        class FakePage:
            def __init__(self) -> None:
                self.closed = False

            def is_closed(self) -> bool:
                return self.closed

            async def close(self) -> None:
                self.closed = True

        class FakeController:
            async def new_worker_page(self) -> FakePage:
                return FakePage()

            async def navigate(self, page: FakePage, url: str) -> None:
                return None

        async def scenario(directory: str) -> None:
            path = Path(directory) / "items.jsonl"
            errors = Path(directory) / "errors.jsonl"
            first_control = CrawlControl()

            async def interrupted_parse(
                page: FakePage, item: int, url: str
            ) -> dict[str, int]:
                if item == 2:
                    first_control.cancel()
                    await first_control.checkpoint()
                return {"id": item}

            with self.assertRaises(CrawlCancelled):
                await crawl_items(
                    mode="test",
                    controller=FakeController(),  # type: ignore[arg-type]
                    items=[1, 2, 3],
                    url_for=lambda item: f"https://example.test/{item}",
                    parse=interrupted_parse,  # type: ignore[arg-type]
                    store=JsonlStore(path, "id"),
                    error_path=errors,
                    concurrency=1,
                    delay_seconds=0,
                    control=first_control,
                )

            self.assertEqual([record["id"] for record in JsonlStore(path, "id").records()], [1])

            async def complete_parse(
                page: FakePage, item: int, url: str
            ) -> dict[str, int]:
                return {"id": item}

            summary = await crawl_items(
                mode="test",
                controller=FakeController(),  # type: ignore[arg-type]
                items=[1, 2, 3],
                url_for=lambda item: f"https://example.test/{item}",
                parse=complete_parse,  # type: ignore[arg-type]
                store=JsonlStore(path, "id"),
                error_path=errors,
                concurrency=1,
                delay_seconds=0,
                control=CrawlControl(),
            )
            self.assertEqual(summary.skipped, 1)
            self.assertEqual(summary.saved, 2)
            self.assertEqual(
                [record["id"] for record in JsonlStore(path, "id").records()],
                [1, 2, 3],
            )

        with tempfile.TemporaryDirectory() as directory:
            asyncio.run(scenario(directory))

    def test_clan_resume_extends_range_and_retries_legacy_records(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = ParserDatabase(Path(directory) / "teslacraft.db")
            store = SqliteStore(database, "clans:index", "page")
            store.append_many(
                {
                    "page": page,
                    "url": f"https://teslacraft.org/clan/?page={page}",
                    "clan_links": [f"https://teslacraft.org/clan/{page}"],
                    "_complete": CLAN_INDEX_FORMAT,
                }
                for page in range(1, 11)
            )

            pending, skipped = store.partition_pending(range(1, 51))
            self.assertEqual(skipped, 10)
            self.assertEqual(pending, list(range(11, 51)))

            store.append({"page": 11, "url": "legacy", "clan_links": []})
            self.assertEqual(
                _discard_incomplete_clan_records(
                    store,
                    expected_format=CLAN_INDEX_FORMAT,
                ),
                1,
            )
            pending, skipped = store.partition_pending(range(1, 51))
            self.assertEqual(skipped, 10)
            self.assertEqual(pending, list(range(11, 51)))

    def test_clan_heading_and_non_member_rows_are_hidden(self) -> None:
        class LinkLocator:
            async def evaluate_all(self, script: str) -> list[str]:
                return ["/clan/", "/clan/real-clan/"]

        class LinkPage:
            def locator(self, selector: str) -> LinkLocator:
                return LinkLocator()

        record = asyncio.run(
            parse_clan_index(
                LinkPage(),  # type: ignore[arg-type]
                1,
                "https://teslacraft.org/clan/?page=1",
            )
        )
        self.assertEqual(record["clan_links"], ["https://teslacraft.org/clan/real-clan/"])

        with tempfile.TemporaryDirectory() as directory:
            database = ParserDatabase(Path(directory) / "teslacraft.db")
            database.upsert_many(
                "clans:details",
                [
                    (
                        "root",
                        {
                            "url": "https://teslacraft.org/clan/",
                            "title": "Кланы",
                            "members": [
                                {"username": "NotAClan", "score": 100, "raw": "1\tNotAClan"}
                            ],
                        },
                    ),
                    (
                        "real",
                        {
                            "url": "https://teslacraft.org/clan/real/",
                            "title": "Real",
                            "members": [
                                {
                                    "username": "Player",
                                    "score": 42,
                                    "raw": "1\tPlayer\tЛидер\t42",
                                },
                                {
                                    "username": "рейтинга:",
                                    "score": 900,
                                    "raw": "Очки рейтинга:\t900",
                                },
                            ],
                        },
                    ),
                ],
            )
            clan_table, member_table = namespace_tables(database, "clans:details")
            self.assertEqual(clan_table["total_count"], 1)
            self.assertEqual(clan_table["rows"][0]["member_count"], 1)
            self.assertEqual(member_table["total_count"], 1)
            self.assertEqual(member_table["rows"][0]["username"], "Player")

    def test_data_labels_are_user_friendly_and_zero_is_visible(self) -> None:
        self.assertEqual(_namespace_label("members"), "Игроки и рейтинги")
        self.assertEqual(
            _namespace_label("punishments:ban:all:entries"),
            "Баны — список (все)",
        )
        self.assertEqual(_display_value(0), "0")
        self.assertEqual(_display_value(False), "Нет")

    def test_gui_references_only_available_flet_icons(self) -> None:
        source = Path(gui_module.__file__).read_text(encoding="utf-8")
        names = set(re.findall(r"ft\.Icons\.([A-Z0-9_]+)", source))
        self.assertEqual(
            sorted(name for name in names if not hasattr(ft.Icons, name)),
            [],
        )

    def test_all_gui_pages_can_be_built(self) -> None:
        class FakePage:
            theme_mode = ft.ThemeMode.SYSTEM
            overlay: list[ft.Control] = []

            @staticmethod
            def update() -> None:
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output_dir = root / "results"
            database = ParserDatabase(output_dir / "teslacraft.db")
            database.upsert_many(
                "members",
                [
                    (
                        str(index),
                        {
                            "id": index,
                            "status": "ok",
                            "username": f"Player{index}",
                            "positive": index,
                        },
                    )
                    for index in range(35)
                ],
            )
            save_gui_settings(
                root / "gui-settings.json",
                {
                    "output_dir": str(output_dir),
                    "database_path": str(output_dir / "teslacraft.db"),
                },
            )
            with patch.object(
                gui_module,
                "_default_gui_root",
                return_value=root,
            ):
                app = gui_module.TeslaParserApp(FakePage())
                for render in (
                    app._show_runner,
                    app._show_overview,
                    app._show_snapshots,
                    app._show_settings,
                ):
                    render()
                    payload = msgpack.packb(
                        app.content,
                        default=configure_encode_object_for_msgpack(ft.BaseControl),
                    )
                    self.assertTrue(payload)
                app.selected_namespace = "forum:sections"
                app._show_overview()
                forum_payload = msgpack.packb(
                    app.content,
                    default=configure_encode_object_for_msgpack(ft.BaseControl),
                )
                self.assertTrue(forum_payload)
                self.assertEqual(
                    [destination.label for destination in app.navigation.destinations],
                    ["Обзор", "Сбор данных", "Снимки", "Настройки"],
                )
                self.assertEqual(
                    [option.text for option in app.mode.options],
                    [
                        "Игроки и рейтинги",
                        "Наказания",
                        "Кланы",
                        "Раздел форума",
                        "Комплексный запуск",
                    ],
                )

    def test_gui_settings_are_stored_outside_selected_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gui-settings.json"
            settings = {
                "output_dir": str(Path(directory) / "saved"),
                "database_path": str(Path(directory) / "saved" / "teslacraft.db"),
                "theme": "dark",
            }
            save_gui_settings(path, settings)
            self.assertEqual(load_gui_settings(path), settings)

    def test_paths_expand_environment_variables(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(gui_module.os.environ, {"TESLACRAFT_TEST_ROOT": directory}),
        ):
            self.assertEqual(
                _path_from_text(
                    r"%TESLACRAFT_TEST_ROOT%\results",
                    Path("unused"),
                ),
                Path(directory, "results").resolve(),
            )

    def test_changed_output_folder_moves_untouched_default_database(self) -> None:
        class FakePage:
            theme_mode = ft.ThemeMode.SYSTEM

            @staticmethod
            def update() -> None:
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(gui_module, "_default_gui_root", return_value=root):
                app = gui_module.TeslaParserApp(FakePage())
            selected_output = root / "old-results"
            app.output_dir.value = str(selected_output)
            _, database_path, _ = app._normalise_storage_paths()
            self.assertEqual(
                database_path,
                (selected_output / "teslacraft.db").resolve(),
            )

    def test_explicit_database_does_not_follow_output_folder(self) -> None:
        class FakePage:
            theme_mode = ft.ThemeMode.SYSTEM

            @staticmethod
            def update() -> None:
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(gui_module, "_default_gui_root", return_value=root):
                app = gui_module.TeslaParserApp(FakePage())
            custom_database = root / "database" / "custom.sqlite"
            app.database_path.value = str(custom_database)
            app._database_path_changed(type("Event", (), {"control": app.database_path})())
            app.output_dir.value = str(root / "other-results")
            _, database_path, _ = app._normalise_storage_paths()
            self.assertEqual(database_path, custom_database.resolve())

    def test_forum_target_combines_known_custom_and_all_sections(self) -> None:
        class FakePage:
            theme_mode = ft.ThemeMode.SYSTEM

            @staticmethod
            def update() -> None:
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(gui_module, "_default_gui_root", return_value=root):
                app = gui_module.TeslaParserApp(FakePage())
            custom_url = "https://teslacraft.org/forums/custom.1/"
            app.mode.value = "forum"
            app.forum_target.value = "__custom__"
            app.forum_url.value = custom_url
            app._forum_url_changed(type("Event", (), {"control": app.forum_url})())
            self.assertEqual(app._selected_forum_url(), custom_url)
            app.forum_target.value = "appeals"
            app._mode_changed(update_plan=False)
            self.assertIn("Апелляции", app._selected_forum_url())
            app.forum_target.value = "__custom__"
            app._mode_changed(update_plan=False)
            self.assertEqual(app._selected_forum_url(), custom_url)
            app.forum_target.value = "__all__"
            app._mode_changed(update_plan=False)
            self.assertIn(app.section_start_id, app.mode_fields.controls)
            self.assertIn(app.section_end_id, app.mode_fields.controls)

    def test_theme_selector_uses_msgpack_serializable_list(self) -> None:
        selector = _theme_selector("system")
        self.assertEqual(selector.selected, ["system"])
        msgpack.packb(selector.selected)

    def test_packaged_gui_uses_writable_local_app_data(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(gui_module.sys, "frozen", True, create=True),
            patch.dict(gui_module.os.environ, {"LOCALAPPDATA": directory}),
        ):
            self.assertEqual(
                _default_gui_root(),
                Path(directory) / "TeslaParser",
            )

    def test_gui_storage_atomically_adopts_legacy_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            legacy = parent / "TeslaCraftParser"
            preferred = parent / "TeslaParser"
            results = legacy / "results"
            profile = legacy / "user-data"
            results.mkdir(parents=True)
            profile.mkdir()
            (results / "teslacraft.db").write_bytes(b"database")
            (profile / "cookie.bin").write_bytes(b"cookie")
            save_gui_settings(
                legacy / "gui-settings.json",
                {
                    "output_dir": str(results),
                    "database_path": str(results / "teslacraft.db"),
                    "profile_dir": str(profile),
                    "storage_explicit": False,
                },
            )

            prepared = prepare_gui_storage(preferred, [legacy])

            self.assertEqual(prepared.active_root, preferred.resolve())
            self.assertEqual(prepared.migrated_from, legacy.resolve())
            self.assertFalse(legacy.exists())
            self.assertEqual(
                (preferred / "results" / "teslacraft.db").read_bytes(),
                b"database",
            )
            self.assertEqual(
                (preferred / "user-data" / "cookie.bin").read_bytes(),
                b"cookie",
            )
            settings = load_gui_settings(preferred / "gui-settings.json")
            self.assertEqual(settings["output_dir"], str(preferred.resolve() / "results"))
            self.assertEqual(
                settings["database_path"],
                str(preferred.resolve() / "results" / "teslacraft.db"),
            )
            self.assertEqual(
                settings["profile_dir"],
                str(preferred.resolve() / "user-data"),
            )

    def test_gui_storage_does_not_rewrite_external_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            legacy = parent / "TeslaCraftParser"
            preferred = parent / "TeslaParser"
            external = parent / "external"
            legacy.mkdir()
            external.mkdir()
            save_gui_settings(
                legacy / "gui-settings.json",
                {
                    "output_dir": str(external),
                    "database_path": str(external / "custom.sqlite"),
                    "profile_dir": str(external / "chrome"),
                },
            )

            prepare_gui_storage(preferred, [legacy])

            settings = load_gui_settings(preferred / "gui-settings.json")
            self.assertEqual(settings["output_dir"], str(external))
            self.assertEqual(settings["database_path"], str(external / "custom.sqlite"))
            self.assertEqual(settings["profile_dir"], str(external / "chrome"))

    def test_gui_storage_never_merges_two_populated_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            legacy = parent / "TeslaCraftParser"
            preferred = parent / "TeslaParser"
            legacy.mkdir()
            preferred.mkdir()
            (legacy / "legacy.txt").write_text("legacy", encoding="utf-8")
            (preferred / "current.txt").write_text("current", encoding="utf-8")

            prepared = prepare_gui_storage(preferred, [legacy])

            self.assertEqual(prepared.active_root, preferred.resolve())
            self.assertTrue((legacy / "legacy.txt").is_file())
            self.assertTrue((preferred / "current.txt").is_file())

    def test_gui_storage_falls_back_when_atomic_rename_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            legacy = parent / "TeslaCraftParser"
            preferred = parent / "TeslaParser"
            legacy.mkdir()
            (legacy / "sentinel.txt").write_text("safe", encoding="utf-8")

            with patch.object(Path, "rename", side_effect=OSError("locked")):
                prepared = prepare_gui_storage(preferred, [legacy])

            self.assertEqual(prepared.active_root, legacy.resolve())
            self.assertIn("locked", prepared.warning or "")
            self.assertEqual(
                (legacy / "sentinel.txt").read_text(encoding="utf-8"),
                "safe",
            )
            self.assertFalse(preferred.exists())

    def test_packaged_gui_replaces_an_empty_legacy_default_with_saved_results(
        self,
    ) -> None:
        class FakePage:
            theme_mode = ft.ThemeMode.SYSTEM

            @staticmethod
            def update() -> None:
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app_data = root / "local-app-data"
            default_results = app_data / "results"
            project_results = root / "project" / "results"
            executable = (
                root / "project" / "dist" / "TeslaParserGUI" / "TeslaParserGUI.exe"
            )
            save_gui_settings(
                app_data / "gui-settings.json",
                {
                    "output_dir": str(default_results),
                    "database_path": str(default_results / "teslacraft.db"),
                },
            )
            with (
                patch.object(gui_module, "_default_gui_root", return_value=app_data),
                patch.object(gui_module, "_legacy_gui_roots", return_value=[]),
                patch.object(gui_module.sys, "frozen", True, create=True),
                patch.object(gui_module.sys, "executable", str(executable)),
                patch.object(
                    gui_module,
                    "find_saved_results",
                    return_value=project_results.resolve(),
                ) as finder,
            ):
                app = gui_module.TeslaParserApp(FakePage())

            self.assertEqual(app.output_dir.value, str(project_results.resolve()))
            self.assertEqual(
                app.database_path.value,
                str(project_results.resolve() / "teslacraft.db"),
            )
            candidates = finder.call_args.args[0]
            self.assertIn(
                (executable.parent.parent.parent / "results").resolve(),
                candidates,
            )

    def test_explicit_saved_results_are_not_auto_replaced(self) -> None:
        class FakePage:
            theme_mode = ft.ThemeMode.SYSTEM

            @staticmethod
            def update() -> None:
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chosen = root / "chosen-results"
            save_gui_settings(
                root / "gui-settings.json",
                {
                    "output_dir": str(chosen),
                    "database_path": str(chosen / "teslacraft.db"),
                    "database_follows_output": True,
                    "storage_explicit": True,
                },
            )
            with (
                patch.object(gui_module, "_default_gui_root", return_value=root),
                patch.object(gui_module, "find_saved_results") as finder,
            ):
                app = gui_module.TeslaParserApp(FakePage())
            self.assertEqual(app.output_dir.value, str(chosen))
            finder.assert_not_called()

    def test_auxiliary_saved_folder_adds_a_missing_dataset(self) -> None:
        class FakePage:
            theme_mode = ft.ThemeMode.SYSTEM

            @staticmethod
            def update() -> None:
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "primary"
            database = ParserDatabase(primary / "teslacraft.db")
            database.upsert_many("members", [("1", {"id": 1})])
            auxiliary = root / "auxiliary"
            auxiliary.mkdir()
            (auxiliary / "forum_sections.jsonl").write_text(
                '{"url":"https://teslacraft.org/forums/","sections":[]}\n',
                encoding="utf-8",
            )
            save_gui_settings(
                root / "gui-settings.json",
                {
                    "output_dir": str(primary),
                    "database_path": str(primary / "teslacraft.db"),
                    "storage_explicit": True,
                },
            )
            with patch.object(gui_module, "_default_gui_root", return_value=root):
                app = gui_module.TeslaParserApp(FakePage())
            app._auto_import_dirs = [auxiliary]
            merged = app._database_with_saved_data()
            self.assertEqual(merged.record_count("members"), 1)
            self.assertEqual(merged.record_count("forum:sections"), 1)

    def test_material_themes_keep_table_hover_text_readable(self) -> None:
        def contrast_ratio(foreground: str, background: str) -> float:
            def luminance(color: str) -> float:
                channels = [int(color[index : index + 2], 16) / 255 for index in (1, 3, 5)]
                linear = [
                    channel / 12.92
                    if channel <= 0.04045
                    else ((channel + 0.055) / 1.055) ** 2.4
                    for channel in channels
                ]
                return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

            first, second = sorted(
                (luminance(foreground), luminance(background)),
                reverse=True,
            )
            return (first + 0.05) / (second + 0.05)

        row_colors = _data_row_colors()
        self.assertEqual(
            row_colors[ft.ControlState.HOVERED],
            ft.Colors.SURFACE_CONTAINER_HIGHEST,
        )
        self.assertNotEqual(
            row_colors[ft.ControlState.HOVERED],
            row_colors[ft.ControlState.DEFAULT],
        )
        for dark in (False, True):
            scheme = _app_theme(dark=dark).color_scheme
            self.assertGreaterEqual(
                contrast_ratio(scheme.on_surface, scheme.surface_container_highest),
                4.5,
            )

    def test_integer_cleanup(self) -> None:
        self.assertEqual(_clean_integer("+12 345"), 12345)
        self.assertEqual(_clean_integer("-1 204"), -1204)
        self.assertIsNone(_clean_integer("нет"))

    def test_page_url(self) -> None:
        base = "https://teslacraft.org/threads/example.1/"
        self.assertEqual(_page_url(base, 1), base)
        self.assertEqual(
            _page_url(base, 2),
            "https://teslacraft.org/threads/example.1/page-2",
        )

    def test_forum_slug(self) -> None:
        self.assertEqual(
            _forum_slug(
                "https://teslacraft.org/forums/"
                "%D0%A4%D0%BB%D1%83%D0%B4%D0%B8%D0%BB%D1%8C%D0%BD%D1%8F.30/"
            ),
            "Флудильня.30",
        )

    def test_cli_members(self) -> None:
        arguments = build_argument_parser().parse_args(["members", "--end-id", "100"])
        self.assertEqual(arguments.mode, "members")
        self.assertEqual(arguments.end_id, 100)
        self.assertEqual(arguments.concurrency, 5)

    def test_cli_dynamic_forum_prefixes(self) -> None:
        arguments = build_argument_parser().parse_args(
            [
                "forum",
                "--prefix",
                "Исправлено",
                "--prefix",
                "19",
                "--without-prefix",
                "--index-only",
            ]
        )
        self.assertEqual(arguments.prefix_filters, ["Исправлено", "19"])
        self.assertTrue(arguments.without_prefix)

        all_sections = build_argument_parser().parse_args(
            ["forum", "--all-sections", "--section-end-id", "200"]
        )
        self.assertTrue(all_sections.all_sections)
        self.assertEqual(all_sections.section_end_id, 200)

    def test_cli_punishments(self) -> None:
        arguments = build_argument_parser().parse_args(
            [
                "punishments",
                "--type",
                "mute",
                "--type",
                "warn",
                "--nick",
                "Toshka",
                "--index-only",
            ]
        )
        self.assertEqual(arguments.punishment_types, ["mute", "warn"])
        self.assertEqual(arguments.nick, "Toshka")
        self.assertTrue(arguments.index_only)
        historical = build_argument_parser().parse_args(
            [
                "punishments",
                "--include-historical",
                "--historical-start-id",
                "10",
                "--historical-end-id",
                "20",
            ]
        )
        self.assertTrue(historical.include_historical)
        self.assertEqual(historical.historical_start_id, 10)
        self.assertEqual(historical.historical_end_id, 20)

    def test_punishment_url_encodes_filters(self) -> None:
        self.assertEqual(
            _punishment_index_url(
                "warn",
                3,
                nick="Игрок 1",
                moderator="T-b",
            ),
            (
                "https://teslacraft.org/banlist/warn/"
                "?page=3&searchByNick=%D0%98%D0%B3%D1%80%D0%BE%D0%BA+1"
                "&searchByBlys=T-b"
            ),
        )

    def test_punishment_rows_are_normalized_by_type(self) -> None:
        ban = _punishment_entry_from_cells(
            "ban",
            [
                "Player",
                "ABCD-1234",
                "2026-07-31 01:02:03",
                "Console",
                "Активен",
                "Подробнее",
            ],
            "/banlist/3344829",
        )
        mute = _punishment_entry_from_cells(
            "mute",
            [
                "Player",
                "2026-07-31 01:02:03",
                "Moderator",
                "Снят",
                "Подробнее",
            ],
            "/banlist/mute/3755456",
        )
        self.assertEqual(ban["ip"], "ABCD-1234")
        self.assertEqual(ban["status"], "Активен")
        self.assertEqual(ban["detail_url"], "https://teslacraft.org/banlist/3344829")
        self.assertIsNone(mute["ip"])
        self.assertEqual(mute["moderator"], "Moderator")
        self.assertEqual(mute["status"], "Снят")

    def test_punishment_date_and_status_alias_filters(self) -> None:
        entry = {"occurred_at": "31.07.2026 01:02:03", "status": "Активен"}
        self.assertTrue(
            _entry_matches_punishment_filters(
                entry,
                since="2026-07-01",
                until="2026-07-31",
                statuses=["active"],
            )
        )
        self.assertFalse(
            _entry_matches_punishment_filters(
                entry,
                since="2026-08-01",
                until=None,
                statuses=None,
            )
        )

    def test_prefix_filter_by_name_id_and_empty(self) -> None:
        prefixed = {
            "prefixes": [
                {"id": "16", "name": "Исправлено"},
            ]
        }
        plain = {"prefixes": []}
        legacy_prefixed = {"has_prefix": True}
        self.assertTrue(
            _thread_matches_prefixes(
                prefixed,
                prefix_filters=["исправлено"],
                without_prefix=False,
            )
        )
        self.assertTrue(
            _thread_matches_prefixes(
                prefixed,
                prefix_filters=["16"],
                without_prefix=False,
            )
        )
        self.assertFalse(
            _thread_matches_prefixes(
                prefixed,
                prefix_filters=None,
                without_prefix=True,
            )
        )
        self.assertTrue(
            _thread_matches_prefixes(
                plain,
                prefix_filters=["16"],
                without_prefix=True,
            )
        )
        self.assertFalse(
            _thread_matches_prefixes(
                legacy_prefixed,
                prefix_filters=None,
                without_prefix=True,
            )
        )


class HtmlFixtureTests(unittest.TestCase):
    fixtures = Path(__file__).parent / "fixtures"

    def test_forum_index_extended_fields(self) -> None:
        record = parse_forum_index_html(
            (self.fixtures / "forum_index.html").read_text(encoding="utf-8"),
            1,
            "https://teslacraft.org/forums/example.44/",
        )
        thread = record["threads"][0]
        self.assertEqual(thread["total_pages"], 12)
        self.assertEqual(thread["replies"], 1234)
        self.assertEqual(thread["views"], 5678)
        self.assertEqual(thread["prefixes"][0]["id"], "16")
        self.assertTrue(thread["is_sticky"])
        self.assertTrue(thread["is_locked"])
        self.assertEqual(len(record["available_prefixes"]), 2)

    def test_punishment_index_fixture(self) -> None:
        record = parse_punishment_index_html(
            (self.fixtures / "punishments.html").read_text(encoding="utf-8"),
            "ban",
            1,
            "https://teslacraft.org/banlist/",
        )
        self.assertEqual(record["entries"][0]["id"], 3344829)
        self.assertEqual(record["entries"][0]["status"], "Активен")

    def test_sections_fixture(self) -> None:
        record = parse_forum_sections_html(
            (self.fixtures / "sections.html").read_text(encoding="utf-8"),
            "https://teslacraft.org/forums/",
        )
        self.assertEqual([item["level"] for item in record["sections"]], [2, 3])
        self.assertEqual(record["sections"][0]["node_id"], 44)

    def test_nested_forum_categories_are_assigned_to_their_own_sections(self) -> None:
        html = """
        <ol class="nodeList sectionMain">
          <li class="node category level_1">
            <div class="nodeInfo"><div class="nodeTitle"><a>Общение</a></div></div>
            <ol class="nodeList">
              <li class="node forum level_2 node_30">
                <div class="nodeInfo"><div class="nodeTitle">
                  <a href="/forums/chat.30/">Флудильня</a>
                </div></div>
              </li>
            </ol>
          </li>
          <li class="node category level_1">
            <div class="nodeInfo"><div class="nodeTitle"><a>Ошибки</a></div></div>
            <ol class="nodeList">
              <li class="node forum level_2 node_44">
                <div class="nodeInfo"><div class="nodeTitle">
                  <a href="/forums/bugs.44/">Прочие баги</a>
                </div></div>
              </li>
            </ol>
          </li>
        </ol>
        """
        record = parse_forum_sections_html(html, "https://teslacraft.org/forums/")
        self.assertEqual(
            [(item["category"], item["title"]) for item in record["sections"]],
            [("Общение", "Флудильня"), ("Ошибки", "Прочие баги")],
        )

    def test_flat_xenforo_categories_follow_document_order(self) -> None:
        html = """
        <ol class="nodeList sectionMain">
          <li class="node category level_1">
            <div class="nodeInfo categoryNodeInfo"><div class="categoryText">
              <h3 class="nodeTitle"><a>Новости</a></h3>
            </div></div>
          </li>
          <li class="node forum level_2 node_4"><div class="nodeInfo">
            <div class="nodeTitle"><a href="/forums/news.4/">Новости сервера</a></div>
          </div></li>
          <li class="node category level_1">
            <div class="nodeInfo categoryNodeInfo"><div class="categoryText">
              <h3 class="nodeTitle"><a>Общение</a></h3>
            </div></div>
          </li>
          <li class="node forum level_2 node_30"><div class="nodeInfo">
            <div class="nodeTitle"><a href="/forums/chat.30/">Флудильня</a></div>
          </div></li>
        </ol>
        """
        record = parse_forum_sections_html(html, "https://teslacraft.org/forums/")
        self.assertEqual(
            [(item["category"], item["title"]) for item in record["sections"]],
            [("Новости", "Новости сервера"), ("Общение", "Флудильня")],
        )


if __name__ == "__main__":
    unittest.main()
