from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    oci_config_path: str
    oci_profile: str
    namespace: str
    bucket_name: str
    compartment_id: str | None
    preview_text_limit: int = 20000
    max_list_limit: int = 200
    auth_username: str = "admin"
    auth_password: str = "change-me"
    session_secret: str = "change-this-session-secret"
    session_cookie_name: str = "oci_bucket_browser_session"
    upload_chunk_size_mb: int = 16
    upload_single_put_threshold_mb: int = 32
    upload_parallelism: int = 6
    upload_session_dir: str = "./tmp/upload_sessions"
    upload_task_dir: str = "./tmp/upload_tasks"
    upload_temp_dir: str = "./tmp/upload_staging"
    upload_proxy_chunk_size_mb: int = 8
    upload_cleanup_enabled: bool = True
    upload_cleanup_startup_enabled: bool = True
    upload_cleanup_scheduler_enabled: bool = True
    upload_cleanup_interval_seconds: int = 3600
    upload_completed_task_visible_seconds: float = 1.0
    upload_cleanup_completed_retention_hours: int = 24
    upload_cleanup_failed_retention_hours: int = 72
    upload_cleanup_stale_staging_retention_hours: int = 24


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv()
    namespace = os.getenv("OCI_NAMESPACE", "").strip()
    bucket_name = os.getenv("OCI_BUCKET_NAME", "").strip()
    compartment_id = os.getenv("OCI_COMPARTMENT_ID", "").strip() or None
    return Settings(
        oci_config_path=os.getenv("OCI_CONFIG_PATH", os.path.expanduser("~/.oci/config")),
        oci_profile=os.getenv("OCI_PROFILE", "DEFAULT"),
        namespace=namespace,
        bucket_name=bucket_name,
        compartment_id=compartment_id,
        preview_text_limit=int(os.getenv("OCI_PREVIEW_TEXT_LIMIT", "20000")),
        max_list_limit=int(os.getenv("OCI_MAX_LIST_LIMIT", "200")),
        auth_username=os.getenv("APP_AUTH_USERNAME", "admin").strip() or "admin",
        auth_password=os.getenv("APP_AUTH_PASSWORD", "change-me"),
        session_secret=os.getenv("APP_SESSION_SECRET", "change-this-session-secret"),
        session_cookie_name=os.getenv("APP_SESSION_COOKIE_NAME", "oci_bucket_browser_session").strip() or "oci_bucket_browser_session",
        upload_chunk_size_mb=max(8, int(os.getenv("APP_UPLOAD_CHUNK_SIZE_MB", "16"))),
        upload_single_put_threshold_mb=max(1, int(os.getenv("APP_UPLOAD_SINGLE_PUT_THRESHOLD_MB", "32"))),
        upload_parallelism=max(1, int(os.getenv("APP_UPLOAD_PARALLELISM", "6"))),
        upload_session_dir=os.getenv("APP_UPLOAD_SESSION_DIR", "./tmp/upload_sessions").strip() or "./tmp/upload_sessions",
        upload_task_dir=os.getenv("APP_UPLOAD_TASK_DIR", "./tmp/upload_tasks").strip() or "./tmp/upload_tasks",
        upload_temp_dir=os.getenv("APP_UPLOAD_TEMP_DIR", "./tmp/upload_staging").strip() or "./tmp/upload_staging",
        upload_proxy_chunk_size_mb=max(1, int(os.getenv("APP_UPLOAD_PROXY_CHUNK_SIZE_MB", "8"))),
        upload_cleanup_enabled=os.getenv("APP_UPLOAD_CLEANUP_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"},
        upload_cleanup_startup_enabled=os.getenv("APP_UPLOAD_CLEANUP_STARTUP_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"},
        upload_cleanup_scheduler_enabled=os.getenv("APP_UPLOAD_CLEANUP_SCHEDULER_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"},
        upload_cleanup_interval_seconds=max(1, int(os.getenv("APP_UPLOAD_CLEANUP_INTERVAL_SECONDS", "3600"))),
        upload_completed_task_visible_seconds=max(0.0, float(os.getenv("APP_UPLOAD_COMPLETED_TASK_VISIBLE_SECONDS", "1.0"))),
        upload_cleanup_completed_retention_hours=max(0, int(os.getenv("APP_UPLOAD_CLEANUP_COMPLETED_RETENTION_HOURS", "24"))),
        upload_cleanup_failed_retention_hours=max(0, int(os.getenv("APP_UPLOAD_CLEANUP_FAILED_RETENTION_HOURS", "72"))),
        upload_cleanup_stale_staging_retention_hours=max(1, int(os.getenv("APP_UPLOAD_CLEANUP_STALE_STAGING_RETENTION_HOURS", "24"))),
    )
