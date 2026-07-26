from __future__ import annotations

import json
import os
import secrets
import threading
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from app.config import get_settings


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TrashRecordStore:
    def __init__(self, path: str) -> None:
        self.path = Path(os.path.expanduser(path)).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._payload = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "records": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "records": []}
        records = payload.get("records")
        if not isinstance(records, list):
            records = []
        return {"version": 1, "records": records}

    def _write_unlocked(self) -> None:
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(json.dumps(self._payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.path)

    def create(self, record: dict) -> dict:
        with self._lock:
            self._payload["records"].append(deepcopy(record))
            self._write_unlocked()
        return deepcopy(record)

    def update_status(self, record_id: str, *, status: str, deleted_at: str | None = None, error: str | None = None) -> dict:
        with self._lock:
            for record in self._payload["records"]:
                if record.get("id") != record_id:
                    continue
                record["status"] = status
                record["deleted_at"] = deleted_at
                record["error"] = error
                self._write_unlocked()
                return deepcopy(record)
        raise KeyError(record_id)

    def list_records(self) -> list[dict]:
        with self._lock:
            return deepcopy(self._payload["records"])


class RecycleBinService:
    def __init__(self, storage, record_store: TrashRecordStore) -> None:
        self.storage = storage
        self.record_store = record_store

    def recycle(self, object_name: str, *, deleted_by: str) -> dict:
        copied_at = _utc_now()
        timestamp = copied_at.strftime("%Y%m%dT%H%M%S.%fZ")
        trash_key = f".trash/{timestamp}/{object_name.lstrip('/')}"

        stream, content_type, headers = self.storage.open_stream(object_name)
        with closing(stream):
            self.storage.upload_file(trash_key, stream, content_type)

        size_text = (headers or {}).get("content-length")
        try:
            size = int(size_text) if size_text is not None else None
        except (TypeError, ValueError):
            size = None

        record = {
            "id": f"trash_{secrets.token_hex(8)}",
            "original_key": object_name,
            "trash_key": trash_key,
            "size": size,
            "content_type": content_type,
            "copied_at": copied_at.isoformat(),
            "deleted_at": None,
            "deleted_by": deleted_by,
            "status": "copied",
            "error": None,
        }
        try:
            self.record_store.create(record)
        except Exception as record_error:
            try:
                self.storage.delete_object(trash_key)
            except Exception as cleanup_error:
                raise RuntimeError(
                    f"回收站记录写入失败，且无法清理已复制对象 {trash_key}: {cleanup_error}"
                ) from record_error
            raise

        try:
            self.storage.delete_object(object_name)
        except Exception as exc:
            self.record_store.update_status(record["id"], status="delete_failed", error=str(exc))
            raise

        deleted_at = _utc_now().isoformat()
        return self.record_store.update_status(record["id"], status="deleted", deleted_at=deleted_at)


_store: TrashRecordStore | None = None
_store_lock = threading.Lock()


def get_trash_record_store() -> TrashRecordStore:
    global _store
    path = Path(os.path.expanduser(get_settings().trash_record_file)).resolve()
    if _store is not None and _store.path == path:
        return _store
    with _store_lock:
        if _store is None or _store.path != path:
            _store = TrashRecordStore(str(path))
    return _store


def reset_trash_record_store() -> None:
    global _store
    with _store_lock:
        _store = None
