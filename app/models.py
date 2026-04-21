from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ObjectEntry:
    name: str
    size: int | None
    etag: str | None
    time_created: str | None
    content_type: str | None = None


@dataclass
class PreviewData:
    kind: str
    content_type: str
    text: str | None = None
    bytes_data: bytes | None = None
    download_only: bool = False
