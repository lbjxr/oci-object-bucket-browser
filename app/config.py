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
    )
