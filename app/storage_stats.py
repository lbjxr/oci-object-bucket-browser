from __future__ import annotations

from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from app.file_browser import classify_file_type


STAT_TYPES = OrderedDict(
    (
        ("image", "图片"),
        ("document", "文档"),
        ("archive", "压缩包"),
        ("other", "其他"),
    )
)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def stat_type_for(content_type: str | None, name: str) -> str:
    file_type = classify_file_type(content_type, name)
    if file_type == "image":
        return "image"
    if file_type in {"pdf", "text"}:
        return "document"
    if file_type == "archive":
        return "archive"
    return "other"


def summarize_objects(objects: list[object], *, now: datetime | None = None, prefix: str = "") -> dict:
    effective_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    recent_cutoff = effective_now - timedelta(days=7)
    distribution = {
        key: {"type": key, "label": label, "count": 0, "bytes": 0, "percent": 0.0}
        for key, label in STAT_TYPES.items()
    }
    object_count = 0
    total_bytes = 0
    recent_bytes = 0
    recent_count = 0

    for obj in objects:
        name = str(getattr(obj, "name", "") or "")
        if not name or name.endswith("/"):
            continue
        size = max(0, int(getattr(obj, "size", 0) or 0))
        kind = stat_type_for(getattr(obj, "content_type", None), name)
        object_count += 1
        total_bytes += size
        distribution[kind]["count"] += 1
        distribution[kind]["bytes"] += size
        created_at = _parse_time(getattr(obj, "time_created", None))
        if created_at is not None and created_at >= recent_cutoff:
            recent_bytes += size
            recent_count += 1

    if total_bytes:
        for item in distribution.values():
            item["percent"] = round(item["bytes"] / total_bytes * 100, 2)

    return {
        "prefix": prefix,
        "object_count": object_count,
        "total_bytes": total_bytes,
        "recent_7d_bytes": recent_bytes,
        "recent_7d_object_count": recent_count,
        "type_distribution": list(distribution.values()),
    }
