from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.config import get_settings
from app.file_browser import classify_file_type
from app.security import hash_password, verify_password


class ShareAccessError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def share_status(record: dict, *, now: datetime | None = None) -> str:
    effective_now = (now or utc_now()).astimezone(timezone.utc)
    if record.get("revoked_at"):
        return "revoked"
    expires_at = parse_utc(record.get("expires_at"))
    if expires_at is not None and effective_now >= expires_at:
        return "expired"
    limit = record.get("download_limit")
    if limit is not None and int(record.get("download_count", 0)) >= int(limit):
        return "exhausted"
    return "active"


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _error_for_status(status_value: str) -> ShareAccessError:
    if status_value == "revoked":
        return ShareAccessError("revoked", "该分享链接已撤销", 410)
    if status_value == "expired":
        return ShareAccessError("expired", "该分享链接已过期", 410)
    if status_value == "exhausted":
        return ShareAccessError("exhausted", "该分享链接的下载次数已用完", 410)
    return ShareAccessError("not_found", "分享链接不存在", 404)


class ShareStore:
    def __init__(self, path: str) -> None:
        self.path = Path(os.path.expanduser(path)).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._payload = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"version": 1, "shares": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "shares": []}
        shares = payload.get("shares")
        return {"version": 1, "shares": shares if isinstance(shares, list) else []}

    def _write_unlocked(self) -> None:
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(json.dumps(self._payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.path)

    def create(
        self,
        *,
        object_key: str,
        expires_at: datetime,
        download_limit: int | None,
        password: str = "",
        now: datetime | None = None,
    ) -> tuple[dict, str]:
        created_at = (now or utc_now()).astimezone(timezone.utc)
        token = secrets.token_urlsafe(32)
        record = {
            "id": f"share_{secrets.token_hex(8)}",
            "object_key": object_key,
            "token_hash": _token_hash(token),
            "password_hash": hash_password(password) if password else None,
            "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
            "download_limit": download_limit,
            "download_count": 0,
            "access_count": 0,
            "daily_accesses": {},
            "created_at": created_at.isoformat(),
            "last_accessed_at": None,
            "last_downloaded_at": None,
            "revoked_at": None,
        }
        with self._lock:
            self._payload["shares"].append(record)
            self._write_unlocked()
        return deepcopy(record), token

    def list_records(self) -> list[dict]:
        with self._lock:
            return deepcopy(self._payload["shares"])

    def get(self, share_id: str) -> dict | None:
        with self._lock:
            for record in self._payload["shares"]:
                if record.get("id") == share_id:
                    return deepcopy(record)
        return None

    def get_by_token(self, token: str) -> dict | None:
        candidate_hash = _token_hash(token)
        with self._lock:
            for record in self._payload["shares"]:
                stored_hash = str(record.get("token_hash") or "")
                if stored_hash and hmac.compare_digest(candidate_hash, stored_hash):
                    return deepcopy(record)
        return None

    def require_active(self, token: str, *, now: datetime | None = None) -> dict:
        record = self.get_by_token(token)
        if record is None:
            raise ShareAccessError("not_found", "分享链接不存在", 404)
        status_value = share_status(record, now=now)
        if status_value != "active":
            raise _error_for_status(status_value)
        return record

    def password_matches(self, record: dict, password: str) -> bool:
        encoded = record.get("password_hash")
        return not encoded or verify_password(password, str(encoded))

    def record_access(self, share_id: str, *, now: datetime | None = None) -> dict:
        accessed_at = (now or utc_now()).astimezone(timezone.utc)
        day_key = accessed_at.date().isoformat()
        with self._lock:
            record = self._find_unlocked(share_id)
            status_value = share_status(record, now=accessed_at)
            if status_value != "active":
                raise _error_for_status(status_value)
            record["access_count"] = int(record.get("access_count", 0)) + 1
            daily = record.setdefault("daily_accesses", {})
            daily[day_key] = int(daily.get(day_key, 0)) + 1
            record["last_accessed_at"] = accessed_at.isoformat()
            self._write_unlocked()
            return deepcopy(record)

    def reserve_download(self, share_id: str, *, now: datetime | None = None) -> dict:
        downloaded_at = (now or utc_now()).astimezone(timezone.utc)
        with self._lock:
            record = self._find_unlocked(share_id)
            status_value = share_status(record, now=downloaded_at)
            if status_value != "active":
                raise _error_for_status(status_value)
            record["download_count"] = int(record.get("download_count", 0)) + 1
            record["last_downloaded_at"] = downloaded_at.isoformat()
            self._write_unlocked()
            return deepcopy(record)

    def revoke(self, share_id: str, *, now: datetime | None = None) -> dict:
        revoked_at = (now or utc_now()).astimezone(timezone.utc)
        with self._lock:
            record = self._find_unlocked(share_id)
            if not record.get("revoked_at"):
                record["revoked_at"] = revoked_at.isoformat()
                self._write_unlocked()
            return deepcopy(record)

    def _find_unlocked(self, share_id: str) -> dict:
        for record in self._payload["shares"]:
            if record.get("id") == share_id:
                return record
        raise ShareAccessError("not_found", "分享记录不存在", 404)


def public_share(record: dict, *, now: datetime | None = None) -> dict:
    effective_now = (now or utc_now()).astimezone(timezone.utc)
    payload = {key: deepcopy(value) for key, value in record.items() if key not in {"token_hash", "password_hash", "daily_accesses"}}
    payload["status"] = share_status(record, now=effective_now)
    payload["password_protected"] = bool(record.get("password_hash"))
    limit = record.get("download_limit")
    payload["remaining_downloads"] = None if limit is None else max(0, int(limit) - int(record.get("download_count", 0)))
    payload["today_accesses"] = int((record.get("daily_accesses") or {}).get(effective_now.date().isoformat(), 0))
    payload["object_name"] = str(record.get("object_key") or "").rstrip("/").split("/")[-1]
    object_type = classify_file_type(None, str(record.get("object_key") or ""))
    payload["object_type"] = object_type
    payload["object_type_label"] = {
        "image": "图片",
        "pdf": "PDF",
        "text": "文本",
        "archive": "压缩包",
        "video": "视频",
        "audio": "音频",
        "other": "文件",
    }.get(object_type, "文件")
    return payload


def summarize_shares(records: list[dict], *, now: datetime | None = None) -> dict[str, int]:
    effective_now = (now or utc_now()).astimezone(timezone.utc)
    day_key = effective_now.date().isoformat()
    active_count = 0
    expiring_soon_count = 0
    today_access_count = 0
    for record in records:
        today_access_count += int((record.get("daily_accesses") or {}).get(day_key, 0))
        if share_status(record, now=effective_now) != "active":
            continue
        active_count += 1
        expires_at = parse_utc(record.get("expires_at"))
        if expires_at is not None and expires_at <= effective_now + timedelta(hours=24):
            expiring_soon_count += 1
    return {
        "active_count": active_count,
        "today_access_count": today_access_count,
        "expiring_soon_count": expiring_soon_count,
        "total_count": len(records),
    }


_store: ShareStore | None = None
_store_lock = threading.Lock()


def get_share_store() -> ShareStore:
    global _store
    path = Path(os.path.expanduser(get_settings().share_file)).resolve()
    if _store is not None and _store.path == path:
        return _store
    with _store_lock:
        if _store is None or _store.path != path:
            _store = ShareStore(str(path))
    return _store


def reset_share_store() -> None:
    global _store
    with _store_lock:
        _store = None
