from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import flet as ft
import flet_charts as fch

from .analytics import dashboard_summary, namespace_statistics
from .cli import build_argument_parser, describe_plan, run_from_arguments
from .crawler import CrawlCancelled, CrawlControl, ProgressEvent
from .database import ParserDatabase
from .modes import DEFAULT_APPEALS_URL, DEFAULT_FORUM_URL

APP_TITLE = "TeslaCraft Parser"
WINDOW_WIDTH = 1180
WINDOW_HEIGHT = 760
MODE_LABELS = {
    "members": "Игроки и рейтинги",
    "punishments": "Наказания",
    "bans": "Исторические баны",
    "clans": "Кланы",
    "forum": "Раздел форума",
    "sections": "Карта форума",
    "appeals": "Апелляции",
    "all": "Комплексный запуск",
}
MODE_GUIDES = {
    "members": {
        "action": (
            "Программа откроет публичные карточки игроков по ID и сохранит ник, "
            "дату регистрации и три вида рейтинга."
        ),
        "result": "Таблица игроков, рейтинги, текстовые топы, JSONL и записи в SQLite.",
        "note": (
            "Последний ID можно оставить пустым — программа попробует найти его сама. "
            "Большой диапазон лучше собирать частями."
        ),
    },
    "punishments": {
        "action": (
            "Будут прочитаны актуальные списки банов, киков, мутов и предупреждений. "
            "Если не включён режим «только списки», программа также откроет карточку "
            "каждого найденного наказания."
        ),
        "result": "Нормализованный список наказаний, подробные карточки и статистика.",
        "note": (
            "Фильтры по нику и блюстителю передаются сайту. Даты и статусы применяются "
            "к полученному результату."
        ),
    },
    "bans": {
        "action": (
            "Программа последовательно проверит старые карточки банов по числовому ID. "
            "Этот режим нужен для исторической статистики, а не только последних банов."
        ),
        "result": "Исторические карточки банов со статусом и доступными полями.",
        "note": (
            "Полный диапазон может быть очень большим. Для быстрой проверки укажите "
            "небольшой начальный и конечный ID."
        ),
    },
    "clans": {
        "action": (
            "Сначала программа соберёт список кланов, затем — страницы их участников "
            "и клановые очки."
        ),
        "result": "Список кланов, участники, очки и общий рейтинг игроков.",
        "note": (
            "«Только списки» остановит работу после получения ссылок на кланы. "
            "Ограничение карточек удобно для пробного запуска."
        ),
    },
    "forum": {
        "action": (
            "Будут собраны темы выбранного раздела: префиксы, авторы, даты, ответы, "
            "просмотры и состояние темы. При полном обходе также подсчитываются авторы "
            "сообщений на страницах тем."
        ),
        "result": "Каталог тем, доступные префиксы, ссылки и топ авторов сообщений.",
        "note": (
            "Префиксы можно указать названием или ID через запятую. Пустое поле означает "
            "все префиксы; отдельная галочка добавляет темы без префикса."
        ),
    },
    "sections": {
        "action": (
            "Программа один раз откроет главную страницу форума и построит карту всех "
            "видимых категорий, разделов и подразделов."
        ),
        "result": "Удобный список названий, адресов, категорий и ID разделов.",
        "note": "Это быстрый режим и хороший первый запуск для проверки доступа к сайту.",
    },
    "appeals": {
        "action": (
            "Будут прочитаны темы раздела апелляций и оставлены только темы без "
            "префикса — например, ещё не отмеченные итоговым решением."
        ),
        "result": "Список подходящих апелляций и ссылки на них.",
        "note": "По умолчанию подробные страницы сообщений не обходятся.",
    },
    "all": {
        "action": (
            "Основные режимы будут запущены последовательно с общими настройками. "
            "Дополнительные тяжёлые этапы можно отключить галочками."
        ),
        "result": "Единая база с несколькими наборами данных и общая история запуска.",
        "note": (
            "Это самый долгий вариант. Перед ним лучше отдельно проверить нужные режимы "
            "на небольших диапазонах."
        ),
    },
}


def _number(value: Any) -> str:
    try:
        return f"{int(value):,}".replace(",", " ")
    except (TypeError, ValueError):
        return str(value)


def _default_gui_root() -> Path:
    """Use a writable, stable directory when the GUI runs as a packaged app."""

    if not getattr(sys, "frozen", False):
        return Path.cwd()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "TeslaCraftParser"
    return Path.home() / "TeslaCraftParser"


def _card(content: ft.Control, *, col: int | dict[str, int] = 12) -> ft.Card:
    return ft.Card(
        content=ft.Container(content=content, padding=20),
        col=col,
        variant=ft.CardVariant.FILLED,
    )


def _metric_card(label: str, value: Any, icon: ft.IconData) -> ft.Card:
    return _card(
        ft.Row(
            [
                ft.Icon(icon, size=32),
                ft.Column(
                    [
                        ft.Text(_number(value), size=28, weight=ft.FontWeight.W_600),
                        ft.Text(label, color=ft.Colors.ON_SURFACE_VARIANT),
                    ],
                    spacing=2,
                ),
            ],
            spacing=16,
        ),
        col={"sm": 12, "md": 6, "lg": 3},
    )


