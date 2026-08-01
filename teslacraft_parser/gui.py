from __future__ import annotations

import asyncio
import json
import os
import sys
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any

import flet as ft

from .analytics import namespace_statistics, namespace_tables
from .constants import (
    DEFAULT_FORUM_SCAN_END_ID,
    DEFAULT_FORUM_URL,
    KNOWN_FORUM_SECTIONS,
)
from .database import ParserDatabase
from .gui_state import (
    find_saved_results,
    import_saved_jsonl,
    load_gui_settings,
    prepare_gui_storage,
    save_gui_settings,
)

if TYPE_CHECKING:
    from .crawler import CrawlControl, ProgressEvent

APP_TITLE = "TeslaParser"
APP_DATA_DIRECTORY = "TeslaParser"
LEGACY_APP_DATA_DIRECTORIES = ("TeslaCraftParser",)
WINDOW_ICON = "icon.ico"
WINDOW_WIDTH = 1180
WINDOW_HEIGHT = 760
MODE_LABELS = {
    "members": "Игроки и рейтинги",
    "punishments": "Наказания",
    "clans": "Кланы",
    "forum": "Раздел форума",
    "all": "Комплексный запуск",
}
FIELD_LABELS = {
    "id": "ID",
    "key": "Ключ",
    "username": "Игрок",
    "nickname": "Ник",
    "title": "Название",
    "status": "Статус",
    "occurred_at": "Дата",
    "registered_at": "Регистрация",
    "positive": "Положительный",
    "neutral": "Нейтральный",
    "negative": "Отрицательный",
    "moderator": "Блюститель",
    "reason": "Причина",
    "url": "Ссылка",
    "profile_url": "Профиль",
    "requested_url": "Проверенный адрес",
    "page": "Страница",
    "category": "Категория",
    "kind": "Тип",
    "level": "Уровень",
    "node_id": "ID раздела",
    "parent": "Родительский раздел",
    "author": "Автор",
    "authors": "Авторы",
    "created_at": "Создана",
    "replies": "Ответы",
    "views": "Просмотры",
    "last_poster": "Последний автор",
    "last_post_at": "Последнее сообщение",
    "total_pages": "Страниц",
    "prefix": "Префикс",
    "prefix_id": "ID префикса",
    "prefix_name": "Префикс",
    "thread_count": "Тем в сохранённых страницах",
    "is_sticky": "Закреплена",
    "is_locked": "Закрыта",
    "ip": "IP / идентификатор",
    "type": "Тип",
    "detail_url": "Карточка",
    "status_text": "Статус на сайте",
    "clan": "Клан",
    "member_count": "Участников",
    "score": "Очки",
    "messages": "Сообщений",
    "unique_authors": "Уникальных авторов",
    "label": "Показатель",
    "value": "Количество",
}
VALUE_LABELS = {
    "ok": "Получено",
    "missing": "Не найдено",
    "active": "Активно",
    "lifted": "Снято",
    "recorded": "Записано",
    "ban": "Бан",
    "kick": "Кик",
    "mute": "Мут",
    "warn": "Предупреждение",
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
            "В одном режиме можно прочитать актуальные баны, кики, муты и "
            "предупреждения, исторический архив банов по ID либо оба источника."
        ),
        "result": "Общие наборы наказаний, подробные карточки и историческая статистика.",
        "note": (
            "Фильтры по нику, блюстителю, датам и статусу относятся к текущему "
            "банлисту. Для архива задаётся отдельный диапазон ID."
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
            "Можно выбрать готовый раздел, вставить свой URL либо найти все "
            "существующие разделы перебором ID. Затем собираются темы, префиксы, "
            "авторы, ответы и просмотры."
        ),
        "result": "Карта форума, каталог тем, доступные префиксы и топ авторов.",
        "note": (
            "При выборе всех разделов сначала проверяются числовые ID. Страницы с "
            "заголовком ошибки не добавляются."
        ),
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


def _namespace_label(namespace: str) -> str:
    fixed = {
        "members": "Игроки и рейтинги",
        "bans:historical": "Исторические баны",
        "clans:index": "Список кланов",
        "clans:details": "Кланы и участники",
        "forum:sections": "Карта форума",
        "forum:discovery": "Проверка ID разделов форума",
    }
    if namespace in fixed:
        return fixed[namespace]
    parts = namespace.split(":")
    if len(parts) >= 4 and parts[0] == "punishments":
        punishment = {
            "ban": "Баны",
            "kick": "Кики",
            "mute": "Муты",
            "warn": "Предупреждения",
        }.get(parts[1], parts[1])
        kind = {
            "index": "страницы",
            "entries": "список",
            "details": "карточки",
        }.get(parts[-1], parts[-1])
        filter_name = "все" if parts[2] == "all" else parts[2]
        return f"{punishment} — {kind} ({filter_name})"
    if len(parts) >= 3 and parts[0] == "forum":
        kind = {"index": "темы", "pages": "страницы тем"}.get(parts[-1], parts[-1])
        return f"Форум {parts[1]} — {kind}"
    return namespace


def _display_value(value: Any, *, limit: int = 100) -> str:
    if value is None:
        text = ""
    elif isinstance(value, bool):
        text = "Да" if value else "Нет"
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    return text[:limit]


def _default_gui_root() -> Path:
    """Use a writable, stable directory when the GUI runs as a packaged app."""

    if not getattr(sys, "frozen", False):
        return Path.cwd()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / APP_DATA_DIRECTORY
    return Path.home() / APP_DATA_DIRECTORY


def _legacy_gui_roots() -> list[Path]:
    """Return read-only compatibility locations used by older releases."""

    if not getattr(sys, "frozen", False):
        return []
    local_app_data = os.environ.get("LOCALAPPDATA")
    parent = Path(local_app_data) if local_app_data else Path.home()
    return [parent / name for name in LEGACY_APP_DATA_DIRECTORIES]


def _path_from_text(value: str, default: Path, *, base: Path | None = None) -> Path:
    cleaned = str(value or "").strip().strip("\"'")
    expanded = os.path.expandvars(os.path.expanduser(cleaned)) if cleaned else str(default)
    path = Path(expanded)
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    return path.resolve()


