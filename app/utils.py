from __future__ import annotations

import base64
import mimetypes
from pathlib import PurePosixPath

TEXT_TYPES = {
    "application/json",
    "application/xml",
    "application/javascript",
}


def guess_content_type(name: str, explicit: str | None = None) -> str:
    if explicit:
        return explicit
    guessed, _ = mimetypes.guess_type(name)
    return guessed or "application/octet-stream"


def is_text_type(content_type: str) -> bool:
    return content_type.startswith("text/") or content_type in TEXT_TYPES


def is_image_type(content_type: str) -> bool:
    return content_type.startswith("image/")


def is_pdf_type(content_type: str) -> bool:
    return content_type == "application/pdf"


def to_data_url(content_type: str, payload: bytes) -> str:
    encoded = base64.b64encode(payload).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def object_name_from_upload(filename: str) -> str:
    return PurePosixPath(filename).as_posix().lstrip("/")
