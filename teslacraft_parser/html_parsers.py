from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

BASE_URL = "https://teslacraft.org"


def _text(node: Tag | None) -> str:
    return node.get_text(" ", strip=True) if node is not None else ""


def _attr(node: Tag | None, name: str) -> str:
    if node is None:
        return ""
    value = node.get(name)
    return str(value).strip() if value is not None else ""


def _integer(value: str) -> int | None:
    match = re.search(r"[+-]?\s*\d[\d\s]*", value)
    return int(match.group(0).replace(" ", "")) if match else None


def _punishment_entry(
    punishment_type: str,
    cells: list[str],
    detail_href: str,
) -> dict[str, Any] | None:
    match = re.search(r"/(\d+)/?$", detail_href)
    if match is None or not cells:
        return None
    punishment_id = int(match.group(1))
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


def parse_punishment_index_html(
    html: str,
    punishment_type: str,
    page_number: int,
    url: str,
) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    entries: list[dict[str, Any]] = []
    for row in soup.select("#content table tbody tr"):
        cells = [_text(cell) for cell in row.select("td")]
        detail = row.select_one("a[href*='/banlist/']")
        entry = _punishment_entry(
            punishment_type,
            cells,
            _attr(detail, "href"),
        )
        if entry is not None:
            entries.append(entry)
    return {
        "key": f"{punishment_type}:{page_number}",
        "type": punishment_type,
        "page": page_number,
        "url": url,
        "entries": entries,
    }


def parse_punishment_detail_html(
    html: str,
    key: str,
    url: str,
) -> dict[str, Any]:
    punishment_type, raw_id = key.split(":", 1)
    soup = BeautifulSoup(html, "html.parser")
    banlist = soup.select_one(".banlist")
    if banlist is None:
        return {
            "key": key,
            "type": punishment_type,
            "id": int(raw_id),
            "url": url,
            "status": "missing",
            "data": {},
        }
    labels = soup.select(".banlist .left-label, .banlist .center-label")
    values = soup.select(".banlist .right-data, .banlist .center-data")
    data = {
        _text(label): _text(value)
        for label, value in zip(labels, values, strict=False)
        if _text(label)
    }
    badge_text = " ".join(
        _text(badge)
        for badge in soup.select(".titleBar .label, .rekt_titleContainer .label")
        if _text(badge)
    )
    normalized = badge_text.casefold()
    status = (
        "active" if "актив" in normalized else "lifted" if "снят" in normalized else "recorded"
    )
    return {
        "key": key,
        "type": punishment_type,
        "id": int(raw_id),
        "url": url,
        "status": status,
        "status_text": badge_text or None,
        "data": data,
    }


def parse_forum_sections_html(html: str, url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    sections: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in soup.select(".nodeList.sectionMain"):
        category = _text(group.select_one("li.node.category.level_1 .nodeTitle a"))
        for node in group.select("li.node.forum.level_2, li.node.link.level_2"):
            direct_info = node.find(class_="nodeInfo", recursive=False)
            title_link = (
                direct_info.select_one(".nodeTitle a")
                if isinstance(direct_info, Tag)
                else node.select_one(".nodeInfo .nodeTitle a")
            )
            href = _attr(title_link, "href")
            title = _text(title_link)
            absolute = urljoin(BASE_URL + "/", href)
            class_names = " ".join(node.get("class", []))
            node_match = re.search(r"node_(\d+)", class_names)
            if href and absolute not in seen:
                seen.add(absolute)
                sections.append(
                    {
                        "category": category,
                        "title": title,
                        "url": absolute,
                        "node_id": int(node_match.group(1)) if node_match else None,
                        "kind": "link" if "link" in node.get("class", []) else "forum",
                        "level": 2,
                    }
                )
            for subforum in node.select(
                ".subForumList a, .subForums a, .nodeSubForum a, .nodeList .nodeTitle a"
            ):
                sub_href = _attr(subforum, "href")
                sub_title = _text(subforum)
                sub_absolute = urljoin(BASE_URL + "/", sub_href)
                if not sub_href or not sub_title or sub_absolute in seen:
                    continue
                seen.add(sub_absolute)
                sub_match = re.search(r"\.(\d+)/?$", sub_absolute)
                sections.append(
                    {
                        "category": category,
                        "parent": title,
                        "title": sub_title,
                        "url": sub_absolute,
                        "node_id": int(sub_match.group(1)) if sub_match else None,
                        "kind": "forum",
                        "level": 3,
                    }
                )
    return {"url": url, "sections": sections}


def parse_forum_index_html(
    html: str,
    page_number: int,
    url: str,
) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    threads: list[dict[str, Any]] = []
    for discussion in soup.select(".discussionListItem"):
        title_links = discussion.select("h3.title a")
        title_link = title_links[-1] if title_links else None
        href = _attr(title_link, "href")
        if not href:
            continue
        page_links = discussion.select(".secondRow .itemPageNav a")
        last_page = page_links[-1] if page_links else None
        total_pages = _integer(_text(last_page)) or 1
        page_match = re.search(r"page-(\d+)", _attr(last_page, "href"))
        if total_pages == 1 and page_match:
            total_pages = int(page_match.group(1))
        prefixes = []
        for prefix in discussion.select(".prefix"):
            prefix_link = prefix if prefix.name == "a" else prefix.find_parent("a")
            prefix_href = _attr(prefix_link, "href") or _attr(prefix, "href")
            prefix_match = re.search(r"[?&]prefix_id=(\d+)", prefix_href)
            name = _text(prefix)
            if name:
                prefixes.append(
                    {
                        "id": prefix_match.group(1) if prefix_match else None,
                        "name": name,
                    }
                )
        created_node = discussion.select_one(
            ".posterDate .DateTime, .posterDate time, .posterDate abbr"
        )
        last_post_node = discussion.select_one(
            ".lastPostInfo .DateTime, .lastPostInfo time, .lastPostInfo abbr"
        )
        classes = {str(value).casefold() for value in discussion.get("class", [])}
        threads.append(
            {
                "url": urljoin(BASE_URL + "/", href),
                "title": _text(title_link),
                "total_pages": total_pages,
                "author": _text(discussion.select_one(".posterDate .username, .username"))
                or None,
                "created_at": _attr(created_node, "title") or _text(created_node) or None,
                "replies": _integer(
                    _text(discussion.select_one(".stats .major dd, .stats dl:first-child dd"))
                ),
                "views": _integer(
                    _text(discussion.select_one(".stats .minor dd, .stats dl:last-child dd"))
                ),
                "last_poster": _text(discussion.select_one(".lastPostInfo .username")) or None,
                "last_post_at": _attr(last_post_node, "title") or _text(last_post_node) or None,
                "is_sticky": "sticky" in classes,
                "is_locked": "locked" in classes,
                "prefixes": prefixes,
            }
        )
    available_prefixes = []
    for option in soup.select("select[name='prefix_id'] option"):
        prefix_id = _attr(option, "value")
        name = _text(option)
        if prefix_id and prefix_id != "0" and name:
            available_prefixes.append({"id": prefix_id, "name": name})
    return {
        "page": page_number,
        "url": url,
        "title": _text(soup.select_one("h1")),
        "available_prefixes": available_prefixes,
        "threads": threads,
    }