def _startup_loader() -> ft.Container:
    """Create a fresh animated first-frame loading view."""

    return ft.Container(
        expand=True,
        alignment=ft.Alignment.CENTER,
        content=ft.Column(
            [
                ft.Icon(ft.Icons.TRAVEL_EXPLORE, size=52, color=ft.Colors.PRIMARY),
                ft.Text(APP_TITLE, size=24, weight=ft.FontWeight.W_600),
                ft.ProgressRing(width=34, height=34, stroke_width=4),
                ft.Text(
                    "Загрузка данных…",
                    color=ft.Colors.ON_SURFACE_VARIANT,
                    text_align=ft.TextAlign.CENTER,
                ),
            ],
            alignment=ft.MainAxisAlignment.CENTER,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=16,
            tight=True,
        ),
    )


def _card(content: ft.Control, *, col: int | dict[str, int] = 12) -> ft.Card:
    return ft.Card(
        content=ft.Container(content=content, padding=20),
        col=col,
        variant=ft.CardVariant.OUTLINED,
        bgcolor=ft.Colors.SURFACE_CONTAINER_LOWEST,
        elevation=0,
    )


def _metric_card(label: str, value: Any, icon: ft.IconData) -> ft.Card:
    return _card(
        ft.Row(
            [
                ft.Container(
                    ft.Icon(icon, size=26, color=ft.Colors.ON_SECONDARY_CONTAINER),
                    width=48,
                    height=48,
                    alignment=ft.Alignment.CENTER,
                    border_radius=14,
                    bgcolor=ft.Colors.SECONDARY_CONTAINER,
                ),
                ft.Column(
                    [
                        ft.Text(
                            _number(value),
                            size=27,
                            weight=ft.FontWeight.W_600,
                            color=ft.Colors.ON_SURFACE,
                        ),
                        ft.Text(
                            label,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                            size=13,
                        ),
                    ],
                    spacing=2,
                    alignment=ft.MainAxisAlignment.CENTER,
                ),
            ],
            spacing=16,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
            height=64,
        ),
        col={"sm": 12, "md": 6, "lg": 3},
    )


def _metric_icon(label: str) -> ft.IconData:
    return {
        "Карточек": ft.Icons.BADGE,
        "Найдено игроков": ft.Icons.GROUP,
        "Отсутствуют": ft.Icons.PERSON_OFF,
        "Наказаний": ft.Icons.GAVEL,
        "Страниц списка": ft.Icons.VIEW_LIST,
        "Страниц раздела": ft.Icons.MENU_BOOK,
        "Страниц тем": ft.Icons.ARTICLE,
        "Тем": ft.Icons.FORUM,
        "Ответов": ft.Icons.CHAT_BUBBLE,
        "Просмотров": ft.Icons.VISIBILITY,
        "Кланов": ft.Icons.GROUPS,
        "Участников": ft.Icons.PEOPLE,
        "Проверено ID": ft.Icons.FACT_CHECK,
        "Разделов": ft.Icons.ACCOUNT_TREE,
        "Категорий": ft.Icons.CATEGORY,
        "Сообщений": ft.Icons.MESSAGE,
        "Авторов": ft.Icons.PERSON,
        "Записей": ft.Icons.TABLE_ROWS,
    }.get(label, ft.Icons.QUERY_STATS)


