from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv


PRODUCTION_ENVS = frozenset({"prod", "production"})
DEFAULT_AUTH_PASSWORD = "change-me"
DEFAULT_SESSION_SECRET = "change-this-session-secret"
MIN_SESSION_SECRET_LENGTH = 32


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true/false")


def is_production_env(value: str) -> bool:
    return value.strip().lower() in PRODUCTION_ENVS


@dataclass(frozen=True)
class Settings:
    oci_config_path: str
    oci_profile: str
    namespace: str
    bucket_name: str
    compartment_id: str | None
    app_env: str = "development"
    session_https_only: bool = False
    region: str = ""
    prefix_root: str = ""
    settings_file: str = "./tmp/settings.json"
    trash_record_file: str = "./tmp/trash_records.json"
    share_file: str = "./tmp/shares.json"
    preview_text_limit: int = 20000
    max_list_limit: int = 200
    auth_username: str = "admin"
    auth_password: str = DEFAULT_AUTH_PASSWORD
    session_secret: str = DEFAULT_SESSION_SECRET
    session_cookie_name: str = "oci_bucket_browser_session"
    upload_chunk_size_mb: int = 16
    upload_single_put_threshold_mb: int = 32
    upload_parallelism: int = 6
    upload_session_dir: str = "./tmp/upload_sessions"
    upload_task_dir: str = "./tmp/upload_tasks"
    upload_temp_dir: str = "./tmp/upload_staging"
    upload_proxy_chunk_size_mb: int = 8
    upload_max_file_size_mb: int = 5120
    upload_max_chunk_size_mb: int = 64
    upload_max_staging_mb: int = 20480
    upload_max_active_tasks: int = 100
    upload_cleanup_enabled: bool = True
    upload_cleanup_startup_enabled: bool = True
    upload_cleanup_scheduler_enabled: bool = True
    upload_cleanup_interval_seconds: int = 3600
    upload_completed_task_visible_seconds: float = 86400.0
    upload_cleanup_completed_retention_hours: int = 24
    upload_cleanup_failed_retention_hours: int = 72
    upload_cleanup_stale_staging_retention_hours: int = 24


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    load_dotenv()
    namespace = os.getenv("OCI_NAMESPACE", "").strip()
    bucket_name = os.getenv("OCI_BUCKET_NAME", "").strip()
    compartment_id = os.getenv("OCI_COMPARTMENT_ID", "").strip() or None
    app_env = os.getenv("APP_ENV", "development").strip().lower() or "development"
    return Settings(
        oci_config_path=os.getenv("OCI_CONFIG_PATH", os.path.expanduser("~/.oci/config")),
        oci_profile=os.getenv("OCI_PROFILE", "DEFAULT"),
        namespace=namespace,
        bucket_name=bucket_name,
        compartment_id=compartment_id,
        app_env=app_env,
        session_https_only=_env_bool("APP_SESSION_HTTPS_ONLY", is_production_env(app_env)),
        region=os.getenv("OCI_REGION", "").strip(),
        prefix_root=os.getenv("OCI_PREFIX_ROOT", "").strip().strip("/"),
        settings_file=os.getenv("APP_SETTINGS_FILE", "./tmp/settings.json").strip() or "./tmp/settings.json",
        trash_record_file=os.getenv("APP_TRASH_RECORD_FILE", "./tmp/trash_records.json").strip() or "./tmp/trash_records.json",
        share_file=os.getenv("APP_SHARE_FILE", "./tmp/shares.json").strip() or "./tmp/shares.json",
        preview_text_limit=int(os.getenv("OCI_PREVIEW_TEXT_LIMIT", "20000")),
        max_list_limit=int(os.getenv("OCI_MAX_LIST_LIMIT", "200")),
        auth_username=os.getenv("APP_AUTH_USERNAME", "admin").strip() or "admin",
        auth_password=os.getenv("APP_AUTH_PASSWORD", DEFAULT_AUTH_PASSWORD),
        session_secret=os.getenv("APP_SESSION_SECRET", DEFAULT_SESSION_SECRET),
        session_cookie_name=os.getenv("APP_SESSION_COOKIE_NAME", "oci_bucket_browser_session").strip() or "oci_bucket_browser_session",
        upload_chunk_size_mb=max(8, int(os.getenv("APP_UPLOAD_CHUNK_SIZE_MB", "16"))),
        upload_single_put_threshold_mb=max(1, int(os.getenv("APP_UPLOAD_SINGLE_PUT_THRESHOLD_MB", "32"))),
        upload_parallelism=max(1, int(os.getenv("APP_UPLOAD_PARALLELISM", "6"))),
        upload_session_dir=os.getenv("APP_UPLOAD_SESSION_DIR", "./tmp/upload_sessions").strip() or "./tmp/upload_sessions",
        upload_task_dir=os.getenv("APP_UPLOAD_TASK_DIR", "./tmp/upload_tasks").strip() or "./tmp/upload_tasks",
        upload_temp_dir=os.getenv("APP_UPLOAD_TEMP_DIR", "./tmp/upload_staging").strip() or "./tmp/upload_staging",
        upload_proxy_chunk_size_mb=max(1, int(os.getenv("APP_UPLOAD_PROXY_CHUNK_SIZE_MB", "8"))),
        upload_max_file_size_mb=max(1, int(os.getenv("APP_UPLOAD_MAX_FILE_SIZE_MB", "5120"))),
        upload_max_chunk_size_mb=max(1, int(os.getenv("APP_UPLOAD_MAX_CHUNK_SIZE_MB", "64"))),
        upload_max_staging_mb=max(1, int(os.getenv("APP_UPLOAD_MAX_STAGING_MB", "20480"))),
        upload_max_active_tasks=max(1, int(os.getenv("APP_UPLOAD_MAX_ACTIVE_TASKS", "100"))),
        upload_cleanup_enabled=_env_bool("APP_UPLOAD_CLEANUP_ENABLED", True),
        upload_cleanup_startup_enabled=_env_bool("APP_UPLOAD_CLEANUP_STARTUP_ENABLED", True),
        upload_cleanup_scheduler_enabled=_env_bool("APP_UPLOAD_CLEANUP_SCHEDULER_ENABLED", True),
        upload_cleanup_interval_seconds=max(1, int(os.getenv("APP_UPLOAD_CLEANUP_INTERVAL_SECONDS", "3600"))),
        upload_completed_task_visible_seconds=max(0.0, float(os.getenv("APP_UPLOAD_COMPLETED_TASK_VISIBLE_SECONDS", "86400"))),
        upload_cleanup_completed_retention_hours=max(0, int(os.getenv("APP_UPLOAD_CLEANUP_COMPLETED_RETENTION_HOURS", "24"))),
        upload_cleanup_failed_retention_hours=max(0, int(os.getenv("APP_UPLOAD_CLEANUP_FAILED_RETENTION_HOURS", "72"))),
        upload_cleanup_stale_staging_retention_hours=max(1, int(os.getenv("APP_UPLOAD_CLEANUP_STALE_STAGING_RETENTION_HOURS", "24"))),
    )

