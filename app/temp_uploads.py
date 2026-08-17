from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


_TEMP_UPLOAD_LOCK = threading.RLock()

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


class UploadQuotaExceeded(ValueError):
    pass


class TempUploadSessionStore:
    def __init__(self, base_dir: str) -> None:
        self.base_dir = Path(os.path.expanduser(base_dir)).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = _TEMP_UPLOAD_LOCK

    @property
    def lock(self) -> threading.RLock:
        return self._lock

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
        max_staging_bytes: int | None = None,
        max_active_sessions: int | None = None,
    ) -> TempUploadSession:
        with self._lock:
            staging_bytes = 0
            active_sessions = 0
            for path in self.base_dir.glob("*.upload.json"):
                try:
                    existing = TempUploadSession.from_dict(json.loads(path.read_text(encoding="utf-8")))
                except Exception:
                    continue
                if not existing.committed:
                    active_sessions += 1
                staged = Path(existing.staged_path)
                actual_size = staged.stat().st_size if staged.exists() else 0
                staging_bytes += max(actual_size, max(0, existing.total_size))

            if max_active_sessions is not None and active_sessions >= max_active_sessions:
                raise UploadQuotaExceeded("当前上传任务数量已达到上限，请等待已有任务完成后再试")
            if max_staging_bytes is not None and staging_bytes + total_size > max_staging_bytes:
                raise UploadQuotaExceeded("服务器暂存空间已达到上限，请等待已有上传完成或清理后再试")

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
            self._write_unlocked(session)
            return session

    def save(self, session: TempUploadSession) -> None:
        with self._lock:
            self._write_unlocked(session)
    def delete(self, temp_upload_id: str) -> bool:
        with self._lock:
            path = self._path_for(temp_upload_id)
            tmp_path = path.with_suffix(".upload.json.tmp")
            existed = path.exists() or tmp_path.exists()
            path.unlink(missing_ok=True)
            tmp_path.unlink(missing_ok=True)
            return existed

    def stage_chunk(self, temp_upload_id: str, chunk_index: int, body: bytes, chunk_sha256: str | None = None) -> tuple[TempUploadSession, bool, int]:
        """Validate, write, and persist one chunk under the same lock."""
        with self._lock:
            session = self._read_unlocked(temp_upload_id)
            if not session:
                raise FileNotFoundError(temp_upload_id)
            if session.committed:
                raise ValueError("临时上传已提交，不能继续写入")
            if chunk_index < 0 or chunk_index >= session.total_chunks:
                raise ValueError("chunk_index 超出范围")
            expected_size = session.chunk_size if chunk_index < session.total_chunks - 1 else session.total_size - session.chunk_size * (session.total_chunks - 1)
            if len(body) != expected_size:
                raise ValueError(f"chunk 大小不匹配，期望 {expected_size}，实际 {len(body)}")
            body_sha256 = hashlib.sha256(body).hexdigest()
            if chunk_sha256 and chunk_sha256.lower() != body_sha256:
                raise ValueError("chunk 校验失败：sha256 不匹配")
            existing = session.uploaded_chunks.get(chunk_index)
            staged_path = Path(session.staged_path)
            if existing:
                if existing.size != len(body) or existing.sha256 != body_sha256:
                    raise ValueError("该 chunk 已存在且内容不一致，请确认是否选择了同一文件")
                return session, True, staged_path.stat().st_size if staged_path.exists() else session.uploaded_bytes
            staged_path.parent.mkdir(parents=True, exist_ok=True)
            if not staged_path.exists():
                staged_path.write_bytes(b"")
            with open(staged_path, "r+b") as fileobj:
                fileobj.seek(chunk_index * session.chunk_size)
                fileobj.write(body)
            session.uploaded_chunks[chunk_index] = UploadedChunk(chunk_index=chunk_index, size=len(body), sha256=body_sha256)
            self._write_unlocked(session)
            return session, False, staged_path.stat().st_size

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
        with self._lock:
            return self._find_active_by_fingerprint_unlocked(file_fingerprint)

    def _find_active_by_fingerprint_unlocked(self, file_fingerprint: str) -> TempUploadSession | None:
        for path in sorted(self.base_dir.glob("*.upload.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                session = TempUploadSession.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                continue
            if session.file_fingerprint == file_fingerprint and not session.committed:
                return session
        return None

__all__ = ["UploadQuotaExceeded", "UploadedChunk", "TempUploadSession", "TempUploadSessionStore"]
