from __future__ import annotations

from dataclasses import dataclass
from math import ceil
from pathlib import PurePosixPath
from typing import Protocol, TypeVar


class FolderLike(Protocol):
    name: str


class FileLike(Protocol):
    name: str
    size: int | None
    content_type: str | None


FolderT = TypeVar("FolderT", bound=FolderLike)
FileT = TypeVar("FileT", bound=FileLike)


FILE_TYPE_FILTERS = {
    "all",
    "folder",
    "image",
    "pdf",
    "text",
    "archive",
    "video",
    "audio",
    "other",
}


@dataclass(frozen=True)
class FileBrowserQuery:
    query: str = ""
    file_type: str = "all"
    size_min_mb: float | None = None
    size_max_mb: float | None = None
    page: int = 1
    page_size: int = 25

    @property
    def normalized_query(self) -> str:
        return self.query.strip().casefold()

    @property
    def normalized_file_type(self) -> str:
        return self.file_type if self.file_type in FILE_TYPE_FILTERS else "all"

    @property
    def size_min_bytes(self) -> int | None:
        if self.size_min_mb is None:
            return None
        return int(self.size_min_mb * 1024 * 1024)

    @property
    def size_max_bytes(self) -> int | None:
        if self.size_max_mb is None:
            return None
        return int(self.size_max_mb * 1024 * 1024)


@dataclass(frozen=True)
class FileBrowserPage:
    folders: list[FolderT]
    files: list[FileT]
    total_items: int
    page: int
    page_size: int
    total_pages: int
    start_item: int
    end_item: int

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages


def classify_file_type(content_type: str | None, object_name: str = "") -> str:
    normalized = (content_type or "").casefold()
    suffix = PurePosixPath(object_name).suffix.casefold()
    if normalized.startswith("image/"):
        return "image"
    if normalized == "application/pdf" or suffix == ".pdf":
        return "pdf"
    if normalized.startswith("text/") or suffix in {".md", ".json", ".xml", ".yaml", ".yml", ".csv", ".log"}:
        return "text"
    if "zip" in normalized or "compressed" in normalized or suffix in {".zip", ".7z", ".rar", ".tar", ".gz"}:
        return "archive"
    if normalized.startswith("video/"):
        return "video"
    if normalized.startswith("audio/"):
        return "audio"
    return "other"


def filter_and_paginate(
    folders: list[FolderT],
    files: list[FileT],
    filters: FileBrowserQuery,
) -> FileBrowserPage:
    query = filters.normalized_query
    file_type = filters.normalized_file_type
    minimum = filters.size_min_bytes
    maximum = filters.size_max_bytes
    has_size_filter = minimum is not None or maximum is not None

    filtered_folders = [
        folder
        for folder in folders
        if file_type in {"all", "folder"}
        and not has_size_filter
        and (not query or query in folder.name.casefold())
    ]

    filtered_files: list[FileT] = []
    if file_type != "folder":
        for item in files:
            basename = PurePosixPath(item.name).name.casefold()
            if query and query not in basename and query not in item.name.casefold():
                continue
            if file_type != "all" and classify_file_type(item.content_type, item.name) != file_type:
                continue
            size = item.size or 0
            if minimum is not None and size < minimum:
                continue
            if maximum is not None and size > maximum:
                continue
            filtered_files.append(item)

    combined: list[tuple[str, FolderT | FileT]] = [
        *(('folder', folder) for folder in filtered_folders),
        *(('file', item) for item in filtered_files),
    ]
    total_items = len(combined)
    page_size = min(max(filters.page_size, 10), 100)
    total_pages = max(1, ceil(total_items / page_size))
    page = min(max(filters.page, 1), total_pages)
    start = (page - 1) * page_size
    visible = combined[start:start + page_size]

    visible_folders = [item for kind, item in visible if kind == "folder"]
    visible_files = [item for kind, item in visible if kind == "file"]
    start_item = start + 1 if total_items else 0
    end_item = min(start + page_size, total_items)
    return FileBrowserPage(
        folders=visible_folders,
        files=visible_files,
        total_items=total_items,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
        start_item=start_item,
        end_item=end_item,
    )
