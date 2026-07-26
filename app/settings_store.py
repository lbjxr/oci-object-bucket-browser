from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from app.config import Settings, get_settings
from app.security import hash_password


def _default_payload(settings: Settings) -> dict:
    return {
        "version": 1,
        "access": {
            "read_only_mode": False,
        },
        "storage": {
            "namespace": settings.namespace,
            "bucket_name": settings.bucket_name,
            "region": settings.region,
            "prefix_root": settings.prefix_root,
        },
        "upload": {
            "chunk_size_mb": settings.upload_chunk_size_mb,
            "parallelism": settings.upload_parallelism,
            "single_put_threshold_mb": settings.upload_single_put_threshold_mb,
        },
        "safety": {
            "trash_enabled": False,
            "batch_delete_confirmation_required": True,
        },
        "webdav": {
            "enabled": False,
            "username": "",
            "password_hash": "",
        },
    }


class AppSettingsStore:
    def __init__(self, path: str, deployment_settings: Settings | None = None) -> None:
        self.path = Path(os.path.expanduser(path)).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.deployment_settings = deployment_settings or get_settings()
        self._lock = threading.RLock()
        self._payload = self._load()

    def _load(self) -> dict:
        payload = _default_payload(self.deployment_settings)
        if not self.path.exists():
            return payload
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return payload
        for section in ("access", "storage", "upload", "safety", "webdav"):
            if isinstance(stored.get(section), dict):
                payload[section].update(stored[section])
        return payload

    def _write_unlocked(self) -> None:
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(json.dumps(self._payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.path)

    def snapshot(self) -> dict:
        with self._lock:
            return deepcopy(self._payload)

    def public_snapshot(self) -> dict:
        with self._lock:
            payload = deepcopy(self._payload)
        webdav = payload["webdav"]
        webdav["password_configured"] = bool(webdav.pop("password_hash", ""))
        payload["effective"] = {
            "namespace": self._effective_string("OCI_NAMESPACE", payload["storage"]["namespace"]),
            "bucket_name": self._effective_string("OCI_BUCKET_NAME", payload["storage"]["bucket_name"]),
            "region": self._effective_string("OCI_REGION", payload["storage"]["region"]),
            "prefix_root": self._effective_string("OCI_PREFIX_ROOT", payload["storage"]["prefix_root"]),
            "chunk_size_mb": self._effective_int("APP_UPLOAD_CHUNK_SIZE_MB", payload["upload"]["chunk_size_mb"]),
            "parallelism": self._effective_int("APP_UPLOAD_PARALLELISM", payload["upload"]["parallelism"]),
            "single_put_threshold_mb": self._effective_int(
                "APP_UPLOAD_SINGLE_PUT_THRESHOLD_MB",
                payload["upload"]["single_put_threshold_mb"],
            ),
        }
        payload["environment_overrides"] = {
            key: os.getenv(key) is not None
            for key in (
                "OCI_NAMESPACE",
                "OCI_BUCKET_NAME",
                "OCI_REGION",
                "OCI_PREFIX_ROOT",
                "APP_UPLOAD_CHUNK_SIZE_MB",
                "APP_UPLOAD_PARALLELISM",
                "APP_UPLOAD_SINGLE_PUT_THRESHOLD_MB",
            )
        }
        return payload

    @staticmethod
    def _effective_string(env_name: str, stored_value: str) -> str:
        value = os.getenv(env_name)
        return value.strip() if value is not None else str(stored_value or "").strip()

    @staticmethod
    def _effective_int(env_name: str, stored_value: int) -> int:
        value = os.getenv(env_name)
        return int(value) if value is not None else int(stored_value)

    def effective_settings(self) -> Settings:
        public = self.public_snapshot()["effective"]
        return replace(
            self.deployment_settings,
            namespace=public["namespace"],
            bucket_name=public["bucket_name"],
            region=public["region"],
            prefix_root=public["prefix_root"],
            upload_chunk_size_mb=max(8, int(public["chunk_size_mb"])),
            upload_parallelism=max(1, int(public["parallelism"])),
            upload_single_put_threshold_mb=max(1, int(public["single_put_threshold_mb"])),
        )

    def read_only_enabled(self) -> bool:
        with self._lock:
            return bool(self._payload["access"].get("read_only_mode", False))

    def trash_enabled(self) -> bool:
        with self._lock:
            return bool(self._payload["safety"].get("trash_enabled", False))

    def batch_delete_confirmation_required(self) -> bool:
        with self._lock:
            return bool(self._payload["safety"].get("batch_delete_confirmation_required", True))

    def webdav_credentials(self) -> tuple[bool, str, str]:
        with self._lock:
            config = self._payload["webdav"]
            return bool(config.get("enabled")), str(config.get("username", "")), str(config.get("password_hash", ""))

    def validate_update(self, payload: dict) -> dict:
        normalized = deepcopy(payload)
        storage = normalized.get("storage") or {}
        upload = normalized.get("upload") or {}
        access = normalized.get("access") or {}
        safety = normalized.get("safety") or {}
        webdav = normalized.get("webdav") or {}

        chunk_size = int(upload.get("chunk_size_mb", 16))
        parallelism = int(upload.get("parallelism", 6))
        threshold = int(upload.get("single_put_threshold_mb", 32))
        if not 8 <= chunk_size <= 512:
            raise ValueError("分片大小必须在 8-512 MB 之间")
        if not 1 <= parallelism <= 32:
            raise ValueError("并发数必须在 1-32 之间")
        if not 1 <= threshold <= 5120:
            raise ValueError("单 PUT 阈值必须在 1-5120 MB 之间")

        prefix_root = str(storage.get("prefix_root", "")).strip().strip("/")
        if prefix_root:
            prefix_root += "/"
        username = str(webdav.get("username", "")).strip()
        password = str(webdav.get("password", ""))
        password_configured = bool(self.snapshot()["webdav"].get("password_hash"))
        if bool(webdav.get("enabled")) and not username:
            raise ValueError("启用 WebDAV 前必须填写用户名")
        if bool(webdav.get("enabled")) and not password and not password_configured:
            raise ValueError("启用 WebDAV 前必须设置密码")
        if password and len(password) < 8:
            raise ValueError("WebDAV 密码至少需要 8 个字符")

        return {
            "access": {"read_only_mode": bool(access.get("read_only_mode", False))},
            "storage": {
                "namespace": str(storage.get("namespace", "")).strip(),
                "bucket_name": str(storage.get("bucket_name", "")).strip(),
                "region": str(storage.get("region", "")).strip(),
                "prefix_root": prefix_root,
            },
            "upload": {
                "chunk_size_mb": chunk_size,
                "parallelism": parallelism,
                "single_put_threshold_mb": threshold,
            },
            "safety": {
                "trash_enabled": bool(safety.get("trash_enabled", False)),
                "batch_delete_confirmation_required": bool(safety.get("batch_delete_confirmation_required", True)),
            },
            "webdav": {
                "enabled": bool(webdav.get("enabled", False)),
                "username": username,
                "password": password,
            },
        }

    def update(self, payload: dict) -> dict:
        normalized = self.validate_update(payload)
        with self._lock:
            for section in ("access", "storage", "upload", "safety"):
                self._payload[section].update(normalized[section])
            webdav = normalized["webdav"]
            self._payload["webdav"]["enabled"] = webdav["enabled"]
            self._payload["webdav"]["username"] = webdav["username"]
            if webdav["password"]:
                self._payload["webdav"]["password_hash"] = hash_password(webdav["password"])
            self._write_unlocked()
        return self.public_snapshot()


_store: AppSettingsStore | None = None
_store_lock = threading.Lock()


def get_app_settings_store() -> AppSettingsStore:
    global _store
    settings = get_settings()
    settings_path = Path(os.path.expanduser(settings.settings_file)).resolve()
    if _store is not None and _store.path == settings_path and _store.deployment_settings == settings:
        return _store
    with _store_lock:
        if _store is None or _store.path != settings_path or _store.deployment_settings != settings:
            _store = AppSettingsStore(settings.settings_file, settings)
    return _store


def reset_app_settings_store() -> None:
    global _store
    with _store_lock:
        _store = None
