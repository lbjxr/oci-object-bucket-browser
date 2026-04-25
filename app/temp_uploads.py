from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class UploadedChunk:
    chunk_index: int
    size: int
    sha256: str


@dataclass
class TempUploadSession:
    temp_upload_id: str
    filename: str
    object_name: str
    content_type: str
    total_size: int
    chunk_size: int
    strategy: str
    file_fingerprint: str
    staged_path: str
    created_at: str
    updated_at: str
    committed: bool = False
    uploaded_chunks: dict[int, UploadedChunk] = field(default_factory=dict)

    @property
    def total_chunks(self) -> int:
        if self.total_size <= 0 or self.chunk_size <= 0:
            return 0
        return (self.total_size + self.chunk_size - 1) // self.chunk_size

    @property
    def uploaded_chunk_indexes(self) -> list[int]:
        return sorted(self.uploaded_chunks.keys())

    @property
    def uploaded_bytes(self) -> int:
        return sum(chunk.size for chunk in self.uploaded_chunks.values())

    @property
    def missing_chunk_indexes(self) -> list[int]:
        return [idx for idx in range(self.total_chunks) if idx not in self.uploaded_chunks]

    def to_dict(self) -> dict:
        data = asdict(self)
        data["uploaded_chunks"] = {str(k): asdict(v) for k, v in self.uploaded_chunks.items()}
        return data

    @classmethod
    def from_dict(cls, payload: dict) -> "TempUploadSession":
        uploaded_chunks = {
            int(k): UploadedChunk(
                chunk_index=int(v["chunk_index"]),
                size=int(v["size"]),
                sha256=v["sha256"],
            )
            for k, v in (payload.get("uploaded_chunks") or {}).items()
        }
        return cls(
            temp_upload_id=payload["temp_upload_id"],
            filename=payload["filename"],
            object_name=payload["object_name"],
            content_type=payload["content_type"],
            total_size=int(payload["total_size"]),
            chunk_size=int(payload["chunk_size"]),
            strategy=payload["strategy"],
            file_fingerprint=payload["file_fingerprint"],
            staged_path=payload["staged_path"],
            created_at=payload["created_at"],
            updated_at=payload["updated_at"],
            committed=bool(payload.get("committed", False)),
            uploaded_chunks=uploaded_chunks,
        )


class TempUploadSessionStore:
    def __init__(self, base_dir: str) -> None:
        self.base_dir = Path(os.path.expanduser(base_dir)).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path_for(self, temp_upload_id: str) -> Path:
        return self.base_dir / f"{temp_upload_id}.upload.json"

    def _read_unlocked(self, temp_upload_id: str) -> TempUploadSession | None:
        path = self._path_for(temp_upload_id)
        if not path.exists():
            return None
        return TempUploadSession.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def _write_unlocked(self, session: TempUploadSession) -> None:
        session.updated_at = utc_now_iso()
        path = self._path_for(session.temp_upload_id)
        tmp_path = path.with_suffix(".upload.json.tmp")
        tmp_path.write_text(json.dumps(session.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(path)

    def create(
        self,
        *,
        temp_upload_id: str,
        filename: str,
        object_name: str,
        content_type: str,
        total_size: int,
        chunk_size: int,
        strategy: str,
        file_fingerprint: str,
        staged_path: str,
    ) -> TempUploadSession:
        now = utc_now_iso()
        session = TempUploadSession(
            temp_upload_id=temp_upload_id,
            filename=filename,
            object_name=object_name,
            content_type=content_type,
            total_size=total_size,
            chunk_size=chunk_size,
            strategy=strategy,
            file_fingerprint=file_fingerprint,
            staged_path=staged_path,
            created_at=now,
            updated_at=now,
        )
        self.save(session)
        return session

    def save(self, session: TempUploadSession) -> None:
        with self._lock:
            self._write_unlocked(session)

    def get(self, temp_upload_id: str) -> TempUploadSession | None:
        with self._lock:
            return self._read_unlocked(temp_upload_id)

    def update(self, temp_upload_id: str, mutator) -> TempUploadSession:
        with self._lock:
            session = self._read_unlocked(temp_upload_id)
            if not session:
                raise FileNotFoundError(temp_upload_id)
            mutator(session)
            self._write_unlocked(session)
            return session

    def find_active_by_fingerprint(self, file_fingerprint: str) -> TempUploadSession | None:
        for path in sorted(self.base_dir.glob("*.upload.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                session = TempUploadSession.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
            if session.file_fingerprint == file_fingerprint and not session.committed:
                return session
        return None


__all__ = ["UploadedChunk", "TempUploadSession", "TempUploadSessionStore"]