def _scroll_view(controls: list[ft.Control], *, spacing: int = 16) -> ft.ListView:
    """Keep scrollbars away from cards and provide consistent page spacing."""

    return ft.ListView(
        controls=controls,
        spacing=spacing,
        padding=ft.Padding.only(right=14, bottom=24),
        expand=True,
        scroll=ft.ScrollMode.AUTO,
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
    return ft.Theme(
        color_scheme=ft.ColorScheme(**colors),
        use_material3=True,
        scrollbar_theme=ft.ScrollbarTheme(
            thickness=7,
            radius=8,
            thumb_color=ft.Colors.OUTLINE,
            track_color=ft.Colors.TRANSPARENT,
            cross_axis_margin=3,
            main_axis_margin=8,
        ),
    )


def _data_row_colors() -> dict[ft.ControlState, ft.ColorValue]:
    """Keep text readable in every interactive state of a data row."""

    return {
        ft.ControlState.DEFAULT: ft.Colors.SURFACE_CONTAINER_LOWEST,
        ft.ControlState.HOVERED: ft.Colors.SURFACE_CONTAINER_HIGHEST,
        ft.ControlState.FOCUSED: ft.Colors.SECONDARY_CONTAINER,
        ft.ControlState.PRESSED: ft.Colors.SECONDARY_CONTAINER,
        ft.ControlState.SELECTED: ft.Colors.SECONDARY_CONTAINER,
    }


class TeslaParserApp:
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        preferred_data_root = _default_gui_root()
        legacy_data_roots = _legacy_gui_roots()
        storage = (
            prepare_gui_storage(preferred_data_root, legacy_data_roots)
            if getattr(sys, "frozen", False)
            else None
        )
        self.data_root = storage.active_root if storage is not None else preferred_data_root
        self.legacy_data_roots = [
            root for root in legacy_data_roots if root != self.data_root
        ]
        self.default_output_dir = self.data_root / "results"
        self.default_profile_dir = self.data_root / "user-data"
        self.settings_path = self.data_root / "gui-settings.json"
        self.file_picker = ft.FilePicker()
        self._checked_database_paths: set[Path] = set()
        self._auto_import_dirs: list[Path] = []
        self._last_import_result: dict[str, Any] | None = None
        self._database_follows_output = True
        self._storage_explicit = False
        self._database_cache: ParserDatabase | None = None
        self.control: CrawlControl | None = None
        self.running = False
        self.selected_namespace: str | None = None
        self.log_lines: list[str] = []
        if storage is not None and storage.migrated_from is not None:
            self.log_lines.append(
                f"Данные перенесены из {storage.migrated_from} в {self.data_root}."
            )
        if storage is not None and storage.warning:
            self.log_lines.append(storage.warning)
        self._forum_url_value = DEFAULT_FORUM_URL
        self._forum_targets = {
            key: (title, url) for key, title, url in KNOWN_FORUM_SECTIONS
        }
        self._plan_revision = 0

        self.output_dir = ft.TextField(
            label="Папка результатов",
            value=str(self.default_output_dir),
            helper="Сюда записываются отчёты, CSV и журнал ошибок.",
            on_change=self._output_dir_changed,
        )
        self.database_path = ft.TextField(
            label="SQLite-база",
            value=str(self.default_output_dir / "teslacraft.db"),
            helper="Основное хранилище: текущие записи, снимки и история запусков.",
            on_change=self._database_path_changed,
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
        self.forum_url = ft.TextField(
            label="Свой URL раздела",
            value=DEFAULT_FORUM_URL,
            on_change=self._forum_url_changed,
        )
        self.forum_target = ft.Dropdown(
            label="Какой раздел обработать",
            value="flud",
            options=[
                ft.DropdownOption(key="__all__", text="Все существующие разделы"),
                *[
                    ft.DropdownOption(key=key, text=title)
                    for key, (title, _url) in self._forum_targets.items()
                ],
                ft.DropdownOption(key="__custom__", text="Другой URL…"),
            ],
            on_select=self._forum_target_changed,
        )
        self.section_start_id = ft.TextField(
            label="Первый ID раздела",
            value="1",
        )
        self.section_end_id = ft.TextField(
            label="Последний ID раздела",
            value=str(DEFAULT_FORUM_SCAN_END_ID),
            helper="Обычно достаточно 150; значение можно увеличить.",
        )
        self.punishment_source = ft.Dropdown(
            label="Источник наказаний",
            value="current",
            options=[
                ft.DropdownOption(key="current", text="Текущий банлист"),
                ft.DropdownOption(key="historical", text="Исторический архив"),
                ft.DropdownOption(key="all", text="Оба источника"),
            ],
            on_select=self._mode_changed,
        )
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

        for parameter in (
            self.output_dir,
            self.database_path,
            self.profile_dir,
            self.concurrency,
            self.delay,
            self.retries,
            self.request_timeout,
            self.captcha_timeout,
            self.start_id,
            self.end_id,
            self.start_page,
            self.end_page,
            self.forum_url,
            self.section_start_id,
            self.section_end_id,
            self.punishment_types,
            self.nick,
            self.moderator,
            self.since,
            self.until,
            self.statuses,
            self.prefixes,
            self.max_details,
            self.index_only,
            self.without_prefix,
            self.refresh_existing,
            self.make_snapshot,
            self.include_bans,
            self.skip_clans,
        ):
            parameter.on_change = self._parameter_changed

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
        self._mode_changed(update_plan=False)

    @property
    def database(self) -> ParserDatabase:
        output_dir = _path_from_text(
            self.output_dir.value,
            self.default_output_dir,
            base=self.data_root,
        )
        database_path = _path_from_text(
            self.database_path.value,
            output_dir / "teslacraft.db",
            base=self.data_root,
        )
        if database_path.is_dir():
            database_path /= "teslacraft.db"
        if self._database_cache is None or self._database_cache.path != database_path:
            self._database_cache = ParserDatabase(database_path)
        return self._database_cache

    def _database_path_changed(self, event: ft.Event[ft.TextField]) -> None:
        self._database_follows_output = False
        self._storage_explicit = True

    def _output_dir_changed(self, event: ft.Event[ft.TextField]) -> None:
        self._storage_explicit = True

    def _forum_url_changed(self, event: ft.Event[ft.TextField]) -> None:
        if self.mode.value == "forum":
            self._forum_url_value = event.control.value or DEFAULT_FORUM_URL

    def _forum_target_changed(self, event: ft.Event[ft.Dropdown]) -> None:
        if self.forum_target.value == "__all__":
            self.index_only.value = True
        self._mode_changed(event)

    def _selected_forum_url(self) -> str:
        selected = self.forum_target.value or "flud"
        if selected == "__custom__":
            return self.forum_url.value.strip() or DEFAULT_FORUM_URL
        return self._forum_targets.get(selected, ("", DEFAULT_FORUM_URL))[1]

    def _sync_forum_targets(self, database: ParserDatabase) -> None:
        known_urls = {url for _title, url in self._forum_targets.values()}
        for record in database.iter_records("forum:sections"):
            for section in record.get("sections") or []:
                if not isinstance(section, dict) or section.get("kind") != "forum":
                    continue
                title = str(section.get("title") or "").strip()
                url = str(section.get("url") or "").strip()
                node_id = section.get("node_id")
                if not title or not url or node_id is None or url in known_urls:
                    continue
                self._forum_targets[f"section:{node_id}"] = (title, url)
                known_urls.add(url)
        dynamic = sorted(
            self._forum_targets.items(),
            key=lambda item: item[1][0].casefold(),
        )
        self.forum_target.options = [
            ft.DropdownOption(key="__all__", text="Все существующие разделы"),
            *[
                ft.DropdownOption(key=key, text=title)
                for key, (title, _url) in dynamic
            ],
            ft.DropdownOption(key="__custom__", text="Другой URL…"),
        ]

    def _normalise_storage_paths(self) -> tuple[Path, Path, Path]:
        output_dir = _path_from_text(
            self.output_dir.value,
            self.default_output_dir,
            base=self.data_root,
        )
        database_path = (
            output_dir / "teslacraft.db"
            if self._database_follows_output
            else _path_from_text(
                self.database_path.value,
                output_dir / "teslacraft.db",
                base=self.data_root,
            )
        )
        if database_path.is_dir():
            database_path /= "teslacraft.db"
        profile_dir = _path_from_text(
            self.profile_dir.value,
            self.default_profile_dir,
            base=self.data_root,
        )
        self.output_dir.value = str(output_dir)
        self.database_path.value = str(database_path)
        self.profile_dir.value = str(profile_dir)
        return output_dir, database_path, profile_dir

    def _settings_payload(self, theme: str | None = None) -> dict[str, Any]:
        return {
            "output_dir": self.output_dir.value,
            "database_path": self.database_path.value,
            "profile_dir": self.profile_dir.value,
            "concurrency": self.concurrency.value,
            "delay": self.delay.value,
            "retries": self.retries.value,
            "request_timeout": self.request_timeout.value,
            "captcha_timeout": self.captcha_timeout.value,
            "punishment_source": self.punishment_source.value,
            "forum_target": self.forum_target.value,
            "forum_url": self.forum_url.value,
            "database_follows_output": self._database_follows_output,
            "storage_explicit": self._storage_explicit,
            "selected_namespace": self.selected_namespace or "",
            "theme": theme or self.page.theme_mode.value,
        }

    def _save_gui_settings(self, theme: str | None = None) -> None:
        _, database_path, _ = self._normalise_storage_paths()
        self._database_cache = ParserDatabase(database_path)
        save_gui_settings(self.settings_path, self._settings_payload(theme))
        self._checked_database_paths.discard(database_path.resolve())

    def _database_with_saved_data(self) -> ParserDatabase:
        database = self.database
        if database.path in self._checked_database_paths:
            return database
        self._last_import_result = None
        output_dir = _path_from_text(
            self.output_dir.value,
            self.default_output_dir,
            base=self.data_root,
        )
        saved_manifest = database.get_setting("legacy_jsonl_manifest", {})
        manifest = saved_manifest if isinstance(saved_manifest, dict) else {}
        imported: dict[str, Any] = {
            "files": 0,
            "read": 0,
            "new": 0,
            "changed": 0,
            "invalid": 0,
            "errors": [],
            "signatures": {},
        }
        directories = list(
            dict.fromkeys(
                [output_dir.resolve(), *(path.resolve() for path in self._auto_import_dirs)]
            )
        )
        active_manifest = dict(manifest)
        for directory in directories:
            current = import_saved_jsonl(
                database,
                directory,
                only_missing_namespaces=directory != output_dir.resolve(),
                known_signatures=active_manifest,
            )
            for key in ("files", "read", "new", "changed", "invalid"):
                imported[key] += int(current[key])
            imported["errors"].extend(current["errors"])
            imported["signatures"].update(current["signatures"])
            active_manifest.update(current["signatures"])
        if imported["signatures"]:
            database.set_setting(
                "legacy_jsonl_manifest",
                active_manifest,
            )
        if imported["files"] or imported["invalid"] or imported["errors"]:
            self._last_import_result = imported
        self._checked_database_paths.add(database.path)
        return database

    def _load_settings(self) -> None:
        settings = load_gui_settings(self.settings_path)
        legacy_settings_root: Path | None = None
        if not settings:
            for legacy_root in self.legacy_data_roots:
                legacy_settings = load_gui_settings(legacy_root / "gui-settings.json")
                if legacy_settings:
                    settings = legacy_settings
                    legacy_settings_root = legacy_root
                    break
        try:
            if not settings and Path(self.database_path.value).is_file():
                database = ParserDatabase(Path(self.database_path.value))
                settings = database.get_setting("gui", {})
        except Exception:
            settings = {}
        controls = {
            "output_dir": self.output_dir,
            "database_path": self.database_path,
            "profile_dir": self.profile_dir,
            "concurrency": self.concurrency,
            "delay": self.delay,
            "retries": self.retries,
            "request_timeout": self.request_timeout,
            "captcha_timeout": self.captcha_timeout,
            "punishment_source": self.punishment_source,
            "forum_target": self.forum_target,
            "forum_url": self.forum_url,
        }
        for key, control in controls.items():
            if key in settings:
                control.value = str(settings[key])
        if "profile_dir" not in settings:
            for legacy_root in self.legacy_data_roots:
                legacy_profile = legacy_root / "user-data"
                if legacy_profile.is_dir():
                    self.profile_dir.value = str(legacy_profile.resolve())
                    break
        self._forum_url_value = self.forum_url.value or DEFAULT_FORUM_URL
        theme = settings.get("theme")
        if theme in {"light", "dark", "system"}:
            self.page.theme_mode = ft.ThemeMode(theme)
        saved_namespace = settings.get("selected_namespace")
        if isinstance(saved_namespace, str) and saved_namespace:
            self.selected_namespace = saved_namespace
        stored_follow_mode = settings.get("database_follows_output")
        if isinstance(stored_follow_mode, bool):
            self._database_follows_output = stored_follow_mode
        else:
            output_dir = _path_from_text(
                self.output_dir.value,
                self.default_output_dir,
                base=self.data_root,
            )
            database_path = _path_from_text(
                self.database_path.value,
                output_dir / "teslacraft.db",
                base=self.data_root,
            )
            self._database_follows_output = database_path == output_dir / "teslacraft.db"

        configured_output = _path_from_text(
            self.output_dir.value,
            self.default_output_dir,
            base=self.data_root,
        )
        stored_explicit = settings.get("storage_explicit")
        if isinstance(stored_explicit, bool):
            self._storage_explicit = stored_explicit
        else:
            # Older settings did not record this flag. A non-default path was
            # almost certainly chosen intentionally and must not be replaced.
            self._storage_explicit = configured_output != self.default_output_dir.resolve()

        if not self._storage_explicit:
            candidates = [
                configured_output,
                self.default_output_dir,
                *(root / "results" for root in self.legacy_data_roots),
                Path.cwd() / "results",
            ]
            if getattr(sys, "frozen", False):
                executable_dir = Path(sys.executable).resolve().parent
                candidates.extend(
                    [
                        executable_dir / "results",
                        executable_dir.parent / "results",
                        executable_dir.parent.parent / "results",
                    ]
                )
            self._auto_import_dirs = list(
                dict.fromkeys(path.resolve() for path in candidates if path.is_dir())
            )
            detected = find_saved_results(candidates)
            if detected is not None:
                self.output_dir.value = str(detected)
                self.database_path.value = str(detected / "teslacraft.db")
                self._database_follows_output = True
                with suppress(OSError):
                    save_gui_settings(self.settings_path, self._settings_payload())
        if legacy_settings_root is not None:
            # Keep existing databases and browser profiles in place, but copy the
            # lightweight settings into the new application directory. Nothing in
            # the legacy directory is deleted or overwritten.
            with suppress(OSError):
                save_gui_settings(self.settings_path, self._settings_payload())

    def _refresh_storage_view(self) -> None:
        self.selected_namespace = None
        index = self.navigation.selected_index or 0
        if index == 0:
            self._show_overview()
        elif index == 2:
            self._show_snapshots()
        else:
            self.page.update()

    async def _choose_results_directory(self, event: ft.Event[Any]) -> None:
        current = _path_from_text(
            self.output_dir.value,
            self.default_output_dir,
            base=self.data_root,
        )
        try:
            selected = await self.file_picker.get_directory_path(
                dialog_title="Выберите папку с результатами TeslaParser",
                initial_directory=str(
                    current
                    if current.is_dir()
                    else self.data_root
                    if self.data_root.is_dir()
                    else Path.home()
                ),
            )
        except Exception as error:
            self._notify(f"Не удалось открыть выбор папки: {error}", error=True)
            return
        if not selected:
            return
        output_dir = Path(selected).resolve()
        self.output_dir.value = str(output_dir)
        self.database_path.value = str(output_dir / "teslacraft.db")
        self._database_follows_output = True
        self._storage_explicit = True
        try:
            self._save_gui_settings()
        except Exception as error:
            self._notify(f"Не удалось открыть папку: {error}", error=True)
            return
        self._notify(f"Открыта папка результатов: {output_dir}")
        self._refresh_storage_view()

    async def _choose_database_file(self, event: ft.Event[Any]) -> None:
        current = _path_from_text(
            self.database_path.value,
            self.default_output_dir,
            base=self.data_root,
        )
        try:
            files = await self.file_picker.pick_files(
                dialog_title="Выберите базу TeslaParser",
                initial_directory=str(
                    current.parent
                    if current.parent.is_dir()
                    else self.data_root
                    if self.data_root.is_dir()
                    else Path.home()
                ),
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["db", "sqlite", "sqlite3"],
            )
        except Exception as error:
            self._notify(f"Не удалось открыть выбор базы: {error}", error=True)
            return
        if not files or not files[0].path:
            return
        database_path = Path(files[0].path).resolve()
        self.database_path.value = str(database_path)
        self.output_dir.value = str(database_path.parent)
        self._database_follows_output = False
        self._storage_explicit = True
        try:
            self._save_gui_settings()
        except Exception as error:
            self._notify(f"Не удалось открыть базу: {error}", error=True)
            return
        self._notify(f"Открыта база: {database_path}")
        self._refresh_storage_view()

    async def _choose_jsonl_file(self, event: ft.Event[Any]) -> None:
        current = _path_from_text(
            self.migrate_jsonl.value,
            _path_from_text(
                self.output_dir.value,
                self.default_output_dir,
                base=self.data_root,
            ),
            base=self.data_root,
        )
        try:
            files = await self.file_picker.pick_files(
                dialog_title="Выберите JSONL-файл для импорта",
                initial_directory=str(
                    current
                    if current.is_dir()
                    else current.parent
                    if current.parent.is_dir()
                    else self.data_root
                    if self.data_root.is_dir()
                    else Path.home()
                ),
                file_type=ft.FilePickerFileType.CUSTOM,
                allowed_extensions=["jsonl"],
            )
        except Exception as error:
            self._notify(f"Не удалось открыть выбор JSONL: {error}", error=True)
            return
        if files and files[0].path:
            self.migrate_jsonl.value = str(Path(files[0].path).resolve())
            self.page.update()

    def _storage_error_card(self, error: Exception) -> ft.Card:
        return _card(
            ft.Column(
                [
                    ft.Text("Не удалось открыть сохранённые данные", size=20),
                    ft.Text(str(error), color=ft.Colors.ERROR),
                    ft.Text(
                        f"Проверяемая база: {self.database_path.value}",
                        selectable=True,
                    ),
                    ft.OutlinedButton(
                        "Выбрать другую базу",
                        icon=ft.Icons.FOLDER_OPEN,
                        on_click=self._choose_database_file,
                    ),
                ],
                spacing=12,
            )
        )

    def mount(self) -> None:
        self.page.title = APP_TITLE
        self.page.on_close = self._page_closed
        self.page.on_disconnect = self._page_closed
        self.page.theme = _app_theme()
        self.page.dark_theme = _app_theme(dark=True)
        self.page.bgcolor = ft.Colors.SURFACE
        self.page.padding = 0
        self.page.window.width = WINDOW_WIDTH
        self.page.window.height = WINDOW_HEIGHT
        self.page.window.min_width = 980
        self.page.window.min_height = 680
        self.content.content = _startup_loader()
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

    def _page_closed(self, event: ft.Event[Any] | None = None) -> None:
        if self.control is not None:
            self.control.cancel()

    def _navigate(self, event: ft.Event[ft.NavigationRail] | None = None) -> None:
        index = self.navigation.selected_index or 0
        pages = [
            self._show_overview,
            self._show_runner,
            self._show_snapshots,
            self._show_settings,
        ]
        pages[index]()

    def _open_settings(self, event: ft.Event[Any] | None = None) -> None:
        self.navigation.selected_index = 3
        self._show_settings()

    def _header(self, title: str, subtitle: str) -> ft.Column:
        return ft.Column(
            [
                ft.Text(title, size=32, weight=ft.FontWeight.W_600),
                ft.Text(subtitle, color=ft.Colors.ON_SURFACE_VARIANT),
            ],
            spacing=4,
        )

    def _mode_changed(
        self,
        event: ft.Event[Any] | None = None,
        *,
        update_plan: bool = True,
    ) -> None:
        mode = self.mode.value or "members"
        controls: list[ft.Control]
        if mode == "members":
            self.start_id.label = "Начальный ID игрока"
            self.end_id.label = "Последний ID игрока"
            controls = [self.start_id, self.end_id]
        elif mode == "punishments":
            source = self.punishment_source.value or "current"
            self.start_id.label = "Первый ID исторического бана"
            self.end_id.label = "Последний ID исторического бана"
            current_controls: list[ft.Control] = [
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
            historical_controls: list[ft.Control] = [self.start_id, self.end_id]
            controls = [self.punishment_source]
            if source != "historical":
                controls.extend(current_controls)
            if source != "current":
                controls.extend(historical_controls)
        elif mode == "clans":
            controls = [self.start_page, self.end_page, self.max_details, self.index_only]
        elif mode == "forum":
            controls = [self.forum_target]
            if self.forum_target.value == "__custom__":
                self.forum_url.value = self._forum_url_value
                controls.append(self.forum_url)
            if self.forum_target.value == "__all__":
                controls.extend([self.section_start_id, self.section_end_id])
            controls.extend(
                [
                    self.start_page,
                    self.end_page,
                    self.prefixes,
                    self.without_prefix,
                    self.max_details,
                    self.index_only,
                ]
            )
        elif mode == "all":
            self.forum_url.value = self._forum_url_value
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
        self.refresh_existing.visible = True
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
        if update_plan:
            self._update_plan()

    def _show_runner(self) -> None:
        self._mode_changed()
        self.content.content = _scroll_view(
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
            ]
        )
        self.page.update()

    def _common_arguments(self) -> list[str]:
        output_dir, database_path, profile_dir = self._normalise_storage_paths()
        result = [
            "--output-dir",
            str(output_dir),
            "--profile-dir",
            str(profile_dir),
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
            "--database",
            str(database_path),
        ]
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
        from .cli import build_argument_parser

        mode = self.mode.value or "members"
        values = [mode, *self._common_arguments()]
        if mode == "members":
            values.extend(["--start-id", self.start_id.value.strip() or "1"])
            self._optional(values, "--end-id", self.end_id.value)
        elif mode == "punishments":
            source = self.punishment_source.value or "current"
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
            if source == "all":
                values.append("--include-historical")
            elif source == "historical":
                values.append("--historical-only")
            if source != "current":
                values.extend(
                    ["--historical-start-id", self.start_id.value.strip() or "1"]
                )
                self._optional(values, "--historical-end-id", self.end_id.value)
        elif mode == "clans":
            values.extend(["--start-page", self.start_page.value.strip() or "1"])
            self._optional(values, "--end-page", self.end_page.value)
            self._optional(values, "--max-clans", self.max_details.value)
            if self.index_only.value:
                values.append("--index-only")
        elif mode == "forum":
            values.extend(["--forum-url", self._selected_forum_url()])
            values.extend(["--start-page", self.start_page.value.strip() or "1"])
            self._optional(values, "--end-page", self.end_page.value)
            for prefix in self.prefixes.value.split(","):
                if prefix.strip():
                    values.extend(["--prefix", prefix.strip()])
            self._optional(values, "--max-thread-pages", self.max_details.value)
            if self.without_prefix.value:
                values.append("--without-prefix")
            if self.index_only.value:
                values.append("--index-only")
            if self.forum_target.value == "__all__":
                values.append("--all-sections")
                values.extend(
                    ["--section-start-id", self.section_start_id.value.strip() or "1"]
                )
                values.extend(
                    [
                        "--section-end-id",
                        self.section_end_id.value.strip()
                        or str(DEFAULT_FORUM_SCAN_END_ID),
                    ]
                )
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
        from .cli import describe_plan

        try:
            plan = describe_plan(self._arguments())
            self.plan_text.value = "План: " + " · ".join(plan)
            self.plan_text.color = ft.Colors.ON_SURFACE_VARIANT
        except Exception as error:
            self.plan_text.value = f"Ошибка параметров: {error}"
            self.plan_text.color = ft.Colors.ERROR
        self.page.update()

    async def _parameter_changed(self, event: ft.Event[Any]) -> None:
        """Keep the launch plan in sync without a separate refresh button."""

        if event.control is self.output_dir:
            self._output_dir_changed(event)
        elif event.control is self.database_path:
            self._database_path_changed(event)
        elif event.control is self.forum_url:
            self._forum_url_changed(event)

        self._plan_revision += 1
        revision = self._plan_revision
        await asyncio.sleep(0.12)
        if revision == self._plan_revision:
            self._update_plan()

    async def _start(self, event: ft.Event[Any]) -> None:
        from .cli import run_from_arguments
        from .crawler import CrawlCancelled, CrawlControl

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
            self._sync_forum_targets(self.database)
            self.progress.value = 1
            self.status_text.value = f"Готово. Сохранено записей: {result.get('saved', 0)}"
            self._notify("Сбор данных завершён")
        except CrawlCancelled:
            self.status_text.value = (
                "Остановлено безопасно. Готовые пачки сохранены, "
                "незавершённая будет повторена при следующем запуске."
            )
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
            self.control = None
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
            self.status_text.value = (
                "Безопасно останавливаю… Незавершённая пачка не будет сохранена."
            )
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

    def _show_overview(self, event: ft.Event[Any] | None = None) -> None:
        try:
            database = self._database_with_saved_data()
            self._sync_forum_targets(database)
            namespaces = database.namespaces()
            summary = database.summary_counts()
        except Exception as error:
            self.content.content = _scroll_view(
                [
                    self._header(
                        "Обзор",
                        "Сохранённые данные, краткая статистика и экспорт CSV.",
                    ),
                    self._storage_error_card(error),
                ]
            )
            self.page.update()
            return
        namespaces = sorted(
            namespaces,
            key=lambda row: _namespace_label(str(row["namespace"])).casefold(),
        )
        names = [str(row["namespace"]) for row in namespaces]
        if self.selected_namespace not in names:
            self.selected_namespace = names[0] if names else None
        selector = ft.Dropdown(
            label="Набор данных",
            value=self.selected_namespace,
            options=[
                ft.DropdownOption(
                    key=str(row["namespace"]),
                    text=_namespace_label(str(row["namespace"])),
                )
                for row in namespaces
            ],
            on_select=self._select_namespace,
        )
        body: list[ft.Control] = []
        if self._last_import_result and self._last_import_result["read"]:
            imported = self._last_import_result
            body.append(
                _card(
                    ft.Text(
                        f"Прежние выгрузки загружены автоматически: {imported['read']} "
                        f"записей из {imported['files']} JSONL-файлов. Исходники сохранены."
                    )
                )
            )
        if self._last_import_result and self._last_import_result["errors"]:
            body.append(
                _card(
                    ft.Text(
                        "Не удалось загрузить часть прежних файлов: "
                        + " · ".join(self._last_import_result["errors"][:3]),
                        color=ft.Colors.ERROR,
                    )
                )
            )
        if self.selected_namespace:
            try:
                stats = namespace_statistics(database, self.selected_namespace)
                tables = namespace_tables(database, self.selected_namespace, limit=100)
            except Exception as error:
                body.append(
                    _card(
                        ft.Column(
                            [
                                ft.Text(
                                    "Не удалось прочитать выбранный набор",
                                    size=20,
                                    weight=ft.FontWeight.W_600,
                                ),
                                ft.Text(str(error), color=ft.Colors.ERROR),
                            ],
                            spacing=8,
                        )
                    )
                )
            else:
                if (
                    self.selected_namespace == "forum:sections"
                    and len(stats["series"]) == 1
                    and any(
                        item["label"] == "Разделов" and int(item["value"]) > 1
                        for item in stats["metrics"]
                    )
                ):
                    body.append(
                        _card(
                            ft.Row(
                                [
                                    ft.Icon(
                                        ft.Icons.REFRESH,
                                        color=ft.Colors.ON_TERTIARY_CONTAINER,
                                    ),
                                    ft.Text(
                                        "Карта сохранена старой версией: категории могли "
                                        "объединиться в одну. Запустите «Карта форума» "
                                        "ещё раз — теперь она обновляется автоматически.",
                                        color=ft.Colors.ON_SURFACE,
                                        expand=True,
                                    ),
                                ],
                                spacing=12,
                            )
                        )
                    )
                body.append(
                    ft.ResponsiveRow(
                        [
                            _metric_card(
                                item["label"],
                                item["value"],
                                _metric_icon(str(item["label"])),
                            )
                            for item in stats["metrics"]
                        ]
                    )
                )
                summary_titles = {
                    "members": ("", "Топ по положительному рейтингу"),
                    "forum:sections": ("Разделы по категориям", ""),
                    "clans:details": ("", "Топ участников по очкам"),
                    "bans:historical": ("Распределение по статусам", ""),
                }
                series_title, top_title = summary_titles.get(
                    self.selected_namespace,
                    ("Распределение", "Лидеры"),
                )
                if self.selected_namespace.startswith("punishments:"):
                    series_title = "Наказания по статусам"
                    top_title = (
                        "Наказания по блюстителям"
                        if self.selected_namespace.endswith(":entries")
                        else "Частые причины"
                    )
                elif (
                    self.selected_namespace.startswith("forum:")
                    and self.selected_namespace != "forum:sections"
                ):
                    series_title = ""
                    top_title = (
                        "Активные авторы"
                        if self.selected_namespace.endswith(":pages")
                        else "Самые просматриваемые темы"
                    )
                if stats["series"] and series_title:
                    body.append(
                        self._records_table(
                            stats["series"],
                            title=series_title,
                            columns=["label", "value"],
                            total_count=len(stats["series"]),
                        )
                    )
                if stats["top"] and top_title:
                    body.append(
                        self._records_table(
                            stats["top"],
                            title=top_title,
                            columns=["label", "value"],
                            total_count=len(stats["top"]),
                        )
                    )
                for table_spec in tables:
                    body.append(
                        self._records_table(
                            table_spec["rows"],
                            title=str(table_spec["title"]),
                            description=str(table_spec["description"]),
                            columns=list(table_spec["columns"]),
                            total_count=int(table_spec["total_count"]),
                        )
                    )
        else:
            body.append(
                _card(
                    ft.Column(
                        [
                            ft.Text(
                                "В выбранной базе пока нет записей. Выберите папку, "
                                "где лежат teslacraft.db или прежние JSONL-файлы."
                            ),
                            ft.FilledButton(
                                "Открыть папку с результатами",
                                icon=ft.Icons.FOLDER_OPEN,
                                on_click=self._choose_results_directory,
                            ),
                        ],
                        spacing=12,
                    )
                )
            )
        self.content.content = _scroll_view(
            [
                self._header(
                    "Обзор",
                    "Понятные таблицы выбранного набора, сводка и экспорт CSV.",
                ),
                ft.ResponsiveRow(
                    [
                        _metric_card("Записей в базе", summary["records"], ft.Icons.STORAGE),
                        _metric_card("Наборов данных", summary["namespaces"], ft.Icons.DATASET),
                        _metric_card("Запусков", summary["runs"], ft.Icons.HISTORY),
                        _metric_card("Снимков", summary["snapshots"], ft.Icons.CAMERA_ALT),
                    ]
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
                ft.Row(
                    [
                        ft.Icon(ft.Icons.STORAGE, size=18),
                        ft.Text(
                            f"Источник: {database.path}",
                            size=12,
                            color=ft.Colors.ON_SURFACE_VARIANT,
                            selectable=True,
                            expand=True,
                        ),
                        ft.TextButton("Изменить", on_click=self._open_settings),
                    ],
                    spacing=8,
                ),
                *body,
            ]
        )
        self.page.update()

    def _select_namespace(self, event: ft.Event[ft.Dropdown]) -> None:
        self.selected_namespace = event.control.value
        with suppress(OSError):
            save_gui_settings(self.settings_path, self._settings_payload())
        self._show_overview()

    def _records_table(
        self,
        records: list[dict[str, Any]],
        *,
        title: str,
        columns: list[str],
        total_count: int,
        description: str = "",
    ) -> ft.Card:
        all_fields = {str(key) for record in records for key in record}
        fields = [field for field in columns if field in all_fields]
        header = ft.Column(
            [
                ft.Text(title, size=20, weight=ft.FontWeight.W_600),
                *(
                    [ft.Text(description, color=ft.Colors.ON_SURFACE_VARIANT)]
                    if description
                    else []
                ),
                ft.Text(
                    f"Показано {_number(len(records))} из {_number(total_count)}",
                    size=13,
                    color=ft.Colors.ON_SURFACE_VARIANT,
                ),
            ],
            spacing=4,
        )
        if not records or not fields:
            return _card(
                ft.Column(
                    [
                        header,
                        ft.Text("В этом наборе пока нет строк для отображения."),
                    ],
                    spacing=14,
                )
            )

        numeric_fields = {
            "id",
            "node_id",
            "page",
            "positive",
            "neutral",
            "negative",
            "replies",
            "views",
            "total_pages",
            "member_count",
            "score",
            "messages",
            "unique_authors",
            "value",
            "prefix_id",
            "thread_count",
        }

        def cell(field: str, value: Any) -> ft.Control:
            if isinstance(value, str) and value.startswith(("https://", "http://")):
                return ft.IconButton(
                    icon=ft.Icons.OPEN_IN_NEW,
                    icon_color=ft.Colors.ON_SURFACE_VARIANT,
                    url=value,
                    tooltip="Открыть ссылку",
                )
            translated = VALUE_LABELS.get(str(value).casefold(), value)
            text = _display_value(translated, limit=140)
            width = 240 if field in {"title", "category", "parent", "authors", "data"} else None
            return ft.Text(
                text,
                size=13,
                color=ft.Colors.ON_SURFACE,
                max_lines=2,
                overflow=ft.TextOverflow.ELLIPSIS,
                tooltip=text if len(text) > 60 else None,
                selectable=True,
                width=width,
            )

        table = ft.DataTable(
            columns=[
                ft.DataColumn(
                    ft.Text(FIELD_LABELS.get(field, field)),
                    numeric=field in numeric_fields,
                )
                for field in fields
            ],
            bgcolor=ft.Colors.SURFACE_CONTAINER_LOWEST,
            border=ft.Border.all(1, ft.Colors.OUTLINE_VARIANT),
            border_radius=12,
            data_row_color=_data_row_colors(),
            data_text_style=ft.TextStyle(color=ft.Colors.ON_SURFACE),
            heading_row_color=ft.Colors.SURFACE_CONTAINER_HIGH,
            heading_text_style=ft.TextStyle(
                color=ft.Colors.ON_SURFACE,
                weight=ft.FontWeight.W_600,
            ),
            column_spacing=24,
            horizontal_margin=16,
            heading_row_height=48,
            data_row_min_height=48,
            data_row_max_height=64,
            show_bottom_border=True,
            rows=[
                ft.DataRow(
                    cells=[ft.DataCell(cell(field, record.get(field))) for field in fields]
                )
                for record in records
            ],
        )
        return _card(
            ft.Column(
                [
                    header,
                    ft.Container(
                        ft.Row([table], scroll=ft.ScrollMode.AUTO),
                        padding=ft.Padding.only(bottom=8),
                    ),
                ],
                spacing=14,
            )
        )

    def _export_selected(self, event: ft.Event[Any]) -> None:
        if not self.selected_namespace:
            return
        safe_name = self.selected_namespace.replace(":", "_")
        output_dir = _path_from_text(
            self.output_dir.value,
            self.default_output_dir,
            base=self.data_root,
        )
        path = output_dir / "exports" / f"{safe_name}.csv"
        try:
            count = self.database.export_csv(self.selected_namespace, path)
        except Exception as error:
            self._notify(f"Не удалось экспортировать CSV: {error}", error=True)
            return
        self._notify(f"Экспортировано {count} записей: {path.resolve()}")

    def _import_jsonl(self, event: ft.Event[Any]) -> None:
        try:
            path = _path_from_text(
                self.migrate_jsonl.value,
                self.data_root,
                base=self.data_root,
            )
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
        self.navigation.selected_index = 0
        self._show_overview()

    def _show_snapshots(self) -> None:
        try:
            database = self._database_with_saved_data()
            snapshots = database.list_snapshots()
        except Exception as error:
            self.content.content = _scroll_view(
                [
                    self._header(
                        "Снимки",
                        "Сравнение двух состояний одного набора данных.",
                    ),
                    self._storage_error_card(error),
                ]
            )
            self.page.update()
            return
        if not snapshots:
            self.content.content = _scroll_view(
                [
                    self._header(
                        "Снимки",
                        "Сравнение двух состояний одного набора данных.",
                    ),
                    _card(
                        ft.Column(
                            [
                                ft.Text(
                                    "Снимков пока нет",
                                    size=20,
                                    weight=ft.FontWeight.W_600,
                                ),
                                ft.Text(
                                    "В разделе «Сбор данных» включите «Создать "
                                    "снимок» перед запуском.",
                                    color=ft.Colors.ON_SURFACE_VARIANT,
                                ),
                            ],
                            spacing=8,
                        )
                    ),
                ]
            )
            self.page.update()
            return
        options = [
            ft.DropdownOption(
                key=str(item["id"]),
                text=(
                    f"#{item['id']} · {_namespace_label(str(item['namespace']))} · "
                    f"{item['name']} ({item['record_count']})"
                ),
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
            try:
                difference = database.compare_snapshots(int(older.value), int(newer.value))
            except Exception as error:
                self._notify(f"Не удалось сравнить снимки: {error}", error=True)
                return
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

        self.content.content = _scroll_view(
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
            ]
        )
        self.page.update()

    def _show_settings(self) -> None:
        theme = _theme_selector(self.page.theme_mode.value)

        def save(event: ft.Event[Any]) -> None:
            selected_theme = next(iter(theme.selected), "system")
            try:
                self._save_gui_settings(selected_theme)
            except Exception as error:
                self._notify(f"Не удалось сохранить настройки: {error}", error=True)
                return
            self.page.theme_mode = ft.ThemeMode(selected_theme)
            self.selected_namespace = None
            self._notify(f"Настройки сохранены. Активная база: {self.database_path.value}")

        self.content.content = _scroll_view(
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
                            ft.Row(
                                [
                                    ft.OutlinedButton(
                                        "Выбрать папку результатов",
                                        icon=ft.Icons.FOLDER_OPEN,
                                        on_click=self._choose_results_directory,
                                    ),
                                    ft.OutlinedButton(
                                        "Выбрать готовую SQLite-базу",
                                        icon=ft.Icons.STORAGE,
                                        on_click=self._choose_database_file,
                                    ),
                                ],
                                wrap=True,
                            ),
                            ft.Text(
                                "Список в разделе «Обзор» читается из SQLite. "
                                "Если в выбранной папке есть прежние JSONL, программа "
                                "перенесёт их в базу автоматически, не удаляя исходники.",
                                color=ft.Colors.ON_SURFACE_VARIANT,
                            ),
                            ft.Divider(),
                            ft.Text(
                                "Ручной импорт JSONL",
                                size=18,
                                weight=ft.FontWeight.W_600,
                            ),
                            ft.Text(
                                "Используйте этот блок только для стороннего или "
                                "переименованного JSONL, который приложение не распознало.",
                                color=ft.Colors.ON_SURFACE_VARIANT,
                            ),
                            ft.ResponsiveRow(
                                [
                                    ft.Container(
                                        self.migrate_jsonl,
                                        col={"sm": 12, "md": 9},
                                    ),
                                    ft.Container(
                                        ft.OutlinedButton(
                                            "Выбрать JSONL",
                                            icon=ft.Icons.FOLDER_OPEN,
                                            on_click=self._choose_jsonl_file,
                                        ),
                                        col={"sm": 12, "md": 3},
                                    ),
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
                            ft.Text(
                                "Кнопка ниже сохраняет пути, параметры сети и тему.",
                                color=ft.Colors.ON_SURFACE_VARIANT,
                            ),
                            ft.FilledButton(
                                "Сохранить все настройки",
                                icon=ft.Icons.SAVE,
                                on_click=save,
                            ),
                        ],
                        spacing=16,
                    )
                ),
            ]
        )
        self.page.update()


async def app_main(page: ft.Page) -> None:
    # Render the animated loader before settings discovery or database access.
    # The client keeps animating it even while Python prepares the application.
    page.title = APP_TITLE
    page.theme = _app_theme()
    page.dark_theme = _app_theme(dark=True)
    page.bgcolor = ft.Colors.SURFACE
    page.padding = 0
    page.window.icon = WINDOW_ICON
    page.window.width = WINDOW_WIDTH
    page.window.height = WINDOW_HEIGHT
    page.window.min_width = 980
    page.window.min_height = 680
    page.add(_startup_loader())
    await page.window.center()
    await asyncio.sleep(0.05)
    try:
        import pyi_splash
    except ImportError:
        pyi_splash = None
    if pyi_splash is not None and pyi_splash.is_alive():
        pyi_splash.close()

    application = TeslaParserApp(page)
    page.clean()
    application.mount()
    await asyncio.sleep(0)
    application._show_overview()


def run_gui() -> None:
    ft.run(app_main, name=APP_TITLE, assets_dir="assets")
