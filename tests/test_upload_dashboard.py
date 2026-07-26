from dataclasses import dataclass
from datetime import datetime, timezone

from app.upload_dashboard import summarize_upload_tasks


@dataclass
class Task:
    status: str
    total_size: int
    uploaded_bytes: int
    created_at: str


def test_summarize_upload_tasks_counts_state_and_today_bytes():
    tasks = [
        Task("running", 100, 40, "2026-07-26T01:00:00+00:00"),
        Task("completed", 200, 200, "2026-07-26T02:00:00+00:00"),
        Task("failed", 300, 75, "2026-07-25T02:00:00+00:00"),
        Task("canceled", 400, 20, "invalid"),
    ]

    summary = summarize_upload_tasks(tasks, now=datetime(2026, 7, 26, 12, tzinfo=timezone.utc))

    assert summary.active_count == 1
    assert summary.today_uploaded_bytes == 240
    assert summary.failed_count == 1
    assert summary.completed_count == 1
    assert summary.total_count == 4