def _theme_selector(theme_mode: str) -> ft.SegmentedButton:
    """Build a serializable Material theme selector for the settings page."""

    return ft.SegmentedButton(
        selected=[theme_mode],
        segments=[
            ft.Segment(
                value="system",
                label="Системная",
                icon=ft.Icons.BRIGHTNESS_AUTO,
            ),
            ft.Segment(value="light", label="Светлая", icon=ft.Icons.LIGHT_MODE),
            ft.Segment(value="dark", label="Тёмная", icon=ft.Icons.DARK_MODE),
        ],
    )


def _app_theme(*, dark: bool = False) -> ft.Theme:
    """Return a high-contrast Material 3 theme for the selected brightness."""

    colors = (
        {
            "primary": "#AAC7FF",
            "on_primary": "#0A305F",
            "primary_container": "#284777",
            "on_primary_container": "#D6E3FF",
            "secondary": "#BEC6DC",
            "on_secondary": "#283141",
            "secondary_container": "#3E4759",
            "on_secondary_container": "#DAE2F9",
            "tertiary": "#DEBCDF",
            "on_tertiary": "#3F2844",
            "tertiary_container": "#573E5C",
            "on_tertiary_container": "#FAD8FD",
            "error": "#FFB4AB",
            "on_error": "#690005",
            "error_container": "#93000A",
            "on_error_container": "#FFDAD6",
            "surface": "#111318",
            "on_surface": "#E2E2E9",
            "on_surface_variant": "#C4C6D0",
            "outline": "#8E9099",
            "outline_variant": "#43474E",
            "surface_container_lowest": "#0C0E13",
            "surface_container_low": "#191C20",
            "surface_container": "#1D2024",
            "surface_container_high": "#282A2F",
            "surface_container_highest": "#33353A",
        }
        if dark
        else {
            "primary": "#415F91",
            "on_primary": "#FFFFFF",
            "primary_container": "#D6E3FF",
            "on_primary_container": "#001B3D",
            "secondary": "#565F71",
            "on_secondary": "#FFFFFF",
            "secondary_container": "#DAE2F9",
            "on_secondary_container": "#131C2B",
            "tertiary": "#705575",
            "on_tertiary": "#FFFFFF",
            "tertiary_container": "#FAD8FD",
            "on_tertiary_container": "#28132E",
            "error": "#BA1A1A",
            "on_error": "#FFFFFF",
            "error_container": "#FFDAD6",
            "on_error_container": "#410002",
            "surface": "#F9F9FF",
            "on_surface": "#191C20",
            "on_surface_variant": "#43474E",
            "outline": "#74777F",
            "outline_variant": "#C4C6D0",
            "surface_container_lowest": "#FFFFFF",
            "surface_container_low": "#F3F3FA",
            "surface_container": "#EDEDF4",
            "surface_container_high": "#E7E8EE",
            "surface_container_highest": "#E2E2E9",
        }
    )
    return ft.Theme(color_scheme=ft.ColorScheme(**colors), use_material3=True)


def _data_row_colors() -> dict[ft.ControlState, ft.ColorValue]:
    """Keep text readable in every interactive state of a data row."""

    return {
        ft.ControlState.DEFAULT: ft.Colors.SURFACE_CONTAINER_LOWEST,
        ft.ControlState.HOVERED: ft.Colors.SURFACE_CONTAINER_HIGHEST,
        ft.ControlState.FOCUSED: ft.Colors.SECONDARY_CONTAINER,
        ft.ControlState.PRESSED: ft.Colors.SECONDARY_CONTAINER,
        ft.ControlState.SELECTED: ft.Colors.SECONDARY_CONTAINER,
    }


