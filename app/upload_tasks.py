from __future__ import annotations

import json
import os
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app.config import Settings, get_settings
from app.oci_client import OCIStorageError, OCIStorageService, classify_upload_exception
from app.upload_sessions import UploadedPart, UploadSessionStore
from app.utils import object_name_from_upload


TERMINAL_STATUSES = {"completed", "failed", "canceled"}
ACTIVE_STATUSES = {"queued", "running", "finalizing"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ServerUploadTask:
    task_id: str
    object_name: str
    filename: str
    content_type: str
    total_size: int
    strategy: str
    status: str
    phase: str
    created_at: str
    updated_at: str
    temp_path: str
    upload_session_id: str | None = None
    multipart_upload_id: str | None = None
    uploaded_bytes: int = 0
    uploaded_parts: list[int] | None = None
    total_parts: int | None = None
    parallelism: int = 1
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    def to_api_dict(self) -> dict:
        payload = self.to_dict()
        payload.update(describe_task_state(self))
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> "ServerUploadTask":
        return cls(
            task_id=payload["task_id"],
            object_name=payload["object_name"],
            filename=payload["filename"],
            content_type=payload["content_type"],
            total_size=int(payload["total_size"]),
            strategy=payload["strategy"],
            status=payload["status"],
            phase=payload["phase"],
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
            temp_path=payload["temp_path"],
            upload_session_id=payload.get("upload_session_id"),
            multipart_upload_id=payload.get("multipart_upload_id"),
            uploaded_bytes=int(payload.get("uploaded_bytes", 0)),
            uploaded_parts=[int(x) for x in (payload.get("uploaded_parts") or [])],
            total_parts=int(payload["total_parts"]) if payload.get("total_parts") is not None else None,
            parallelism=max(1, int(payload.get("parallelism", 1))),
            error=payload.get("error"),
        )


def _phase_label(phase: str) -> str:
    if not phase:
        return "等待中"
    if phase == "waiting":
        return "等待执行"
    if phase == "uploading_to_oci":
        return "上传到 OCI"
    if phase == "finalizing":
        return "正在完成提交"
    if phase == "done":
        return "已完成"
    if phase == "error":
        return "任务异常"
    if phase == "recovery_missing_temp_file":
        return "恢复失败：暂存文件已丢失"
    if phase.startswith("retrying_part:"):
        progress = phase.split(":", 1)[1] if ":" in phase else ""
        return f"分片重试中 {progress}".strip()
    if phase.startswith("retrying_single_put:"):
        progress = phase.split(":", 1)[1] if ":" in phase else ""
        return f"重试上传中 {progress}".strip()
    if phase.startswith("uploading_parts:"):
        progress = phase.split(":", 1)[1] if ":" in phase else ""
        return f"分片上传中 {progress}".strip()
    if phase.startswith("recovery_requeued_from:"):
        previous_status = phase.split(":", 1)[1] if ":" in phase else "unknown"
        status_map = {
            "queued": "等待中",
            "running": "上传中",
            "finalizing": "完成提交中",
        }
        return f"服务重启后已恢复，原状态：{status_map.get(previous_status, previous_status)}"
    return phase.replace("_", " ")


def _parse_retry_state(task: ServerUploadTask) -> dict:
    phase = task.phase or ""
    retrying = False
    retry_kind = None
    retry_part_num = None
    retry_attempt = 0
    retry_max_attempts = 0

    if phase.startswith("retrying_single_put:"):
        retrying = True
        retry_kind = "single_put"
        progress = phase.split(":", 1)[1] if ":" in phase else ""
        try:
            retry_attempt_text, retry_max_text = progress.split("/", 1)
            retry_attempt = int(retry_attempt_text)
            retry_max_attempts = int(retry_max_text)
        except Exception:
            pass
    elif phase.startswith("retrying_part:"):
        retrying = True
        retry_kind = "part"
        progress = phase.split(":", 1)[1] if ":" in phase else ""
        if "（第" in progress and "次重试）" in progress:
            part_text, attempt_text = progress.split("（第", 1)
            try:
                retry_part_num = int(part_text.strip())
            except Exception:
                retry_part_num = None
            attempt_text = attempt_text.split("次重试）", 1)[0]
            try:
                retry_attempt = int(attempt_text)
            except Exception:
                retry_attempt = 0
            retry_max_attempts = 3

    retry_count = retry_attempt if retrying else 0
    retry_label = None
    if retrying and retry_kind == "single_put":
        retry_label = f"后台重试中（第 {retry_attempt} 次，共 {retry_max_attempts} 次）" if retry_max_attempts else f"后台重试中（第 {retry_attempt} 次）"
    elif retrying and retry_kind == "part":
        retry_label = f"后台重试中（分片 {retry_part_num}，第 {retry_attempt} 次）" if retry_part_num is not None else f"后台重试中（第 {retry_attempt} 次）"

    last_error = task.error or None
    return {
        "is_retrying": retrying,
        "retry_count": retry_count,
        "retry_attempt": retry_attempt,
        "retry_max_attempts": retry_max_attempts,
        "retry_kind": retry_kind,
        "retry_part_num": retry_part_num,
        "retry_label": retry_label,
        "last_error": last_error,
    }


def describe_task_state(task: ServerUploadTask) -> dict:
    phase = task.phase or ""
    recovered = phase.startswith("recovery_")
    recovery_attempted = recovered or bool(task.error and "恢复" in task.error)
    recovery_source_status = None
    if phase.startswith("recovery_requeued_from:"):
        recovery_source_status = phase.split(":", 1)[1] or None
    recovery_problem = "missing_temp_file" if phase == "recovery_missing_temp_file" else None
    retry_state = _parse_retry_state(task)
    return {
        "current_phase": phase,
        "phase_label": _phase_label(phase),
        "recovered": recovered,
        "recovery_attempted": recovery_attempted,
        "recovery_source_status": recovery_source_status,
        "recovery_problem": recovery_problem,
        "status_label": {
            "queued": "排队中",
            "running": "执行中",
            "finalizing": "收尾中",
            "completed": "已完成",
            "failed": "失败",
            "canceled": "已取消",
        }.get(task.status, task.status),
        **retry_state,
    }


class ServerUploadTaskStore:
    def __init__(self, base_dir: str) -> None:
        self.base_dir = Path(os.path.expanduser(base_dir)).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def delete(self, task_id: str) -> bool:
        with self._lock:
            path = self._path_for(task_id)
            if not path.exists():
                return False
            path.unlink(missing_ok=True)
            return True

    def _path_for(self, task_id: str) -> Path:
        return self.base_dir / f"{task_id}.json"

    def _read_path_unlocked(self, path: Path) -> ServerUploadTask | None:
        if not path.exists():
            return None
        return ServerUploadTask.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def _read_unlocked(self, task_id: str) -> ServerUploadTask | None:
        return self._read_path_unlocked(self._path_for(task_id))

    def _write_unlocked(self, task: ServerUploadTask) -> None:
        task.updated_at = utc_now_iso()
        path = self._path_for(task.task_id)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(task.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def create(
        self,
        *,
        object_name: str,
        filename: str,
        content_type: str,
        total_size: int,
        strategy: str,
        temp_path: str,
        parallelism: int,
        total_parts: int | None,
        upload_session_id: str | None,
        multipart_upload_id: str | None,
    ) -> ServerUploadTask:
        now = utc_now_iso()
        task = ServerUploadTask(
            task_id=uuid.uuid4().hex,
            object_name=object_name,
            filename=filename,
            content_type=content_type,
            total_size=total_size,
            strategy=strategy,
            status="queued",
            phase="waiting",
            created_at=now,
            updated_at=now,
            temp_path=temp_path,
            upload_session_id=upload_session_id,
            multipart_upload_id=multipart_upload_id,
            uploaded_parts=[],
            total_parts=total_parts,
            parallelism=max(1, parallelism),
        )
        self.save(task)
        return task

    def save(self, task: ServerUploadTask) -> None:
        with self._lock:
            self._write_unlocked(task)

    def get(self, task_id: str) -> ServerUploadTask | None:
        with self._lock:
            return self._read_unlocked(task_id)

    def update(self, task_id: str, mutator: Callable[[ServerUploadTask], None]) -> ServerUploadTask:
        with self._lock:
            task = self._read_unlocked(task_id)
            if not task:
                raise FileNotFoundError(task_id)
            mutator(task)
            self._write_unlocked(task)
            return task

    def list_recent(self, limit: int = 20) -> list[ServerUploadTask]:
        tasks: list[ServerUploadTask] = []
        for path in sorted(self.base_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                task = self._read_path_unlocked(path)
                if task:
                    tasks.append(task)
            except Exception:
                continue
            if len(tasks) >= limit:
                break
        return tasks

    def list_all(self) -> list[ServerUploadTask]:
        tasks: list[ServerUploadTask] = []
        for path in sorted(self.base_dir.glob("*.json"), key=lambda p: p.stat().st_mtime):
            try:
                task = self._read_path_unlocked(path)
                if task:
                    tasks.append(task)
            except Exception:
                continue
        return tasks


class ServerUploadTaskManager:
    def __init__(self, settings: Settings | None = None, *, auto_recover: bool = True) -> None:
        self.settings = settings or get_settings()
        self.task_store = ServerUploadTaskStore(self.settings.upload_task_dir)
        self.session_store = UploadSessionStore(self.settings.upload_session_dir)
        self.temp_dir = Path(os.path.expanduser(self.settings.upload_temp_dir)).resolve()
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self._threads: dict[str, threading.Thread] = {}
        self._threads_lock = threading.Lock()
        self._max_retry_attempts = 3
        self._base_retry_delay_seconds = 1.0
        self._max_retry_delay_seconds = 8.0
        self._completed_task_grace_period_seconds = max(0.0, float(self.settings.upload_completed_task_visible_seconds))
        if auto_recover:
            self.recover_incomplete_tasks()

    def temp_path_for(self, task_hint: str, filename: str) -> Path:
        safe_name = Path(filename).name or "upload.bin"
        return self.temp_dir / f"{task_hint}-{safe_name}"

    def create_task_from_staged_file(self, *, filename: str, content_type: str | None, staged_path: str, total_size: int) -> ServerUploadTask:
        settings = self.settings
        object_name = object_name_from_upload(filename)
        content_type = content_type or "application/octet-stream"
        chunk_size = settings.upload_chunk_size_mb * 1024 * 1024
        threshold = settings.upload_single_put_threshold_mb * 1024 * 1024
        strategy = "single-put-server-proxy" if total_size <= threshold else "oci-multipart-server-proxy"

        multipart_upload_id = None
        upload_session_id = None
        total_parts = None
        if strategy != "single-put-server-proxy":
            storage = OCIStorageService(settings)
            multipart_upload_id = storage.create_multipart_upload(object_name, content_type)
            session = self.session_store.create(
                object_name=object_name,
                content_type=content_type,
                total_size=total_size,
                chunk_size=chunk_size,
                parallelism=settings.upload_parallelism,
                strategy="oci-multipart-server-proxy",
                fingerprint=f"server-task:{uuid.uuid4().hex}",
                multipart_upload_id=multipart_upload_id,
            )
            upload_session_id = session.upload_id
            total_parts = (total_size + chunk_size - 1) // chunk_size

        task = self.task_store.create(
            object_name=object_name,
            filename=filename,
            content_type=content_type,
            total_size=total_size,
            strategy=strategy,
            temp_path=staged_path,
            parallelism=settings.upload_parallelism if strategy != "single-put-server-proxy" else 1,
            total_parts=total_parts,
            upload_session_id=upload_session_id,
            multipart_upload_id=multipart_upload_id,
        )
        self.start(task.task_id)
        return task

    def start(self, task_id: str) -> bool:
        task = self.task_store.get(task_id)
        if not task or task.status in TERMINAL_STATUSES:
            return False
        with self._threads_lock:
            existing = self._threads.get(task_id)
            if existing and existing.is_alive():
                return False
            thread = threading.Thread(target=self._run_task_safe, args=(task_id,), daemon=True, name=f"upload-task-{task_id[:8]}")
            self._threads[task_id] = thread
        thread.start()
        return True

    def cancel(self, task_id: str) -> ServerUploadTask | None:
        task = self.task_store.get(task_id)
        if not task:
            return None
        if task.status in TERMINAL_STATUSES:
            return task
        return self.task_store.update(task_id, lambda t: setattr(t, "status", "canceled"))

    def recover_incomplete_tasks(self) -> list[str]:
        recovered: list[str] = []
        for task in self.task_store.list_all():
            if task.status not in ACTIVE_STATUSES:
                continue
            if not Path(task.temp_path).exists():
                self.task_store.update(
                    task.task_id,
                    lambda t: (
                        setattr(t, "status", "failed"),
                        setattr(t, "phase", "recovery_missing_temp_file"),
                        setattr(t, "error", "服务重启后恢复失败：暂存文件不存在"),
                    ),
                )
                continue
            self.task_store.update(
                task.task_id,
                lambda t: (
                    setattr(t, "status", "queued"),
                    setattr(t, "phase", f"recovery_requeued_from:{task.status}"),
                    setattr(t, "error", None),
                ),
            )
            if self.start(task.task_id):
                recovered.append(task.task_id)
        return recovered

    def _run_task_safe(self, task_id: str) -> None:
        try:
            self._run_task(task_id)
        except Exception as exc:
            try:
                self.task_store.update(task_id, lambda t: (setattr(t, "status", "failed"), setattr(t, "phase", "error"), setattr(t, "error", str(exc))))
            except Exception:
                pass
        finally:
            with self._threads_lock:
                self._threads.pop(task_id, None)

    def _ensure_not_canceled(self, task_id: str) -> ServerUploadTask:
        task = self.task_store.get(task_id)
        if not task:
            raise FileNotFoundError(task_id)
        if task.status == "canceled":
            raise RuntimeError("上传任务已取消")
        return task

    def _run_task(self, task_id: str) -> None:
        task = self._ensure_not_canceled(task_id)
        if task.strategy == "single-put-server-proxy":
            self._run_single_put(task)
            return
        self._run_multipart(task)

    def _compute_retry_delay(self, attempt: int, retry_after_seconds: int | None = None) -> float:
        if retry_after_seconds is not None and retry_after_seconds > 0:
            return min(float(retry_after_seconds), self._max_retry_delay_seconds)
        return min(self._base_retry_delay_seconds * (2 ** max(0, attempt - 1)), self._max_retry_delay_seconds)

    def _sleep_with_cancel(self, task_id: str, delay_seconds: float) -> None:
        remaining = max(0.0, delay_seconds)
        while remaining > 0:
            self._ensure_not_canceled(task_id)
            step = min(0.25, remaining)
            time.sleep(step)
            remaining -= step

    def _run_single_put(self, task: ServerUploadTask) -> None:
        self.task_store.update(task.task_id, lambda t: (setattr(t, "status", "running"), setattr(t, "phase", "uploading_to_oci"), setattr(t, "error", None)))
        storage = OCIStorageService(self.settings)
        last_error: str | None = None
        for attempt in range(1, self._max_retry_attempts + 1):
            self._ensure_not_canceled(task.task_id)
            try:
                with open(task.temp_path, "rb") as fileobj:
                    storage.upload_file(task.object_name, fileobj, task.content_type)
                self.task_store.update(
                    task.task_id,
                    lambda t: (
                        setattr(t, "uploaded_bytes", t.total_size),
                        setattr(t, "status", "completed"),
                        setattr(t, "phase", "done"),
                        setattr(t, "error", None),
                    ),
                )
                self._cleanup_temp_file(task.temp_path)
                self._schedule_completed_task_removal(task.task_id)
                return
            except Exception as exc:
                category, retryable, _status_code, reason, retry_after_seconds = classify_upload_exception(exc)
                last_error = reason
                if not retryable or attempt >= self._max_retry_attempts:
                    raise OCIStorageError(
                        f"上传失败（single-put，{'可重试' if retryable else '不可重试'}，{category}）: {reason}",
                        category=category,
                        retryable=retryable,
                        reason=reason,
                        retry_after_seconds=retry_after_seconds,
                    ) from exc
                delay = self._compute_retry_delay(attempt, retry_after_seconds)
                self.task_store.update(
                    task.task_id,
                    lambda t: (
                        setattr(t, "status", "running"),
                        setattr(t, "phase", f"retrying_single_put:{attempt}/{self._max_retry_attempts}"),
                        setattr(t, "error", f"single-put 上传失败，{delay:.1f} 秒后重试：{reason}"),
                    ),
                )
                self._sleep_with_cancel(task.task_id, delay)
        raise RuntimeError(last_error or "single-put 上传失败")

    def _read_chunk(self, path: str, start: int, size: int) -> bytes:
        with open(path, "rb") as fileobj:
            fileobj.seek(start)
            return fileobj.read(size)

    def _upload_one_part(self, *, task: ServerUploadTask, part_num: int, chunk_size: int) -> tuple[int, str, int]:
        start = (part_num - 1) * chunk_size
        size = min(chunk_size, task.total_size - start)
        payload = self._read_chunk(task.temp_path, start, size)
        storage = OCIStorageService(self.settings)
        last_error: str | None = None
        for attempt in range(1, self._max_retry_attempts + 1):
            try:
                self._ensure_not_canceled(task.task_id)
                etag = storage.upload_part(
                    object_name=task.object_name,
                    multipart_upload_id=task.multipart_upload_id or "",
                    part_num=part_num,
                    payload=payload,
                    content_type=task.content_type,
                )
                return part_num, etag, size
            except OCIStorageError as exc:
                last_error = str(exc)
                if not exc.retryable or attempt >= self._max_retry_attempts:
                    raise
                delay = self._compute_retry_delay(attempt, exc.retry_after_seconds)
                self.task_store.update(
                    task.task_id,
                    lambda t: (
                        setattr(t, "status", "running"),
                        setattr(t, "phase", f"retrying_part:{part_num}（第{attempt}次重试）"),
                        setattr(t, "error", f"分片 {part_num} 上传失败，{delay:.1f} 秒后重试：{exc.reason}"),
                    ),
                )
                self._sleep_with_cancel(task.task_id, delay)
            except Exception as exc:
                category, retryable, _status_code, reason, retry_after_seconds = classify_upload_exception(exc)
                last_error = reason
                if not retryable or attempt >= self._max_retry_attempts:
                    raise OCIStorageError(
                        f"上传分片失败（part {part_num}，{'可重试' if retryable else '不可重试'}，{category}）: {reason}",
                        category=category,
                        retryable=retryable,
                        reason=reason,
                        retry_after_seconds=retry_after_seconds,
                    ) from exc
                delay = self._compute_retry_delay(attempt, retry_after_seconds)
                self.task_store.update(
                    task.task_id,
                    lambda t: (
                        setattr(t, "status", "running"),
                        setattr(t, "phase", f"retrying_part:{part_num}（第{attempt}次重试）"),
                        setattr(t, "error", f"分片 {part_num} 上传失败，{delay:.1f} 秒后重试：{reason}"),
                    ),
                )
                self._sleep_with_cancel(task.task_id, delay)
        raise RuntimeError(last_error or f"part {part_num} 上传失败")

    def _load_uploaded_parts_from_session(self, task: ServerUploadTask, total_parts: int) -> dict[int, UploadedPart]:
        if not task.upload_session_id:
            return {}
        session = self.session_store.get(task.upload_session_id)
        if not session:
            return {}
        uploaded_parts: dict[int, UploadedPart] = {}
        for part_num, part in session.uploaded_parts.items():
            if 1 <= part_num <= total_parts and part.etag:
                uploaded_parts[part_num] = UploadedPart(part_num=part_num, etag=part.etag, size=int(part.size))
        return uploaded_parts

    def _reconcile_uploaded_parts_with_remote(self, task: ServerUploadTask, uploaded_parts: dict[int, UploadedPart], total_parts: int) -> dict[int, UploadedPart]:
        if not task.multipart_upload_id:
            return uploaded_parts
        remote_parts = OCIStorageService(self.settings).list_multipart_uploaded_parts(
            object_name=task.object_name,
            multipart_upload_id=task.multipart_upload_id,
        )
        if not remote_parts:
            return uploaded_parts
        reconciled: dict[int, UploadedPart] = {}
        for part_num, etag in remote_parts.items():
            if not (1 <= int(part_num) <= total_parts) or not etag:
                continue
            size = uploaded_parts.get(int(part_num), UploadedPart(part_num=int(part_num), etag=etag, size=0)).size
            if size <= 0:
                start = (int(part_num) - 1) * self.settings.upload_chunk_size_mb * 1024 * 1024
                size = min(self.settings.upload_chunk_size_mb * 1024 * 1024, task.total_size - start)
            reconciled[int(part_num)] = UploadedPart(part_num=int(part_num), etag=etag, size=size)
        return reconciled or uploaded_parts

    def _persist_uploaded_parts_snapshot(self, task_id: str, uploaded_parts: dict[int, UploadedPart], total_parts: int) -> None:
        uploaded_bytes = sum(max(0, part.size) for part in uploaded_parts.values())
        uploaded_part_nums = sorted(uploaded_parts.keys())
        self.task_store.update(
            task_id,
            lambda t: (
                setattr(t, "uploaded_bytes", uploaded_bytes),
                setattr(t, "uploaded_parts", uploaded_part_nums),
                setattr(t, "phase", "finalizing" if len(uploaded_part_nums) >= total_parts else f"uploading_parts:{len(uploaded_part_nums)}/{total_parts}"),
            ),
        )

    def _run_multipart(self, task: ServerUploadTask) -> None:
        task = self.task_store.update(task.task_id, lambda t: (setattr(t, "status", "running"), setattr(t, "phase", "uploading_to_oci"), setattr(t, "error", None)))
        chunk_size = self.settings.upload_chunk_size_mb * 1024 * 1024
        total_parts = task.total_parts or ((task.total_size + chunk_size - 1) // chunk_size)
        uploaded_parts_map = self._load_uploaded_parts_from_session(task, total_parts)
        if task.uploaded_parts:
            for part_num in task.uploaded_parts:
                part_num = int(part_num)
                if part_num not in uploaded_parts_map and 1 <= part_num <= total_parts:
                    start = (part_num - 1) * chunk_size
                    size = min(chunk_size, task.total_size - start)
                    uploaded_parts_map[part_num] = UploadedPart(part_num=part_num, etag="", size=size)
        if task.multipart_upload_id:
            try:
                uploaded_parts_map = self._reconcile_uploaded_parts_with_remote(task, uploaded_parts_map, total_parts)
            except Exception:
                pass

        uploaded_parts: list[tuple[int, str]] = [
            (part_num, part.etag)
            for part_num, part in sorted(uploaded_parts_map.items())
            if part.etag
        ]
        self._persist_uploaded_parts_snapshot(task.task_id, uploaded_parts_map, total_parts)
        pending_parts = [part_num for part_num in range(1, total_parts + 1) if part_num not in uploaded_parts_map or not uploaded_parts_map[part_num].etag]

        def persist_part(result: tuple[int, str, int]) -> None:
            part_num, etag, size = result
            uploaded_parts_map[part_num] = UploadedPart(part_num=part_num, etag=etag, size=size)
            if task.upload_session_id:
                try:
                    self.session_store.update(
                        task.upload_session_id,
                        lambda session: session.uploaded_parts.__setitem__(part_num, UploadedPart(part_num=part_num, etag=etag, size=size)),
                    )
                except Exception:
                    pass
            uploaded_parts[:] = [(num, tag) for num, tag in uploaded_parts if num != part_num]
            uploaded_parts.append((part_num, etag))
            self._persist_uploaded_parts_snapshot(task.task_id, uploaded_parts_map, total_parts)

        executor = ThreadPoolExecutor(max_workers=max(1, task.parallelism), thread_name_prefix="oci-part")
        futures = set()
        try:
            while pending_parts or futures:
                self._ensure_not_canceled(task.task_id)
                while pending_parts and len(futures) < max(1, task.parallelism):
                    part_num = pending_parts.pop(0)
                    futures.add(executor.submit(self._upload_one_part, task=task, part_num=part_num, chunk_size=chunk_size))
                done, futures = wait(futures, return_when=FIRST_COMPLETED)
                for future in done:
                    persist_part(future.result())

            self.task_store.update(task.task_id, lambda t: (setattr(t, "phase", "finalizing"), setattr(t, "status", "finalizing")))
            storage = OCIStorageService(self.settings)
            storage.commit_multipart_upload(
                object_name=task.object_name,
                multipart_upload_id=task.multipart_upload_id or "",
                parts=sorted(uploaded_parts, key=lambda item: item[0]),
            )
            if task.upload_session_id:
                try:
                    self.session_store.update(task.upload_session_id, lambda session: setattr(session, "completed", True))
                except Exception:
                    pass
            self.task_store.update(
                task.task_id,
                lambda t: (
                    setattr(t, "uploaded_bytes", t.total_size),
                    setattr(t, "uploaded_parts", list(range(1, total_parts + 1))),
                    setattr(t, "status", "completed"),
                    setattr(t, "phase", "done"),
                    setattr(t, "error", None),
                ),
            )
            self._cleanup_temp_file(task.temp_path)
            self._schedule_completed_task_removal(task.task_id)
        except Exception:
            try:
                refreshed = self.task_store.get(task.task_id)
                if refreshed and refreshed.multipart_upload_id and refreshed.status == "canceled":
                    OCIStorageService(self.settings).abort_multipart_upload(
                        object_name=refreshed.object_name,
                        multipart_upload_id=refreshed.multipart_upload_id,
                    )
            except Exception:
                pass
            raise
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _cleanup_temp_file(self, path: str) -> None:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass

    def _schedule_completed_task_removal(self, task_id: str) -> None:
        delay = max(0.0, float(self._completed_task_grace_period_seconds))

        def remove_later() -> None:
            if delay > 0:
                time.sleep(delay)
            try:
                task = self.task_store.get(task_id)
                if not task or task.status != "completed":
                    return
                self.task_store.delete(task_id)
            except Exception:
                pass

        threading.Thread(
            target=remove_later,
            daemon=True,
            name=f"upload-task-cleanup-{task_id[:8]}",
        ).start()


_manager: ServerUploadTaskManager | None = None
_manager_lock = threading.Lock()


def get_upload_task_manager() -> ServerUploadTaskManager:
    global _manager
    if _manager is not None:
        return _manager
    with _manager_lock:
        if _manager is None:
            _manager = ServerUploadTaskManager()
    return _manager


__all__ = [
    "ServerUploadTask",
    "ServerUploadTaskStore",
    "ServerUploadTaskManager",
    "describe_task_state",
    "get_upload_task_manager",
]
