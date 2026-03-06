from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


REST_ID_PATTERN = re.compile(r"^[A-Za-z]{2}\d{5}$")
SOURCE_PDF_PATTERN = re.compile(r"^- Source PDF:\s*`([^`]+)`\s*$")
TABLE_ROW_PATTERN = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(\d+)\s*\|$")


@dataclass(frozen=True)
class CatalogEntry:
    code: str
    name: str
    major: str
    minor: str
    page: int
    kind: str  # rest | realtime | common


def _kind_from_code(code: str) -> str:
    if REST_ID_PATTERN.fullmatch(code):
        return "rest"
    if code == "공통":
        return "common"
    return "realtime"


@lru_cache(maxsize=4)
def load_catalog(catalog_path: str) -> tuple[CatalogEntry, ...]:
    path = Path(catalog_path)
    text = path.read_text(encoding="utf-8-sig")
    entries: list[CatalogEntry] = []

    for raw in text.splitlines():
        match = TABLE_ROW_PATTERN.match(raw)
        if not match:
            continue
        code, name, major, minor, page = match.groups()
        entries.append(
            CatalogEntry(
                code=code.strip(),
                name=name.strip(),
                major=major.strip(),
                minor=minor.strip(),
                page=int(page),
                kind=_kind_from_code(code.strip()),
            )
        )
    return tuple(entries)


def search_catalog(
    *,
    catalog_path: str,
    query: str = "",
    kind: str = "all",
    major: str = "",
    minor: str = "",
    limit: int = 20,
) -> list[CatalogEntry]:
    normalized_query = query.strip().lower()
    normalized_kind = kind.strip().lower() or "all"
    normalized_major = major.strip().lower()
    normalized_minor = minor.strip().lower()
    max_items = min(max(limit, 1), 200)

    result: list[CatalogEntry] = []
    for entry in load_catalog(catalog_path):
        if normalized_kind != "all" and entry.kind != normalized_kind:
            continue
        if normalized_major and normalized_major not in entry.major.lower():
            continue
        if normalized_minor and normalized_minor not in entry.minor.lower():
            continue
        if normalized_query:
            haystack = " ".join(
                [entry.code, entry.name, entry.major, entry.minor, str(entry.page)]
            ).lower()
            if normalized_query not in haystack:
                continue
        result.append(entry)
        if len(result) >= max_items:
            break
    return result


def find_by_code(catalog_path: str, code: str) -> CatalogEntry | None:
    target = code.strip().lower()
    if not target:
        return None
    for entry in load_catalog(catalog_path):
        if entry.code.lower() == target:
            return entry
    return None


@lru_cache(maxsize=4)
def get_catalog_source_pdf(catalog_path: str) -> str | None:
    text = Path(catalog_path).read_text(encoding="utf-8-sig")
    for line in text.splitlines():
        match = SOURCE_PDF_PATTERN.match(line.strip())
        if match:
            return match.group(1).strip()
    return None


def page_range_for_code(catalog_path: str, code: str) -> tuple[int, int] | None:
    current = find_by_code(catalog_path, code)
    if current is None:
        return None
    if current.kind != "rest":
        return (current.page, current.page)

    rest_entries = sorted(
        (entry for entry in load_catalog(catalog_path) if entry.kind == "rest"),
        key=lambda entry: entry.page,
    )
    next_page = None
    for entry in rest_entries:
        if entry.page > current.page:
            next_page = entry.page
            break

    if next_page is None:
        return (current.page, current.page + 2)
    return (current.page, max(current.page, next_page - 1))