class TeslaCraftApp:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.data_root = _default_gui_root()
        self.default_output_dir = self.data_root / "results"
        self.default_profile_dir = self.data_root / "user-data"
        self.control: CrawlControl | None = None
        self.running = False
        self.selected_namespace: str | None = None
        self.log_lines: list[str] = []

        self.output_dir = ft.TextField(
            label="Папка результатов",
            value=str(self.default_output_dir),
            helper="Сюда записываются отчёты, CSV и журнал ошибок.",
        )
        self.database_path = ft.TextField(
            label="SQLite-база",
            value=str(self.default_output_dir / "teslacraft.db"),
            helper="Основное хранилище: текущие записи, снимки и история запусков.",
        )
        self.profile_dir = ft.TextField(
            label="Профиль Chrome",
            value=str(self.default_profile_dir),
            helper="Здесь сохраняются cookies после ручной проверки Cloudflare.",
        )
        self.concurrency = ft.TextField(
            label="Одновременных страниц",
            value="5",
            helper="Рекомендуется 3–5. Больше — быстрее, но заметно нагружает сайт.",
        )
        self.delay = ft.TextField(
            label="Пауза между пакетами",
            value="0.25",
            helper="Дополнительная задержка в секундах между группами страниц.",
        )
        self.retries = ft.TextField(
            label="Повторных попыток",
            value="3",
            helper="Сколько раз повторить временно не загрузившуюся страницу.",
        )
        self.request_timeout = ft.TextField(
            label="Таймаут страницы, с",
            value="60",
            helper="Максимальное ожидание обычной страницы сайта.",
        )
        self.captcha_timeout = ft.TextField(
            label="Ожидание проверки, с",
            value="300",
            helper="Сколько программа ждёт, пока вы вручную завершите проверку в Chrome.",
        )

        self.mode = ft.Dropdown(
            label="Что собрать",
            value="members",
            options=[
                ft.DropdownOption(key=key, text=value) for key, value in MODE_LABELS.items()
            ],
            on_select=self._mode_changed,
        )
        self.start_id = ft.TextField(label="Начальный ID", value="1")
        self.end_id = ft.TextField(
            label="Последний ID",
            hint_text="Пусто — определить автоматически",
        )
        self.start_page = ft.TextField(label="Начальная страница", value="1")
        self.end_page = ft.TextField(
            label="Последняя страница",
            hint_text="Пусто — определить автоматически",
        )
        self.forum_url = ft.TextField(label="URL раздела", value=DEFAULT_FORUM_URL)
        self.punishment_types = ft.TextField(
            label="Типы наказаний",
            value="ban, kick, mute, warn",
            helper="Через запятую: ban, kick, mute, warn",
        )
        self.nick = ft.TextField(label="Ник игрока")
        self.moderator = ft.TextField(label="Ник блюстителя")
        self.since = ft.TextField(label="Дата от", hint_text="YYYY-MM-DD")
        self.until = ft.TextField(label="Дата до", hint_text="YYYY-MM-DD")
        self.statuses = ft.TextField(
            label="Статусы",
            hint_text="Активен, Снят",
        )
        self.prefixes = ft.TextField(
            label="Префиксы",
            hint_text="Названия или ID через запятую; пусто — все",
        )
        self.max_details = ft.TextField(
            label="Максимум карточек / страниц тем",
            hint_text="Пусто — без ограничения",
        )
        self.index_only = ft.Checkbox(label="Только списки, без подробных страниц")
        self.without_prefix = ft.Checkbox(label="Включить темы без префикса")
        self.refresh_existing = ft.Checkbox(label="Обновить уже сохранённые записи")
        self.make_snapshot = ft.Checkbox(label="Создать снимок после запуска")
        self.include_bans = ft.Checkbox(label="Включить исторические баны")
        self.skip_clans = ft.Checkbox(label="Пропустить кланы")
        self.migrate_jsonl = ft.TextField(label="Старый JSONL-файл")
        self.migrate_namespace = ft.TextField(
            label="Имя набора в SQLite",
            hint_text="например: members",
        )
        self.migrate_key = ft.TextField(
            label="Поле уникального ключа",
            hint_text="например: id, key, url или page",
        )

        self.mode_fields = ft.ResponsiveRow()
        self.mode_help = ft.Card(variant=ft.CardVariant.OUTLINED)
        self.plan_text = ft.Text("", color=ft.Colors.ON_SURFACE_VARIANT)
        self.progress = ft.ProgressBar(value=0, visible=False)
        self.status_text = ft.Text("Готов к запуску")
        self.log_view = ft.ListView(height=180, spacing=4, auto_scroll=True)
        self.start_button = ft.FilledButton(
            "Запустить",
            icon=ft.Icons.PLAY_ARROW,
            on_click=self._start,
        )
        self.pause_button = ft.OutlinedButton(
            "Пауза",
            icon=ft.Icons.PAUSE,
            disabled=True,
            on_click=self._pause,
        )
        self.cancel_button = ft.OutlinedButton(
            "Остановить",
            icon=ft.Icons.STOP,
            disabled=True,
            on_click=self._cancel,
        )

        self.content = ft.Container(expand=True, padding=24)
        self.navigation = ft.NavigationRail(
            selected_index=0,
            extended=True,
            min_extended_width=210,
            destinations=[
                ft.NavigationRailDestination(
                    icon=ft.Icons.DASHBOARD_OUTLINED,
                    selected_icon=ft.Icons.DASHBOARD,
                    label="Обзор",
                ),
                ft.NavigationRailDestination(
                    icon=ft.Icons.TRAVEL_EXPLORE_OUTLINED,
                    selected_icon=ft.Icons.TRAVEL_EXPLORE,
                    label="Сбор данных",
                ),
                ft.NavigationRailDestination(
                    icon=ft.Icons.TABLE_VIEW_OUTLINED,
                    selected_icon=ft.Icons.TABLE_VIEW,
                    label="Данные и аналитика",
                ),
                ft.NavigationRailDestination(
                    icon=ft.Icons.COMPARE_ARROWS,
                    label="Снимки",
                ),
                ft.NavigationRailDestination(
                    icon=ft.Icons.SETTINGS_OUTLINED,
                    selected_icon=ft.Icons.SETTINGS,
                    label="Настройки",
                ),
            ],
            on_change=self._navigate,
        )
        self._load_settings()
        self._mode_changed()

    @property
    def database(self) -> ParserDatabase:
        path = self.database_path.value.strip()
        if not path:
            output_dir = Path(self.output_dir.value.strip() or self.default_output_dir)
            path = str(output_dir / "teslacraft.db")
        return ParserDatabase(Path(path))

    def _load_settings(self) -> None:
        try:
            database = ParserDatabase(Path(self.database_path.value))
            settings = database.get_setting("gui", {})
        except Exception:
            return
        controls = {
            "output_dir": self.output_dir,
            "database_path": self.database_path,
            "profile_dir": self.profile_dir,
            "concurrency": self.concurrency,
            "delay": self.delay,
            "retries": self.retries,
            "request_timeout": self.request_timeout,
            "captcha_timeout": self.captcha_timeout,
        }
        for key, control in controls.items():
            if key in settings:
                control.value = str(settings[key])
        theme = settings.get("theme")
        if theme in {"light", "dark", "system"}:
            self.page.theme_mode = ft.ThemeMode(theme)

    def mount(self) -> None:
        self.page.title = APP_TITLE
        self.page.theme = _app_theme()
        self.page.dark_theme = _app_theme(dark=True)
        self.page.bgcolor = ft.Colors.SURFACE
        self.page.padding = 0
        self.page.window.width = WINDOW_WIDTH
        self.page.window.height = WINDOW_HEIGHT
        self.page.window.min_width = 980
        self.page.window.min_height = 680
        self.page.add(
            ft.Row(
                [
                    self.navigation,
                    ft.VerticalDivider(width=1),
                    self.content,
                ],
                expand=True,
                spacing=0,
            )
        )
        self._show_dashboard()

    def _navigate(self, event: ft.Event[ft.NavigationRail] | None = None) -> None:
        index = self.navigation.selected_index or 0
        pages = [
            self._show_dashboard,
            self._show_runner,
            self._show_data,
            self._show_snapshots,
            self._show_settings,
        ]
        pages[index]()

    def _header(self, title: str, subtitle: str) -> ft.Column:
        return ft.Column(
            [
                ft.Text(title, size=32, weight=ft.FontWeight.W_600),
                ft.Text(subtitle, color=ft.Colors.ON_SURFACE_VARIANT),
            ],
            spacing=4,
        )

    def _show_dashboard(self) -> None:
        summary = dashboard_summary(self.database)
        recent_runs = summary["recent_runs"]
        run_tiles = [
            ft.ListTile(
                leading=ft.Icon(
                    ft.Icons.CHECK_CIRCLE
                    if run["status"] == "completed"
                    else ft.Icons.ERROR_OUTLINE
                    if run["status"] == "failed"
                    else ft.Icons.SCHEDULE
                ),
                title=f"{MODE_LABELS.get(run['mode'], run['mode'])} · {run['status']}",
                subtitle=f"{run['started_at']} · сохранено: {run['summary'].get('saved', 0)}",
            )
            for run in recent_runs[:8]
        ] or [ft.ListTile(title="Запусков пока нет")]
        self.content.content = ft.Column(
            [
                self._header(
                    "Обзор",
                    "Сводка локальной базы и последних запусков.",
                ),
                ft.ResponsiveRow(
                    [
                        _metric_card("Всего записей", summary["records"], ft.Icons.STORAGE),
                        _metric_card("Наборов данных", summary["namespaces"], ft.Icons.DATASET),
                        _metric_card("Последних запусков", summary["runs"], ft.Icons.HISTORY),
                        _metric_card("Снимков", summary["snapshots"], ft.Icons.CAMERA_ALT),
                    ]
                ),
                _card(
                    ft.Column(
                        [
                            ft.Text("Последние запуски", size=20, weight=ft.FontWeight.W_600),
                            *run_tiles,
                        ]
                    )
                ),
            ],
            spacing=16,
            scroll=ft.ScrollMode.AUTO,
        )
        self.page.update()

    def _mode_changed(self, event: ft.Event[Any] | None = None) -> None:
        mode = self.mode.value or "members"
        controls: list[ft.Control]
        if mode in {"members", "bans"}:
            controls = [self.start_id, self.end_id]
        elif mode == "punishments":
            controls = [
                self.punishment_types,
                self.start_page,
                self.end_page,
                self.nick,
                self.moderator,
                self.since,
                self.until,
                self.statuses,
                self.max_details,
                self.index_only,
            ]
        elif mode == "clans":
            controls = [self.start_page, self.end_page, self.max_details, self.index_only]
        elif mode in {"forum", "appeals"}:
            self.forum_url.value = (
                DEFAULT_APPEALS_URL
                if mode == "appeals"
                else self.forum_url.value or DEFAULT_FORUM_URL
            )
            controls = [self.forum_url, self.start_page, self.end_page]
            if mode == "forum":
                controls.extend(
                    [self.prefixes, self.without_prefix, self.max_details, self.index_only]
                )
        elif mode == "all":
            controls = [
                self.end_id,
                self.include_bans,
                self.skip_clans,
                self.forum_url,
                self.end_page,
                self.max_details,
            ]
        else:
            controls = []
        for control in controls:
            control.col = {"sm": 12, "md": 6}
        self.mode_fields.controls = controls
        guide = MODE_GUIDES[mode]
        self.mode_help.content = ft.Container(
            padding=12,
            content=ft.Column(
                [
                    ft.ListTile(
                        leading=ft.Icons.ROUTE,
                        title="Что произойдёт",
                        subtitle=guide["action"],
                    ),
                    ft.ListTile(
                        leading=ft.Icons.SAVE_ALT,
                        title="Что получится",
                        subtitle=guide["result"],
                    ),
                    ft.ListTile(
                        leading=ft.Icons.INFO_OUTLINE,
                        title="Обратите внимание",
                        subtitle=guide["note"],
                    ),
                ],
                spacing=0,
            ),
        )
        try:
            self._update_plan()
            self.page.update()
        except RuntimeError:
            pass

    def _show_runner(self) -> None:
        self._mode_changed()
        self.content.content = ft.Column(
            [
                self._header(
                    "Сбор данных",
                    "Откроется обычный Chrome. Если появится проверка, завершите её вручную.",
                ),
                _card(
                    ft.Column(
                        [
                            self.mode,
                            self.mode_help,
                            self.mode_fields,
                            ft.ResponsiveRow(
                                [
                                    ft.Container(
                                        self.refresh_existing, col={"sm": 12, "md": 6}
                                    ),
                                    ft.Container(self.make_snapshot, col={"sm": 12, "md": 6}),
                                ]
                            ),
                            ft.Divider(),
                            self.plan_text,
                            ft.Row(
                                [
                                    self.start_button,
                                    self.pause_button,
                                    self.cancel_button,
                                    ft.OutlinedButton(
                                        "Обновить план",
                                        icon=ft.Icons.PREVIEW,
                                        on_click=self._update_plan,
                                    ),
                                ],
                                wrap=True,
                            ),
                        ],
                        spacing=16,
                    )
                ),
                _card(
                    ft.Column(
                        [
                            self.status_text,
                            self.progress,
                            self.log_view,
                        ],
                        spacing=12,
                    )
                ),
            ],
            spacing=16,
            scroll=ft.ScrollMode.AUTO,
        )
        self.page.update()

    def _common_arguments(self) -> list[str]:
        result = [
            "--output-dir",
            self.output_dir.value.strip() or str(self.default_output_dir),
            "--profile-dir",
            self.profile_dir.value.strip() or str(self.default_profile_dir),
            "--concurrency",
            self.concurrency.value.strip() or "5",
            "--delay",
            self.delay.value.strip() or "0.25",
            "--retries",
            self.retries.value.strip() or "3",
            "--request-timeout",
            self.request_timeout.value.strip() or "60",
            "--captcha-timeout",
            self.captcha_timeout.value.strip() or "300",
        ]
        if self.database_path.value.strip():
            result.extend(["--database", self.database_path.value.strip()])
        if self.refresh_existing.value:
            result.append("--refresh")
        if self.make_snapshot.value:
            result.append("--snapshot")
        return result

    @staticmethod
    def _optional(arguments: list[str], option: str, value: str) -> None:
        if value.strip():
            arguments.extend([option, value.strip()])

    def _arguments(self) -> Any:
        mode = self.mode.value or "members"
        values = [mode, *self._common_arguments()]
        if mode in {"members", "bans"}:
            values.extend(["--start-id", self.start_id.value.strip() or "1"])
            self._optional(values, "--end-id", self.end_id.value)
        elif mode == "punishments":
            for punishment_type in self.punishment_types.value.split(","):
                if punishment_type.strip():
                    values.extend(["--type", punishment_type.strip().casefold()])
            values.extend(["--start-page", self.start_page.value.strip() or "1"])
            self._optional(values, "--end-page", self.end_page.value)
            self._optional(values, "--max-details", self.max_details.value)
            self._optional(values, "--nick", self.nick.value)
            self._optional(values, "--moderator", self.moderator.value)
            self._optional(values, "--since", self.since.value)
            self._optional(values, "--until", self.until.value)
            for status in self.statuses.value.split(","):
                if status.strip():
                    values.extend(["--status", status.strip()])
            if self.index_only.value:
                values.append("--index-only")
        elif mode == "clans":
            values.extend(["--start-page", self.start_page.value.strip() or "1"])
            self._optional(values, "--end-page", self.end_page.value)
            self._optional(values, "--max-clans", self.max_details.value)
            if self.index_only.value:
                values.append("--index-only")
        elif mode in {"forum", "appeals"}:
            values.extend(["--forum-url", self.forum_url.value.strip()])
            values.extend(["--start-page", self.start_page.value.strip() or "1"])
            self._optional(values, "--end-page", self.end_page.value)
            if mode == "forum":
                for prefix in self.prefixes.value.split(","):
                    if prefix.strip():
                        values.extend(["--prefix", prefix.strip()])
                self._optional(values, "--max-thread-pages", self.max_details.value)
                if self.without_prefix.value:
                    values.append("--without-prefix")
                if self.index_only.value:
                    values.append("--index-only")
        elif mode == "all":
            self._optional(values, "--members-end-id", self.end_id.value)
            if self.include_bans.value:
                values.append("--include-bans")
            if self.skip_clans.value:
                values.append("--skip-clans")
            self._optional(values, "--forum-url", self.forum_url.value)
            self._optional(values, "--forum-end-page", self.end_page.value)
            self._optional(values, "--forum-max-thread-pages", self.max_details.value)
        try:
            return build_argument_parser().parse_args(values)
        except SystemExit as error:
            raise ValueError("Проверьте заполненные параметры") from error

    def _update_plan(self, event: ft.Event[Any] | None = None) -> None:
        try:
            plan = describe_plan(self._arguments())
            self.plan_text.value = "План: " + " · ".join(plan)
            self.plan_text.color = ft.Colors.ON_SURFACE_VARIANT
        except Exception as error:
            self.plan_text.value = f"Ошибка параметров: {error}"
            self.plan_text.color = ft.Colors.ERROR
        self.page.update()

    async def _start(self, event: ft.Event[Any]) -> None:
        if self.running:
            return
        try:
            arguments = self._arguments()
        except Exception as error:
            self._notify(str(error), error=True)
            return
        self.control = CrawlControl()
        self.running = True
        self.log_lines.clear()
        self.log_view.controls.clear()
        self.progress.value = 0
        self.progress.visible = True
        self.status_text.value = "Запуск Chrome и проверка доступа…"
        self.start_button.disabled = True
        self.pause_button.disabled = False
        self.cancel_button.disabled = False
        self.page.update()
        try:
            result = await run_from_arguments(
                arguments,
                control=self.control,
                on_progress=self._on_progress,
            )
            self.progress.value = 1
            self.status_text.value = f"Готово. Сохранено записей: {result.get('saved', 0)}"
            self._notify("Сбор данных завершён")
        except CrawlCancelled:
            self.status_text.value = "Остановлено. Уже полученные записи сохранены."
        except Exception as error:
            self.status_text.value = f"Ошибка: {error}"
            self._notify(str(error), error=True)
        finally:
            self.running = False
            self.start_button.disabled = False
            self.pause_button.disabled = True
            self.pause_button.content = "Пауза"
            self.pause_button.icon = ft.Icons.PAUSE
            self.cancel_button.disabled = True
            self.page.update()

    def _on_progress(self, event: ProgressEvent) -> None:
        self.progress.value = event.processed / event.total if event.total else 1
        self.status_text.value = event.message
        self.log_lines.append(event.message)
        self.log_lines = self.log_lines[-100:]
        self.log_view.controls = [ft.Text(line, size=12) for line in self.log_lines]
        self.page.update()

    def _pause(self, event: ft.Event[Any]) -> None:
        if self.control is None:
            return
        if self.control.paused:
            self.control.resume()
            self.pause_button.content = "Пауза"
            self.pause_button.icon = ft.Icons.PAUSE
            self.status_text.value = "Продолжение работы…"
        else:
            self.control.pause()
            self.pause_button.content = "Продолжить"
            self.pause_button.icon = ft.Icons.PLAY_ARROW
            self.status_text.value = "Пауза после текущего пакета страниц"
        self.page.update()

    def _cancel(self, event: ft.Event[Any]) -> None:
        if self.control is not None:
            self.control.cancel()
            self.status_text.value = "Остановка после текущего пакета страниц…"
            self.cancel_button.disabled = True
            self.page.update()

    def _notify(self, message: str, *, error: bool = False) -> None:
        snack = ft.SnackBar(
            message,
            show_close_icon=True,
            bgcolor=ft.Colors.ERROR_CONTAINER if error else None,
            open=True,
        )
        self.page.overlay.append(snack)
        self.page.update()

    def _bar_chart(self, rows: list[dict[str, Any]]) -> ft.Control:
        rows = rows[:10]
        if not rows:
            return ft.Text("Для диаграммы пока нет данных")
        maximum = max(float(row["value"]) for row in rows) or 1
        return fch.BarChart(
            groups=[
                fch.BarChartGroup(
                    x=index,
                    rods=[
                        fch.BarChartRod(
                            to_y=float(row["value"]),
                            color=ft.Colors.INDIGO,
                            width=18,
                            tooltip=f"{row['label']}: {_number(row['value'])}",
                        )
                    ],
                )
                for index, row in enumerate(rows)
            ],
            bottom_axis=fch.ChartAxis(
                labels=[
                    fch.ChartAxisLabel(
                        value=index,
                        label=ft.Text(str(row["label"])[:12], size=10),
                    )
                    for index, row in enumerate(rows)
                ],
                label_size=44,
            ),
            max_y=maximum * 1.15,
            height=300,
        )

    def _show_data(self, event: ft.Event[Any] | None = None) -> None:
        namespaces = self.database.namespaces()
        names = [str(row["namespace"]) for row in namespaces]
        if self.selected_namespace not in names:
            self.selected_namespace = names[0] if names else None
        selector = ft.Dropdown(
            label="Набор данных",
            value=self.selected_namespace,
            options=[ft.DropdownOption(key=name, text=name) for name in names],
            on_select=self._select_namespace,
        )
        body: list[ft.Control] = []
        if self.selected_namespace:
            stats = namespace_statistics(self.database, self.selected_namespace)
            body.append(
                ft.ResponsiveRow(
                    [
                        _metric_card(item["label"], item["value"], ft.Icons.QUERY_STATS)
                        for item in stats["metrics"]
                    ]
                )
            )
            chart_rows = stats["series"] or stats["top"]
            body.append(
                _card(
                    ft.Column(
                        [
                            ft.Text("Аналитика", size=20, weight=ft.FontWeight.W_600),
                            self._bar_chart(chart_rows),
                        ]
                    )
                )
            )
            records = list(self.database.iter_records(self.selected_namespace))[:100]
            body.append(self._records_table(records))
        else:
            body.append(_card(ft.Text("База пока пуста. Сначала выполните сбор данных.")))
        self.content.content = ft.Column(
            [
                self._header(
                    "Данные и аналитика",
                    "Просмотр первых 100 записей, статистика и экспорт CSV.",
                ),
                ft.Row(
                    [
                        ft.Container(selector, expand=True),
                        ft.FilledButton(
                            "Экспорт CSV",
                            icon=ft.Icons.DOWNLOAD,
                            disabled=not bool(self.selected_namespace),
                            on_click=self._export_selected,
                        ),
                    ]
                ),
                *body,
                _card(
                    ft.Column(
                        [
                            ft.Text(
                                "Импорт прежних результатов",
                                size=20,
                                weight=ft.FontWeight.W_600,
                            ),
                            ft.ResponsiveRow(
                                [
                                    ft.Container(self.migrate_jsonl, col=12),
                                    ft.Container(
                                        self.migrate_namespace,
                                        col={"sm": 12, "md": 6},
                                    ),
                                    ft.Container(
                                        self.migrate_key,
                                        col={"sm": 12, "md": 6},
                                    ),
                                ]
                            ),
                            ft.OutlinedButton(
                                "Импортировать JSONL",
                                icon=ft.Icons.UPLOAD_FILE,
                                on_click=self._import_jsonl,
                            ),
                        ]
                    )
                ),
            ],
            spacing=16,
            scroll=ft.ScrollMode.AUTO,
        )
        self.page.update()

    def _select_namespace(self, event: ft.Event[ft.Dropdown]) -> None:
        self.selected_namespace = event.control.value
        self._show_data()

    def _records_table(self, records: list[dict[str, Any]]) -> ft.Card:
        if not records:
            return _card(ft.Text("В выбранном наборе нет записей"))
        preferred = [
            "id",
            "key",
            "username",
            "nickname",
            "title",
            "status",
            "occurred_at",
            "url",
        ]
        fields = []
        all_fields = {key for record in records for key in record}
        for field in preferred + sorted(all_fields):
            if field in all_fields and field not in fields:
                fields.append(field)
        fields = fields[:8]
        table = ft.DataTable(
            columns=[ft.DataColumn(field) for field in fields],
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOWEST,
            border=ft.border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=12,
            data_row_color=_data_row_colors(),
            data_text_style=ft.TextStyle(color=ft.Colors.ON_SURFACE),
            heading_row_color=ft.Colors.SURFACE_CONTAINER_HIGH,
            heading_text_style=ft.TextStyle(
                color=ft.Colors.ON_SURFACE,
                weight=ft.FontWeight.W_600,
            ),
            show_bottom_border=True,
            rows=[
                ft.DataRow(
                    cells=[
                        ft.DataCell(
                            ft.Text(
                                (
                                    json.dumps(record.get(field), ensure_ascii=False)
                                    if isinstance(record.get(field), (dict, list))
                                    else str(record.get(field) or "")
                                )[:100],
                                size=12,
                            )
                        )
                        for field in fields
                    ]
                )
                for record in records
            ],
        )
        return _card(
            ft.Column(
                [
                    ft.Text("Записи", size=20, weight=ft.FontWeight.W_600),
                    ft.Row([table], scroll=ft.ScrollMode.AUTO),
                ]
            )
        )

    def _export_selected(self, event: ft.Event[Any]) -> None:
        if not self.selected_namespace:
            return
        safe_name = self.selected_namespace.replace(":", "_")
        output_dir = Path(self.output_dir.value.strip() or self.default_output_dir)
        path = output_dir / "exports" / f"{safe_name}.csv"
        count = self.database.export_csv(self.selected_namespace, path)
        self._notify(f"Экспортировано {count} записей: {path.resolve()}")

    def _import_jsonl(self, event: ft.Event[Any]) -> None:
        try:
            path = Path(self.migrate_jsonl.value.strip())
            namespace = self.migrate_namespace.value.strip()
            key_field = self.migrate_key.value.strip()
            if not path.is_file() or not namespace or not key_field:
                raise ValueError("Укажите существующий JSONL, имя набора и поле ключа")
            result = self.database.import_jsonl(namespace, path, key_field)
        except Exception as error:
            self._notify(str(error), error=True)
            return
        self.selected_namespace = namespace
        self._notify(
            f"Прочитано {result['read']}, новых {result['new']}, "
            f"изменено {result['changed']}, пропущено ошибок {result['invalid']}"
        )
        self._show_data()

    def _show_snapshots(self) -> None:
        snapshots = self.database.list_snapshots()
        options = [
            ft.DropdownOption(
                key=str(item["id"]),
                text=f"#{item['id']} · {item['namespace']} · {item['name']} ({item['record_count']})",
            )
            for item in snapshots
        ]
        older = ft.Dropdown(label="Старый снимок", options=options, expand=True)
        newer = ft.Dropdown(label="Новый снимок", options=options, expand=True)
        result = ft.Column()

        def compare(event: ft.Event[Any]) -> None:
            if not older.value or not newer.value:
                self._notify("Выберите два снимка", error=True)
                return
            difference = self.database.compare_snapshots(int(older.value), int(newer.value))
            result.controls = [
                ft.ResponsiveRow(
                    [
                        _metric_card(
                            "Добавлено", len(difference["added"]), ft.Icons.ADD_CIRCLE
                        ),
                        _metric_card("Изменено", len(difference["changed"]), ft.Icons.EDIT),
                        _metric_card(
                            "Удалено", len(difference["removed"]), ft.Icons.REMOVE_CIRCLE
                        ),
                    ]
                ),
                _card(
                    ft.Column(
                        [
                            ft.Text("Изменения", size=20, weight=ft.FontWeight.W_600),
                            *[
                                ft.ListTile(
                                    leading=ft.Icon(icon),
                                    title=str(item["key"]),
                                    subtitle=kind,
                                )
                                for kind, icon, items in (
                                    ("Добавлено", ft.Icons.ADD, difference["added"]),
                                    ("Изменено", ft.Icons.EDIT, difference["changed"]),
                                    ("Удалено", ft.Icons.REMOVE, difference["removed"]),
                                )
                                for item in items[:50]
                            ],
                        ]
                    )
                ),
            ]
            self.page.update()

        self.content.content = ft.Column(
            [
                self._header(
                    "Снимки",
                    "Сравнение двух состояний одного набора данных.",
                ),
                _card(
                    ft.Column(
                        [
                            ft.Row([older, newer]),
                            ft.FilledButton(
                                "Сравнить",
                                icon=ft.Icons.COMPARE_ARROWS,
                                on_click=compare,
                            ),
                        ]
                    )
                ),
                result,
            ],
            spacing=16,
            scroll=ft.ScrollMode.AUTO,
        )
        self.page.update()

    def _show_settings(self) -> None:
        theme = _theme_selector(self.page.theme_mode.value)

        def save(event: ft.Event[Any]) -> None:
            selected_theme = next(iter(theme.selected), "system")
            self.page.theme_mode = ft.ThemeMode(selected_theme)
            settings = {
                "output_dir": self.output_dir.value,
                "database_path": self.database_path.value,
                "profile_dir": self.profile_dir.value,
                "concurrency": self.concurrency.value,
                "delay": self.delay.value,
                "retries": self.retries.value,
                "request_timeout": self.request_timeout.value,
                "captcha_timeout": self.captcha_timeout.value,
                "theme": selected_theme,
            }
            self.database.set_setting("gui", settings)
            self._notify("Настройки сохранены")

        self.content.content = ft.Column(
            [
                self._header(
                    "Настройки",
                    "Пути, безопасная скорость работы и оформление приложения.",
                ),
                _card(
                    ft.Column(
                        [
                            ft.Text("Файлы", size=20, weight=ft.FontWeight.W_600),
                            ft.ResponsiveRow(
                                [
                                    ft.Container(self.output_dir, col={"sm": 12, "md": 6}),
                                    ft.Container(self.database_path, col={"sm": 12, "md": 6}),
                                    ft.Container(self.profile_dir, col=12),
                                ]
                            ),
                        ]
                    )
                ),
                _card(
                    ft.Column(
                        [
                            ft.Text("Сеть и браузер", size=20, weight=ft.FontWeight.W_600),
                            ft.ResponsiveRow(
                                [
                                    ft.Container(control, col={"sm": 12, "md": 6, "lg": 3})
                                    for control in (
                                        self.concurrency,
                                        self.delay,
                                        self.retries,
                                        self.request_timeout,
                                        self.captcha_timeout,
                                    )
                                ]
                            ),
                            ft.Text(
                                "Рекомендуется 3–5 страниц одновременно. Проверка Cloudflare проходится только вручную в Chrome.",
                                color=ft.Colors.ON_SURFACE_VARIANT,
                            ),
                        ]
                    )
                ),
                _card(
                    ft.Column(
                        [
                            ft.Text("Оформление", size=20, weight=ft.FontWeight.W_600),
                            ft.Text(
                                "Системная тема повторяет текущую тему Windows. Изменение "
                                "применится сразу после сохранения.",
                                color=ft.Colors.ON_SURFACE_VARIANT,
                            ),
                            theme,
                            ft.FilledButton("Сохранить", icon=ft.Icons.SAVE, on_click=save),
                        ],
                        spacing=16,
                    )
                ),
            ],
            spacing=16,
            scroll=ft.ScrollMode.AUTO,
        )
        self.page.update()


async def app_main(page: ft.Page) -> None:
    TeslaCraftApp(page).mount()
    await page.window.center()


def run_gui() -> None:
    ft.run(app_main, name=APP_TITLE, assets_dir="assets")
