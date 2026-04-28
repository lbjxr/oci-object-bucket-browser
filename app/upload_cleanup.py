from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from app.config import Settings, get_settings
from app.temp_uploads import TempUploadSession, TempUploadSessionStore
from app.upload_sessions import UploadSessionStore
from app.upload_tasks import ACTIVE_STATUSES, ServerUploadTaskManager, ServerUploadTaskStore, TERMINAL_STATUSES


logger = logging.getLogger(__name__)


@dataclass
class UploadCleanupResult:
    deleted_task_files: list[str]
    deleted_temp_upload_metadata: list[str]
    deleted_temp_files: list[str]
    deleted_upload_session_files: list[str]
    skipped_active_tasks: list[str]
    skipped_active_temp_uploads: list[str]
    skipped_active_upload_sessions: list[str]

    def to_dict(self) -> dict[str, object]:
        return {
            "deleted_task_files": self.deleted_task_files,
            "deleted_temp_upload_metadata": self.deleted_temp_upload_metadata,
            "deleted_temp_files": self.deleted_temp_files,
            "deleted_upload_session_files": self.deleted_upload_session_files,
            "skipped_active_tasks": self.skipped_active_tasks,
            "skipped_active_temp_uploads": self.skipped_active_temp_uploads,
            "skipped_active_upload_sessions": self.skipped_active_upload_sessions,
        }


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _safe_unlink(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _is_task_active(manager: ServerUploadTaskManager | None, task_id: str) -> bool:
    if manager is None:
        return False
    threads_lock = getattr(manager, '_threads_lock', None)
    threads = getattr(manager, '_threads', None)
    if threads_lock is None or threads is None:
        return False
    with threads_lock:  # noqa: SLF001 - minimal cleanup needs current in-memory active threads
        thread = threads.get(task_id)
        return bool(thread and thread.is_alive())


def _read_temp_upload(path: Path) -> TempUploadSession | None:
    try:
        return TempUploadSession.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except Exception:
        return None


def _iter_json_files(base_dir: Path, pattern: str = "*.json") -> Iterable[Path]:
    if not base_dir.exists():
        return []
    return sorted(p for p in base_dir.glob(pattern) if p.is_file())


class UploadCleanupService:
    def __init__(self, settings: Settings | None = None, manager: ServerUploadTaskManager | None = None) -> None:
        self.settings = settings or get_settings()
        self.manager = manager
        self.task_store = ServerUploadTaskStore(self.settings.upload_task_dir)
        self.temp_store = TempUploadSessionStore(self.settings.upload_temp_dir)
        self.upload_session_store = UploadSessionStore(self.settings.upload_session_dir)
        self.task_dir = self.task_store.base_dir
        self.temp_dir = Path(os.path.expanduser(self.settings.upload_temp_dir)).resolve()
        self.upload_session_dir = self.upload_session_store.base_dir
        self._run_lock = threading.Lock()

    def run_once(self) -> UploadCleanupResult:
        with self._run_lock:
            result = UploadCleanupResult(
                deleted_task_files=[],
                deleted_temp_upload_metadata=[],
                deleted_temp_files=[],
                deleted_upload_session_files=[],
                skipped_active_tasks=[],
                skipped_active_temp_uploads=[],
                skipped_active_upload_sessions=[],
            )
            if not self.settings.upload_cleanup_enabled:
                return result

            now = _utc_now()
            completed_cutoff = now - timedelta(hours=self.settings.upload_cleanup_completed_retention_hours)
            failed_cutoff = now - timedelta(hours=self.settings.upload_cleanup_failed_retention_hours)
            stale_staging_cutoff = now - timedelta(hours=self.settings.upload_cleanup_stale_staging_retention_hours)

            active_task_ids: set[str] = set()
            active_upload_session_ids: set[str] = set()
            active_temp_paths: set[str] = set()

            all_tasks = self.task_store.list_all()
            for task in all_tasks:
                if task.status in ACTIVE_STATUSES or _is_task_active(self.manager, task.task_id):
                    active_task_ids.add(task.task_id)
                    if task.upload_session_id:
                        active_upload_session_ids.add(task.upload_session_id)
                    if task.temp_path:
                        active_temp_paths.add(str(Path(task.temp_path).resolve()))

            task_id_by_temp_path: dict[str, str] = {}
            task_by_upload_session_id = {task.upload_session_id: task for task in all_tasks if task.upload_session_id}
            for path in _iter_json_files(self.task_dir):
                try:
                    task = self.task_store._read_path_unlocked(path)  # noqa: SLF001 - reuse existing parser
                except Exception:
                    continue
                if not task:
                    continue
                if task.temp_path:
                    task_id_by_temp_path[str(Path(task.temp_path).resolve())] = task.task_id
                if task.upload_session_id:
                    task_by_upload_session_id[task.upload_session_id] = task

                if task.task_id in active_task_ids or task.status in ACTIVE_STATUSES:
                    result.skipped_active_tasks.append(task.task_id)
                    continue
                if task.status not in TERMINAL_STATUSES:
                    continue

                updated_at = _parse_iso(task.updated_at) or _parse_iso(task.created_at) or now
                cutoff = completed_cutoff if task.status == "completed" else failed_cutoff
                if updated_at > cutoff:
                    continue

                if task.temp_path:
                    temp_path = Path(task.temp_path).resolve()
                    if str(temp_path) not in active_temp_paths and temp_path.exists() and _safe_unlink(temp_path):
                        result.deleted_temp_files.append(str(temp_path))
                if _safe_unlink(path):
                    result.deleted_task_files.append(task.task_id)
                if task.upload_session_id and task.upload_session_id not in active_upload_session_ids:
                    session_path = self.upload_session_store._path_for(task.upload_session_id)  # noqa: SLF001
                    if session_path.exists() and _safe_unlink(session_path):
                        result.deleted_upload_session_files.append(task.upload_session_id)

            for path in _iter_json_files(self.temp_dir, "*.upload.json"):
                session = _read_temp_upload(path)
                if not session:
                    continue
                staged_path = str(Path(session.staged_path).resolve())
                linked_task_id = task_id_by_temp_path.get(staged_path)
                if linked_task_id and linked_task_id in active_task_ids:
                    active_temp_paths.add(staged_path)
                    result.skipped_active_temp_uploads.append(session.temp_upload_id)
                    continue

                updated_at = _parse_iso(session.updated_at) or _parse_iso(session.created_at) or now
                if session.committed:
                    if linked_task_id and linked_task_id in active_task_ids:
                        result.skipped_active_temp_uploads.append(session.temp_upload_id)
                        continue
                    if updated_at <= completed_cutoff:
                        if _safe_unlink(path):
                            result.deleted_temp_upload_metadata.append(session.temp_upload_id)
                    continue

                if updated_at <= stale_staging_cutoff:
                    if staged_path not in active_temp_paths:
                        staged_file = Path(staged_path)
                        if staged_file.exists() and _safe_unlink(staged_file):
                            result.deleted_temp_files.append(str(staged_file))
                    if _safe_unlink(path):
                        result.deleted_temp_upload_metadata.append(session.temp_upload_id)

            for path in _iter_json_files(self.upload_session_dir):
                if path.name.endswith(".tmp"):
                    continue
                upload_id = path.stem
                if upload_id in active_upload_session_ids:
                    result.skipped_active_upload_sessions.append(upload_id)
                    continue
                if upload_id in task_by_upload_session_id:
                    task = task_by_upload_session_id[upload_id]
                    if task.status not in TERMINAL_STATUSES:
                        result.skipped_active_upload_sessions.append(upload_id)
                        continue
                    updated_at = _parse_iso(task.updated_at) or _parse_iso(task.created_at) or now
                    cutoff = completed_cutoff if task.status == "completed" else failed_cutoff
                    if updated_at > cutoff:
                        continue
                else:
                    try:
                        session = self.upload_session_store.get(upload_id)
                    except Exception:
                        session = None
                    session_updated_at = _parse_iso(session.updated_at) if session else None
                    if session_updated_at and session_updated_at > stale_staging_cutoff:
                        continue
                if _safe_unlink(path):
                    result.deleted_upload_session_files.append(upload_id)

            return result


class UploadCleanupScheduler:
    def __init__(self, service: UploadCleanupService) -> None:
        self.service = service
        self.settings = service.settings
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        if not self.settings.upload_cleanup_enabled or not self.settings.upload_cleanup_scheduler_enabled:
            return False
        if self._thread and self._thread.is_alive():
            return False
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="upload-cleanup-scheduler",
        )
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run_loop(self) -> None:
        interval = max(1, self.settings.upload_cleanup_interval_seconds)
        while not self._stop_event.wait(interval):
            try:
                self.service.run_once()
            except Exception:
                logger.exception("scheduled upload cleanup failed")


def run_upload_cleanup(*, settings: Settings | None = None, manager: ServerUploadTaskManager | None = None) -> UploadCleanupResult:
    return UploadCleanupService(settings=settings, manager=manager).run_once()


__all__ = ["UploadCleanupResult", "UploadCleanupService", "UploadCleanupScheduler", "run_upload_cleanup"]
