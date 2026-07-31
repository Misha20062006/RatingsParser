from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import flet as ft
import msgpack

import teslacraft_parser.gui as gui_module
from teslacraft_parser.cli import build_argument_parser
from teslacraft_parser.database import ParserDatabase
from teslacraft_parser.gui import (
    _app_theme,
    _data_row_colors,
    _default_gui_root,
    _theme_selector,
)
from teslacraft_parser.html_parsers import (
    parse_forum_index_html,
    parse_forum_sections_html,
    parse_punishment_index_html,
)
from teslacraft_parser.modes import (
    _clean_integer,
    _entry_matches_punishment_filters,
    _forum_slug,
    _page_url,
    _punishment_entry_from_cells,
    _punishment_index_url,
    _thread_matches_prefixes,
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


class HelperTests(unittest.TestCase):
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
                Path(directory) / "TeslaCraftParser",
            )

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


if __name__ == "__main__":
    unittest.main()
