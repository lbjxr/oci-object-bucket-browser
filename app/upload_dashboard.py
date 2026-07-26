from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


class UploadTaskLike(Protocol):
    status: str
    total_size: int
    uploaded_bytes: int
    created_at: str


ACTIVE_UPLOAD_STATUSES = {"queued", "running", "finalizing"}


@dataclass(frozen=True)
class UploadTaskSummary:
    active_count: int
    today_uploaded_bytes: int
    failed_count: int
    completed_count: int
    total_count: int

    def to_dict(self) -> dict[str, int]:
        return {
            "active_count": self.active_count,
            "today_uploaded_bytes": self.today_uploaded_bytes,
            "failed_count": self.failed_count,
            "completed_count": self.completed_count,
            "total_count": self.total_count,
        }


def _parse_timestamp(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def summarize_upload_tasks(tasks: list[UploadTaskLike], now: datetime | None = None) -> UploadTaskSummary:
    local_now = now or datetime.now().astimezone()
    today = local_now.date()
    active_count = 0
    failed_count = 0
    completed_count = 0
    today_uploaded_bytes = 0

    for task in tasks:
        if task.status in ACTIVE_UPLOAD_STATUSES:
            active_count += 1
        elif task.status == "failed":
            failed_count += 1
        elif task.status == "completed":
            completed_count += 1

        created_at = _parse_timestamp(task.created_at)
        if created_at is not None and created_at.astimezone(local_now.tzinfo).date() == today:
            today_uploaded_bytes += max(0, min(int(task.uploaded_bytes), int(task.total_size)))

    return UploadTaskSummary(
        active_count=active_count,
        today_uploaded_bytes=today_uploaded_bytes,
        failed_count=failed_count,
        completed_count=completed_count,
        total_count=len(tasks),
    )