def validate_startup_security(settings: Settings) -> None:
    if not is_production_env(settings.app_env):
        return

    errors: list[str] = []
    if not settings.auth_password or settings.auth_password == DEFAULT_AUTH_PASSWORD:
        errors.append("APP_AUTH_PASSWORD 未设置安全值")
    if not settings.session_secret or settings.session_secret == DEFAULT_SESSION_SECRET:
        errors.append("APP_SESSION_SECRET 未设置安全值")
    if len(settings.session_secret) < MIN_SESSION_SECRET_LENGTH:
        errors.append(f"APP_SESSION_SECRET 长度不能少于 {MIN_SESSION_SECRET_LENGTH} 个字符")
    if errors:
        raise RuntimeError("生产启动安全检查失败：" + "；".join(errors) + "。请设置环境变量后重启。")


def validate_single_instance_runtime(settings: Settings) -> None:
    if not is_production_env(settings.app_env):
        return

    worker_values = [
        os.getenv("APP_WORKERS"),
        os.getenv("WEB_CONCURRENCY"),
        os.getenv("UVICORN_WORKERS"),
        os.getenv("GUNICORN_WORKERS"),
    ]
    for value in worker_values:
        if not value or not value.strip():
            continue
        try:
            workers = int(value)
        except ValueError:
            continue
        if workers > 1:
            raise RuntimeError(
                "生产启动安全检查失败：本项目本地 JSON 状态只支持单实例单 Worker，"
                "请将 Worker 数量设为 1；多实例部署需要独立的共享状态架构。"
            )
