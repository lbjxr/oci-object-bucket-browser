from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class UploadedPart:
    part_num: int
    etag: str
    size: int


@dataclass
class UploadSession:
    upload_id: str
    object_name: str
    content_type: str
    total_size: int
    chunk_size: int
    parallelism: int
    strategy: str
    fingerprint: str
    created_at: str
    updated_at: str
    multipart_upload_id: str | None = None
    completed: bool = False
    uploaded_parts: dict[int, UploadedPart] = field(default_factory=dict)

    @property
    def uploaded_bytes(self) -> int:
        return sum(part.size for part in self.uploaded_parts.values())

    @property
    def uploaded_part_numbers(self) -> list[int]:
        return sorted(self.uploaded_parts.keys())

    def to_dict(self) -> dict:
        data = asdict(self)
        data["uploaded_parts"] = {str(k): asdict(v) for k, v in self.uploaded_parts.items()}
        return data

    @classmethod
    def from_dict(cls, payload: dict) -> "UploadSession":
        uploaded_parts = {
            int(k): UploadedPart(
                part_num=int(v["part_num"]),
                etag=v["etag"],
                size=int(v["size"]),
            )
            for k, v in (payload.get("uploaded_parts") or {}).items()
        }
        return cls(
            upload_id=payload["upload_id"],
            object_name=payload["object_name"],
            content_type=payload["content_type"],
            total_size=int(payload["total_size"]),
            chunk_size=int(payload["chunk_size"]),
            parallelism=int(payload["parallelism"]),
            strategy=payload["strategy"],
            fingerprint=payload["fingerprint"],
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
            multipart_upload_id=payload.get("multipart_upload_id"),
            completed=bool(payload.get("completed", False)),
            uploaded_parts=uploaded_parts,
        )


class UploadSessionStore:
    def __init__(self, base_dir: str) -> None:
        self.base_dir = Path(os.path.expanduser(base_dir)).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path_for(self, upload_id: str) -> Path:
        return self.base_dir / f"{upload_id}.json"

    def _read_unlocked(self, upload_id: str) -> UploadSession | None:
        path = self._path_for(upload_id)
        if not path.exists():
            return None
        return UploadSession.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def _write_unlocked(self, session: UploadSession) -> None:
        session.updated_at = utc_now_iso()
        path = self._path_for(session.upload_id)
        tmp_path = path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(session.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def create(
        self,
        *,
        object_name: str,
        content_type: str,
        total_size: int,
        chunk_size: int,
        parallelism: int,
        strategy: str,
        fingerprint: str,
        multipart_upload_id: str | None,
    ) -> UploadSession:
        now = utc_now_iso()
        session = UploadSession(
            upload_id=uuid.uuid4().hex,
            object_name=object_name,
            content_type=content_type,
            total_size=total_size,
            chunk_size=chunk_size,
            parallelism=parallelism,
            strategy=strategy,
            fingerprint=fingerprint,
            multipart_upload_id=multipart_upload_id,
            created_at=now,
            updated_at=now,
        )
        self.save(session)
        return session

    def save(self, session: UploadSession) -> None:
        with self._lock:
            self._write_unlocked(session)

    def get(self, upload_id: str) -> UploadSession | None:
        with self._lock:
            return self._read_unlocked(upload_id)

    def update(self, upload_id: str, mutator) -> UploadSession:
        with self._lock:
            session = self._read_unlocked(upload_id)
            if not session:
                raise FileNotFoundError(upload_id)
            mutator(session)
            self._write_unlocked(session)
            return session

    def delete(self, upload_id: str) -> None:
        path = self._path_for(upload_id)
        with self._lock:
            if path.exists():
                path.unlink()

    def find_active_by_fingerprint(self, fingerprint: str) -> UploadSession | None:
        for path in sorted(self.base_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                session = UploadSession.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
            if session.fingerprint == fingerprint and not session.completed:
                return session
        return None


__all__ = ["UploadSession", "UploadedPart", "UploadSessionStore"]
