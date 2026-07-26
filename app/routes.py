from __future__ import annotations

import csv
import hashlib
import json
import secrets
import tempfile
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from fastapi import APIRouter, Body, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.config import get_settings
from app.file_browser import FileBrowserQuery, classify_file_type, filter_and_paginate
from app.oci_client import OCIStorageError, OCIStorageService, classify_upload_exception
from app.settings_store import get_app_settings_store
from app.share_store import ShareAccessError, get_share_store, public_share, share_status, summarize_shares, utc_now
from app.storage_stats import summarize_objects
from app.temp_uploads import TempUploadSessionStore, UploadedChunk
from app.trash_store import RecycleBinService, get_trash_record_store
from app.upload_dashboard import summarize_upload_tasks
from app.upload_cleanup import run_upload_cleanup
from app.upload_sessions import UploadSession, UploadedPart, UploadSessionStore
from app.upload_tasks import get_upload_task_manager
from app.utils import is_image_type, is_pdf_type, is_text_type, object_name_from_upload, to_data_url
from app.webdav import basic_auth_matches, build_multistatus, href_for, map_path, parse_destination, relative_from_key

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def build_upload_error_payload(*, part_num: int, exc: OCIStorageError) -> dict[str, object]:
    payload = {
        "ok": False,
        "part_num": part_num,
        "detail": str(exc),
        "error_code": exc.category,
        "retryable": exc.retryable,
        "reason": exc.reason,
    }
    if exc.retry_after_seconds is not None:
        payload["retry_after_seconds"] = exc.retry_after_seconds
    return payload


def format_size_display(size: int | None) -> str:
    if size is None:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{int(value)} {units[unit_index]}"
    precision = 0 if value >= 10 else 1
    return f"{value:.{precision}f} {units[unit_index]}"


def format_exact_size(size: int | None) -> str:
    if size is None:
        return ""
    return f"{size:,} B"


def format_time_to_seconds(value: str | None) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value[:19].replace("T", " ")


def file_icon_for(content_type: str | None) -> str:
    if not content_type:
        return "📄"
    if is_image_type(content_type):
        return "🖼️"
    if is_pdf_type(content_type):
        return "📕"
    if is_text_type(content_type):
        return "📝"
    if "zip" in content_type or "compressed" in content_type:
        return "🗜️"
    if content_type.startswith("video/"):
        return "🎬"
    if content_type.startswith("audio/"):
        return "🎵"
    return "📄"


def file_type_label_for(content_type: str | None) -> str:
    if not content_type:
        return "未知类型"
    if is_image_type(content_type):
        return "图片"
    if is_pdf_type(content_type):
        return "PDF"
    if is_text_type(content_type):
        return "文本"
    if "zip" in content_type or "compressed" in content_type:
        return "压缩包"
    if content_type.startswith("video/"):
        return "视频"
    if content_type.startswith("audio/"):
        return "音频"
    if content_type.startswith("application/"):
        return "应用文件"
    return "文件"


def enrich_objects(objects):
    for obj in objects:
        setattr(obj, "size_mb", format_size_display(obj.size))
        setattr(obj, "size_exact", format_exact_size(obj.size))
        setattr(obj, "time_display", format_time_to_seconds(obj.time_created))
        setattr(obj, "is_image", is_image_type(obj.content_type or ""))
        setattr(obj, "file_icon", file_icon_for(obj.content_type))
        setattr(obj, "file_type_label", file_type_label_for(obj.content_type))
        setattr(obj, "file_type", classify_file_type(obj.content_type, obj.name))
    return objects


def get_storage() -> OCIStorageService:
    return OCIStorageService(get_app_settings_store().effective_settings())


def get_upload_store() -> UploadSessionStore:
    settings = get_settings()
    return UploadSessionStore(settings.upload_session_dir)


def get_temp_upload_store() -> TempUploadSessionStore:
    settings = get_settings()
    return TempUploadSessionStore(settings.upload_temp_dir)


def require_login(request: Request) -> None:
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401, detail="未登录")


def require_write_access(request: Request) -> None:
    require_login(request)
    if get_app_settings_store().read_only_enabled():
        raise HTTPException(status_code=403, detail="只读模式已开启，当前操作会写入对象存储，已阻止")


def redirect_to_login(next_path: str = "/") -> RedirectResponse:
    return RedirectResponse(url=f"/login?next={quote(next_path, safe='/?:=&')}", status_code=303)


def template_context(request: Request, **extra: object) -> dict[str, object]:
    settings_store = get_app_settings_store()
    settings = settings_store.effective_settings()
    return {
        "request": request,
        "app_title": "OCI Object Bucket Browser",
        "is_authenticated": bool(request.session.get("authenticated")),
        "auth_username": settings.auth_username,
        "namespace": settings.namespace,
        "bucket_name": settings.bucket_name,
        "active_path": request.url.path,
        "upload_chunk_size_mb": settings.upload_chunk_size_mb,
        "upload_single_put_threshold_mb": settings.upload_single_put_threshold_mb,
        "upload_parallelism": settings.upload_parallelism,
        "upload_proxy_chunk_size_mb": settings.upload_proxy_chunk_size_mb,
        "trash_enabled": settings_store.trash_enabled(),
        "batch_delete_confirmation_required": settings_store.batch_delete_confirmation_required(),
        **extra,
    }


def build_upload_fingerprint(*, object_name: str, file_size: int, chunk_size: int, file_fingerprint: str) -> str:
    payload = f"{object_name}|{file_size}|{chunk_size}|{file_fingerprint}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _expected_part_size(session: UploadSession, part_num: int) -> int:
    expected_parts = (session.total_size + session.chunk_size - 1) // session.chunk_size
    if expected_parts <= 0:
        return 0
    if part_num < expected_parts:
        return session.chunk_size
    if part_num == expected_parts:
        tail = session.total_size - session.chunk_size * (expected_parts - 1)
        return max(0, tail)
    return 0


async def reconcile_multipart_session_with_remote(store, storage, session: UploadSession) -> tuple[UploadSession, bool]:
    if session.strategy == "single-put" or session.completed or not session.multipart_upload_id:
        return session, False

    remote_parts = await run_in_threadpool(
        storage.list_multipart_uploaded_parts,
        object_name=session.object_name,
        multipart_upload_id=session.multipart_upload_id,
    )
    expected_parts = (session.total_size + session.chunk_size - 1) // session.chunk_size
    filtered_remote_parts = {
        part_num: etag
        for part_num, etag in remote_parts.items()
        if 1 <= part_num <= expected_parts and etag
    }

    local_parts = session.uploaded_parts
    changed = len(filtered_remote_parts) != len(local_parts)
    if not changed:
        for part_num, part in local_parts.items():
            if filtered_remote_parts.get(part_num) != part.etag:
                changed = True
                break

    if not changed:
        return session, False

    def mutator(s: UploadSession) -> None:
        s.uploaded_parts = {
            part_num: UploadedPart(
                part_num=part_num,
                etag=etag,
                size=_expected_part_size(s, part_num),
            )
            for part_num, etag in sorted(filtered_remote_parts.items())
        }

    updated = store.update(session.upload_id, mutator)
    return updated, True


async def try_reconcile_multipart_session_with_remote(store, storage, session: UploadSession) -> tuple[UploadSession, bool, bool, str | None]:
    try:
        updated, reconciled = await reconcile_multipart_session_with_remote(store, storage, session)
        return updated, reconciled, False, None
    except Exception as exc:
        warning = (
            "本次未完成 OCI 远端分片对账，已按本地上传会话状态继续恢复。"
            f"为安全起见，最终合并前仍会再次校验。异常信息：{exc}"
        )
        return session, False, True, warning


class UploadInitRequest(BaseModel):
    filename: str
    file_size: int
    content_type: str | None = None
    file_fingerprint: str | None = None


class BatchDeleteRequest(BaseModel):
    object_names: list[str]
    confirmation_count: int | None = None


class BatchDownloadRequest(BaseModel):
    object_names: list[str]


class CreateShareRequest(BaseModel):
    object_key: str
    expires_in_hours: int = 24
    password: str = ""
    download_limit: int | None = None


class ServerProxyUploadInitRequest(BaseModel):
    filename: str
    file_size: int
    content_type: str | None = None
    file_fingerprint: str | None = None
    overwrite: bool = False


class ServerProxyCommitRequest(BaseModel):
    filename: str
    file_size: int
    content_type: str | None = None
    overwrite: bool = False


class SingleRangeRequest(BaseModel):
    start: int
    end: int


class CreateFolderRequest(BaseModel):
    prefix: str = ""
    folder_name: str
    overwrite: bool = False


class RenamePathRequest(BaseModel):
    source_path: str
    new_name: str
    overwrite: bool = False


class DeletePathRequest(BaseModel):
    path: str


class ConflictResponse(BaseModel):
    detail: str
    conflict: dict[str, object]
    overwrite_allowed: bool = False
    requires_overwrite: bool = True


@dataclass
class FolderEntry:
    name: str
    full_prefix: str
    item_count: int
    placeholder_exists: bool = False


def _normalize_object_names(object_names: list[str]) -> list[str]:
    normalized = []
    seen = set()
    for raw_name in object_names:
        name = (raw_name or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        normalized.append(name)
    return normalized


def _normalize_prefix(prefix: str | None) -> str:
    normalized = PurePosixPath("/" + (prefix or "").strip()).as_posix().lstrip("/")
    if normalized in {"", "."}:
        return ""
    return normalized.rstrip("/") + "/"


def _normalize_path(path: str | None) -> str:
    raw = (path or "").strip()
    keep_trailing_slash = raw.endswith("/")
    normalized = PurePosixPath("/" + raw).as_posix().lstrip("/")
    if normalized in {"", "."}:
        return ""
    normalized = normalized.rstrip("/")
    if keep_trailing_slash:
        return normalized + "/"
    return normalized


def _join_prefix(prefix: str, name: str) -> str:
    clean_name = (name or "").strip().strip("/")
    if not clean_name:
        return prefix
    return f"{prefix}{clean_name}"


def _ensure_folder_object_name(path: str) -> str:
    normalized = _normalize_path(path)
    if not normalized:
        raise HTTPException(status_code=400, detail="目录路径不能为空")
    return normalized.rstrip("/") + "/"


def _split_directory_entries(prefix: str, objects) -> tuple[list[FolderEntry], list[object]]:
    current_prefix = _normalize_prefix(prefix)
    folders: dict[str, FolderEntry] = {}
    files = []
    for obj in objects:
        name = getattr(obj, "name", "") or ""
        if current_prefix and not name.startswith(current_prefix):
            continue
        remainder = name[len(current_prefix):] if current_prefix else name
        if not remainder:
            continue
        if remainder.endswith("/") and remainder.count("/") == 1:
            folder_name = remainder[:-1]
            if not folder_name:
                continue
            entry = folders.get(folder_name)
            if entry is None:
                entry = FolderEntry(name=folder_name, full_prefix=f"{current_prefix}{folder_name}/", item_count=0, placeholder_exists=True)
                folders[folder_name] = entry
            else:
                entry.placeholder_exists = True
            continue
        if "/" in remainder:
            folder_name = remainder.split("/", 1)[0]
            entry = folders.get(folder_name)
            if entry is None:
                entry = FolderEntry(name=folder_name, full_prefix=f"{current_prefix}{folder_name}/", item_count=1)
                folders[folder_name] = entry
            else:
                entry.item_count += 1
            continue
        files.append(obj)

    return sorted(folders.values(), key=lambda item: item.name.lower()), files


def _parent_prefix(prefix: str) -> str:
    current = _normalize_prefix(prefix)
    if not current:
        return ""
    parts = current.rstrip("/").split("/")
    if len(parts) <= 1:
        return ""
    return "/".join(parts[:-1]) + "/"


def _parent_prefix_for_path(path: str) -> str:
    normalized = _normalize_path(path)
    if not normalized:
        return ""
    if normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    parts = normalized.split("/")
    if len(parts) <= 1:
        return ""
    return "/".join(parts[:-1]) + "/"


def _build_prefix_breadcrumbs(prefix: str) -> list[dict[str, str | bool]]:
    normalized = _normalize_prefix(prefix)
    breadcrumbs: list[dict[str, str | bool]] = [
        {"name": "Bucket 根目录", "prefix": "", "is_current": normalized == ""}
    ]
    if not normalized:
        return breadcrumbs

    current = ""
    parts = [part for part in normalized.rstrip("/").split("/") if part]
    for index, part in enumerate(parts):
        current = f"{current}{part}/"
        breadcrumbs.append(
            {
                "name": part,
                "prefix": current,
                "is_current": index == len(parts) - 1,
            }
        )
    return breadcrumbs


def _build_batch_download_filename(prefix: str, object_count: int) -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    prefix_label = (prefix or "").strip().strip("/")
    if prefix_label:
        prefix_label = prefix_label.replace("/", "-").replace(" ", "-")[:48]
        return f"oci-batch-{prefix_label}-{object_count}items-{timestamp}.zip"
    return f"oci-batch-{object_count}items-{timestamp}.zip"


def _copy_object_via_app(storage: OCIStorageService, *, source_name: str, destination_name: str) -> None:
    stream, content_type, _ = storage.open_stream(source_name)
    with closing(stream):
        storage.upload_file(destination_name, stream, content_type)


def _list_objects_for_prefix(storage: OCIStorageService, prefix: str) -> list[object]:
    return storage.list_objects(prefix=_normalize_prefix(prefix))


def _build_storage_stats(prefix: str = "") -> dict:
    normalized_prefix = _normalize_prefix(prefix)
    storage = get_storage()
    list_all = getattr(storage, "list_objects_all", None)
    objects = list_all(prefix=normalized_prefix) if callable(list_all) else storage.list_objects(prefix=normalized_prefix)
    payload = summarize_objects(objects, prefix=normalized_prefix)
    payload["refreshed_at"] = datetime.now(timezone.utc).isoformat()
    return payload


def _object_exists(storage: OCIStorageService, object_name: str) -> bool:
    normalized = _normalize_path(object_name)
    if not normalized:
        return False
    parent = _parent_prefix_for_path(normalized)
    return any(getattr(obj, "name", None) == normalized for obj in storage.list_objects(prefix=parent or ""))


def _prefix_has_objects(storage: OCIStorageService, prefix: str) -> bool:
    normalized = _normalize_prefix(prefix)
    if not normalized:
        return False
    return any(True for _ in storage.list_objects(prefix=normalized))


def _conflict_response(*, action: str, kind: str, source_path: str | None, destination_path: str, conflict_reason: str, existing_paths: list[str]) -> JSONResponse:
    payload = {
        "detail": conflict_reason,
        "conflict": {
            "action": action,
            "kind": kind,
            "source_path": source_path,
            "destination_path": destination_path,
            "reason": conflict_reason,
            "existing_paths": existing_paths,
        },
        "overwrite_allowed": True,
        "requires_overwrite": True,
    }
    return JSONResponse(payload, status_code=409)


def _ensure_no_upload_conflict(storage: OCIStorageService, *, object_name: str, overwrite: bool) -> JSONResponse | None:
    if overwrite:
        return None
    if _object_exists(storage, object_name):
        return _conflict_response(
            action="upload",
            kind="file",
            source_path=None,
            destination_path=object_name,
            conflict_reason="当前目录已存在同名对象，默认不会直接覆盖。",
            existing_paths=[object_name],
        )
    return None


def _ensure_no_folder_conflict(storage: OCIStorageService, *, folder_object_name: str, overwrite: bool) -> JSONResponse | None:
    if overwrite:
        return None
    existing_paths: list[str] = []
    if _object_exists(storage, folder_object_name):
        existing_paths.append(folder_object_name)
    if _prefix_has_objects(storage, folder_object_name):
        if folder_object_name not in existing_paths:
            existing_paths.append(folder_object_name)
    if existing_paths:
        return _conflict_response(
            action="create_folder",
            kind="folder",
            source_path=None,
            destination_path=folder_object_name,
            conflict_reason="当前目录已存在同名目录或同名前缀内容，默认不会继续创建。",
            existing_paths=existing_paths,
        )
    return None


def _ensure_no_rename_conflict(storage: OCIStorageService, *, source_path: str, destination_path: str, is_folder: bool, overwrite: bool) -> JSONResponse | None:
    if overwrite:
        return None
    normalized_source = _normalize_path(source_path)
    normalized_destination = _normalize_prefix(destination_path) if is_folder else _normalize_path(destination_path)
    if normalized_source == normalized_destination:
        return None
    existing_paths: list[str] = []
    if is_folder:
        destination_prefix = _normalize_prefix(destination_path)
        if _prefix_has_objects(storage, destination_prefix) or _object_exists(storage, destination_prefix):
            existing_paths.append(destination_prefix)
        if existing_paths:
            return _conflict_response(
                action="rename",
                kind="folder",
                source_path=normalized_source,
                destination_path=destination_prefix,
                conflict_reason="目标目录前缀已存在对象，默认不会直接覆盖整个目录。",
                existing_paths=existing_paths,
            )
        return None

    if _object_exists(storage, normalized_destination):
        existing_paths.append(normalized_destination)
    if existing_paths:
        return _conflict_response(
            action="rename",
            kind="file",
            source_path=normalized_source,
            destination_path=normalized_destination,
            conflict_reason="目标文件已存在，默认不会直接覆盖。",
            existing_paths=existing_paths,
        )
    return None


def _rename_single_object(storage: OCIStorageService, *, source_name: str, destination_name: str) -> None:
    _copy_object_via_app(storage, source_name=source_name, destination_name=destination_name)
    storage.delete_object(source_name)


def _rename_prefix(storage: OCIStorageService, *, source_prefix: str, destination_prefix: str) -> dict[str, int]:
    source_prefix = _normalize_prefix(source_prefix)
    destination_prefix = _normalize_prefix(destination_prefix)
    if not source_prefix or not destination_prefix:
        raise HTTPException(status_code=400, detail="目录重命名路径无效")
    objects = _list_objects_for_prefix(storage, source_prefix)
    if not objects:
        raise HTTPException(status_code=404, detail="目录不存在或目录下没有对象")

    moved = 0
    for obj in objects:
        source_name = obj.name
        destination_name = destination_prefix + source_name[len(source_prefix):]
        _rename_single_object(storage, source_name=source_name, destination_name=destination_name)
        moved += 1
    return {"moved_count": moved}


def _delete_object_with_policy(storage: OCIStorageService, *, object_name: str, deleted_by: str) -> dict[str, object]:
    if not get_app_settings_store().trash_enabled():
        storage.delete_object(object_name)
        return {"object_name": object_name, "recycled": False, "trash_record": None}

    record = RecycleBinService(storage, get_trash_record_store()).recycle(object_name, deleted_by=deleted_by)
    return {
        "object_name": object_name,
        "recycled": True,
        "trash_record": record,
    }


def _delete_prefix(storage: OCIStorageService, *, path_prefix: str, deleted_by: str) -> dict[str, object]:
    normalized_prefix = _normalize_prefix(path_prefix)
    if not normalized_prefix:
        raise HTTPException(status_code=400, detail="不允许删除根目录")
    objects = _list_objects_for_prefix(storage, normalized_prefix)
    if not objects:
        raise HTTPException(status_code=404, detail="目录不存在或目录下没有对象")
    deleted = 0
    recycled = 0
    for obj in objects:
        result = _delete_object_with_policy(storage, object_name=obj.name, deleted_by=deleted_by)
        deleted += 1
        recycled += int(bool(result["recycled"]))
    return {"deleted_count": deleted, "recycled_count": recycled}


def _content_disposition_attachment(filename: str) -> str:
    ascii_fallback = filename.encode("ascii", errors="ignore").decode("ascii") or "download.bin"
    quoted = quote(filename)
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quoted}"


def _share_is_authorized(request: Request, share_id: str) -> bool:
    authorized = request.session.get("authorized_shares") or []
    return share_id in authorized


def _remember_share_authorization(request: Request, share_id: str) -> None:
    authorized = [item for item in (request.session.get("authorized_shares") or []) if isinstance(item, str)]
    if share_id not in authorized:
        authorized.append(share_id)
    request.session["authorized_shares"] = authorized[-20:]


def _public_share_response(
    request: Request,
    *,
    token: str,
    record: dict | None,
    error: str | None = None,
    status_code: int = 200,
):
    share = public_share(record) if record is not None else None
    password_required = bool(share and share["password_protected"] and not _share_is_authorized(request, share["id"]))
    return templates.TemplateResponse(
        request,
        "share_public.html",
        template_context(
            request,
            force_public=True,
            share=share,
            share_token=token,
            password_required=password_required,
            share_error=error,
        ),
        status_code=status_code,
    )


def _webdav_authenticate(request: Request) -> None:
    enabled, configured_username, password_hash = get_app_settings_store().webdav_credentials()
    if not enabled:
        raise HTTPException(status_code=404, detail="WebDAV 未启用")
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("basic "):
        raise HTTPException(
            status_code=401,
            detail="需要 WebDAV Basic Auth",
            headers={"WWW-Authenticate": 'Basic realm="OCI Object Bucket Browser"'},
        )
    if basic_auth_matches(header, username=configured_username, password_hash=password_hash):
        return
    raise HTTPException(
        status_code=401,
        detail="WebDAV 用户名或密码错误",
        headers={"WWW-Authenticate": 'Basic realm="OCI Object Bucket Browser"'},
    )


def _webdav_require_write() -> None:
    if get_app_settings_store().read_only_enabled():
        raise HTTPException(status_code=403, detail="只读模式已开启，WebDAV 写操作已阻止")


def _webdav_path(path: str | None) -> tuple[str, str, bool]:
    prefix_root = get_app_settings_store().effective_settings().prefix_root
    try:
        mapped = map_path(path, prefix_root=prefix_root)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return mapped.key, mapped.relative, mapped.collection_hint


def _webdav_relative_from_key(key: str) -> str:
    return relative_from_key(key, prefix_root=get_app_settings_store().effective_settings().prefix_root)


def _webdav_href(relative: str, *, collection: bool) -> str:
    return href_for(relative, collection=collection)


def _webdav_propfind(storage, *, path: str, depth: int) -> Response:
    key, relative, collection_hint = _webdav_path(path)
    root_key = _normalize_prefix(key)
    exact_object = bool(key) and _object_exists(storage, key.rstrip("/"))
    prefix_exists = bool(root_key) and _prefix_has_objects(storage, root_key)
    is_collection = collection_hint or prefix_exists
    if key and not exact_object and not prefix_exists:
        raise HTTPException(status_code=404, detail="WebDAV 资源不存在")

    resources: list[dict[str, object]] = []
    if not is_collection:
        info = storage.head_object(key)
        resources.append(
            {
                "href": _webdav_href(relative, collection=False),
                "collection": False,
                "name": relative.rsplit("/", 1)[-1],
                "size": info.size,
                "content_type": info.content_type,
                "etag": info.etag,
            }
        )
    else:
        collection_prefix = root_key
        listed = storage.list_objects(prefix=collection_prefix)
        resources.append(
            {
                "href": _webdav_href(relative, collection=True),
                "collection": True,
                "name": relative.rsplit("/", 1)[-1] if relative else "Bucket",
            }
        )
        if depth > 0:
            folders, files = _split_directory_entries(collection_prefix, listed)
            for folder in folders:
                child_relative = _webdav_relative_from_key(folder.full_prefix)
                resources.append(
                    {
                        "href": _webdav_href(child_relative, collection=True),
                        "collection": True,
                        "name": folder.name,
                    }
                )
            for obj in files:
                child_relative = _webdav_relative_from_key(obj.name)
                resources.append(
                    {
                        "href": _webdav_href(child_relative, collection=False),
                        "collection": False,
                        "name": child_relative.rsplit("/", 1)[-1],
                        "size": getattr(obj, "size", None),
                        "content_type": getattr(obj, "content_type", None),
                        "etag": getattr(obj, "etag", None),
                        "modified": getattr(obj, "time_created", None),
                    }
                )

    body = build_multistatus(resources)
    return Response(
        content=body,
        status_code=207,
        media_type="application/xml; charset=utf-8",
        headers={"DAV": "1, 2", "Content-Length": str(len(body))},
    )


def _webdav_destination(request: Request, destination: str) -> tuple[str, str, bool]:
    try:
        mapped = parse_destination(
            destination,
            current_netloc=request.url.netloc,
            prefix_root=get_app_settings_store().effective_settings().prefix_root,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return mapped.key, mapped.relative, mapped.collection_hint


@router.api_route("/webdav", methods=["OPTIONS", "PROPFIND", "GET", "PUT", "DELETE", "MKCOL", "MOVE"])
@router.api_route("/webdav/{path:path}", methods=["OPTIONS", "PROPFIND", "GET", "PUT", "DELETE", "MKCOL", "MOVE"])
async def webdav_endpoint(request: Request, path: str = ""):
    _webdav_authenticate(request)
    method = request.method.upper()
    if method == "OPTIONS":
        return Response(
            status_code=200,
            headers={
                "Allow": "OPTIONS, PROPFIND, GET, PUT, DELETE, MKCOL, MOVE",
                "DAV": "1, 2",
                "MS-Author-Via": "DAV",
            },
        )

    storage = get_storage()
    if method == "PROPFIND":
        depth_header = request.headers.get("depth", "1").strip().lower()
        depth = 0 if depth_header == "0" else 1
        return _webdav_propfind(storage, path=path, depth=depth)

    key, relative, collection_hint = _webdav_path(path)
    if method == "GET":
        if collection_hint or not key or not _object_exists(storage, key):
            raise HTTPException(status_code=404, detail="WebDAV 文件不存在")
        try:
            stream, content_type, upstream_headers = storage.open_stream(key)
        except OCIStorageError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        headers = {"Content-Disposition": _content_disposition_attachment(relative.rsplit("/", 1)[-1])}
        if upstream_headers.get("content-length"):
            headers["Content-Length"] = upstream_headers["content-length"]
        if upstream_headers.get("etag"):
            headers["ETag"] = upstream_headers["etag"]
        return StreamingResponse(stream, media_type=content_type, headers=headers)

    _webdav_require_write()
    if method == "PUT":
        if not key or collection_hint:
            raise HTTPException(status_code=400, detail="PUT 目标必须是文件路径")
        existed = _object_exists(storage, key)
        body = await request.body()
        try:
            storage.upload_file(key, BytesIO(body), request.headers.get("content-type"))
        except OCIStorageError as exc:
            raise HTTPException(status_code=500, detail=f"WebDAV PUT 失败：{exc}") from exc
        return Response(status_code=204 if existed else 201, headers={"Content-Length": "0"})

    if method == "DELETE":
        if not key:
            raise HTTPException(status_code=400, detail="不允许删除 WebDAV 根目录")
        if collection_hint or _prefix_has_objects(storage, _normalize_prefix(key)):
            _delete_prefix(storage, path_prefix=key, deleted_by="webdav")
        elif _object_exists(storage, key):
            _delete_object_with_policy(storage, object_name=key, deleted_by="webdav")
        else:
            raise HTTPException(status_code=404, detail="WebDAV 资源不存在")
        return Response(status_code=204, headers={"Content-Length": "0"})

    if method == "MKCOL":
        if not key:
            raise HTTPException(status_code=400, detail="MKCOL 目标必须是目录路径")
        folder_object = _ensure_folder_object_name(key)
        if _object_exists(storage, folder_object) or _prefix_has_objects(storage, folder_object):
            raise HTTPException(status_code=405, detail="WebDAV 目录已存在")
        try:
            storage.upload_file(folder_object, BytesIO(b""), "application/x-directory")
        except OCIStorageError as exc:
            raise HTTPException(status_code=500, detail=f"WebDAV MKCOL 失败：{exc}") from exc
        return Response(status_code=201, headers={"Content-Length": "0"})

    if method == "MOVE":
        destination = request.headers.get("destination", "").strip()
        if not destination:
            raise HTTPException(status_code=400, detail="MOVE 缺少 Destination 头")
        destination_key, destination_relative, destination_collection_hint = _webdav_destination(request, destination)
        if not key or key == destination_key:
            raise HTTPException(status_code=403, detail="MOVE 源和目标不能相同")
        source_collection = collection_hint or _prefix_has_objects(storage, _normalize_prefix(key))
        overwrite = request.headers.get("overwrite", "T").strip().upper() != "F"
        destination_exists = (
            _object_exists(storage, destination_key.rstrip("/"))
            or _prefix_has_objects(storage, _normalize_prefix(destination_key))
        )
        if destination_exists and not overwrite:
            raise HTTPException(status_code=412, detail="MOVE 目标已存在且不允许覆盖")
        if destination_exists and overwrite:
            if _prefix_has_objects(storage, _normalize_prefix(destination_key)):
                _delete_prefix(storage, path_prefix=destination_key, deleted_by="webdav")
            elif _object_exists(storage, destination_key.rstrip("/")):
                _delete_object_with_policy(storage, object_name=destination_key.rstrip("/"), deleted_by="webdav")
        if source_collection:
            result = _rename_prefix(storage, source_prefix=key, destination_prefix=destination_key)
        else:
            if not _object_exists(storage, key):
                raise HTTPException(status_code=404, detail="MOVE 源不存在")
            _rename_single_object(storage, source_name=key, destination_name=destination_key.rstrip("/"))
            result = {"moved_count": 1}
        return Response(status_code=204 if destination_exists else 201, headers={"Content-Length": "0"})

    raise HTTPException(status_code=405, detail="不支持的 WebDAV 方法")


def _parse_single_range_header(range_header: str | None, *, total_size: int) -> SingleRangeRequest | None:
    if not range_header:
        return None
    value = range_header.strip()
    if not value:
        return None
    if not value.startswith("bytes="):
        raise HTTPException(status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE, detail="仅支持 bytes Range")

    ranges = [item.strip() for item in value[6:].split(",") if item.strip()]
    if len(ranges) != 1:
        raise HTTPException(status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE, detail="当前仅支持单段 Range 请求")

    raw_range = ranges[0]
    if "-" not in raw_range:
        raise HTTPException(status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE, detail="非法的 Range 请求")
    start_text, end_text = raw_range.split("-", 1)

    if start_text == "":
        if not end_text.isdigit():
            raise HTTPException(status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE, detail="非法的 Range 请求")
        suffix_length = int(end_text)
        if suffix_length <= 0:
            raise HTTPException(status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE, detail="非法的 Range 请求")
        if total_size <= 0:
            raise HTTPException(status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE, detail="对象为空，无法执行 Range 下载")
        start = max(total_size - suffix_length, 0)
        end = total_size - 1
        return SingleRangeRequest(start=start, end=end)

    if not start_text.isdigit():
        raise HTTPException(status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE, detail="非法的 Range 请求")

    start = int(start_text)
    if start >= total_size:
        raise HTTPException(status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE, detail="Range 起点超出对象大小")

    if end_text == "":
        end = total_size - 1
    else:
        if not end_text.isdigit():
            raise HTTPException(status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE, detail="非法的 Range 请求")
        end = int(end_text)

    if end < start:
        raise HTTPException(status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE, detail="Range 结束位置早于起点")

    end = min(end, total_size - 1)
    return SingleRangeRequest(start=start, end=end)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    if request.session.get("authenticated"):
        return RedirectResponse(url=next or "/", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        template_context(request, error=None, next_path=next or "/"),
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, username: str = Form(...), password: str = Form(...), next_path: str = Form("/")):
    settings = get_settings()
    valid_user = secrets.compare_digest(username, settings.auth_username)
    valid_pass = secrets.compare_digest(password, settings.auth_password)
    if not (valid_user and valid_pass):
        return templates.TemplateResponse(
            request,
            "login.html",
            template_context(request, error="用户名或密码错误", next_path=next_path or "/"),
            status_code=401,
        )
    request.session["authenticated"] = True
    request.session["username"] = settings.auth_username
    return RedirectResponse(url=next_path or "/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    prefix: str = "",
    query: str = "",
    file_type: str = "all",
    size_min: float | None = Query(default=None, ge=0),
    size_max: float | None = Query(default=None, ge=0),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=10, le=100),
):
    if not request.session.get("authenticated"):
        return redirect_to_login(request.url.path + (f"?prefix={quote(prefix)}" if prefix else ""))
    normalized_prefix = _normalize_prefix(prefix)
    breadcrumbs = _build_prefix_breadcrumbs(normalized_prefix)
    current_directory_label = normalized_prefix or "/"
    filters = FileBrowserQuery(
        query=query,
        file_type=file_type,
        size_min_mb=size_min,
        size_max_mb=size_max,
        page=page,
        page_size=page_size,
    )
    try:
        listed_objects = _list_objects_for_prefix(get_storage(), normalized_prefix)
        folders, files = _split_directory_entries(normalized_prefix, enrich_objects(listed_objects))
        file_page = filter_and_paginate(folders, files, filters)
        return templates.TemplateResponse(
            request,
            "index.html",
            template_context(
                request,
                objects=file_page.files,
                folders=file_page.folders,
                file_page=file_page,
                filters=filters,
                prefix=normalized_prefix,
                current_prefix=normalized_prefix,
                current_directory_label=current_directory_label,
                breadcrumbs=breadcrumbs,
                parent_prefix=_parent_prefix(normalized_prefix),
                upload_proxy_chunk_size_mb=get_settings().upload_proxy_chunk_size_mb,
                error=None,
            ),
        )
    except OCIStorageError as exc:
        file_page = filter_and_paginate([], [], filters)
        return templates.TemplateResponse(
            request,
            "index.html",
            template_context(
                request,
                objects=[],
                folders=[],
                file_page=file_page,
                filters=filters,
                prefix=normalized_prefix,
                current_prefix=normalized_prefix,
                current_directory_label=current_directory_label,
                breadcrumbs=breadcrumbs,
                parent_prefix=_parent_prefix(normalized_prefix),
                upload_proxy_chunk_size_mb=get_settings().upload_proxy_chunk_size_mb,
                error=str(exc),
            ),
            status_code=500,
        )


@router.get("/uploads", response_class=HTMLResponse)
def uploads_page(request: Request):
    if not request.session.get("authenticated"):
        return redirect_to_login("/uploads")
    tasks = get_upload_task_manager().task_store.list_recent(limit=100)
    summary = summarize_upload_tasks(tasks)
    return templates.TemplateResponse(
        request,
        "uploads.html",
        template_context(
            request,
            tasks=tasks,
            task_summary=summary,
        ),
    )


@router.get("/shares", response_class=HTMLResponse)
def shares_page(request: Request):
    if not request.session.get("authenticated"):
        return redirect_to_login("/shares")
    records = get_share_store().list_records()
    return templates.TemplateResponse(
        request,
        "shares.html",
        template_context(
            request,
            share_summary=summarize_shares(records),
        ),
    )


@router.get("/api/shares")
def list_shares_api(request: Request):
    require_login(request)
    records = sorted(
        get_share_store().list_records(),
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    )
    return {
        "ok": True,
        "summary": summarize_shares(records),
        "shares": [public_share(record) for record in records],
    }


@router.post("/api/shares")
def create_share_api(request: Request, payload: CreateShareRequest = Body(...)):
    require_login(request)
    object_key = _normalize_path(payload.object_key)
    if not object_key or object_key.endswith("/"):
        raise HTTPException(status_code=400, detail="分享对象路径必须指向一个文件")
    if not 1 <= payload.expires_in_hours <= 8760:
        raise HTTPException(status_code=422, detail="有效期必须在 1-8760 小时之间")
    if payload.download_limit is not None and not 1 <= payload.download_limit <= 1_000_000:
        raise HTTPException(status_code=422, detail="下载次数限制必须在 1-1000000 之间")
    password = payload.password.strip()
    if password and len(password) < 4:
        raise HTTPException(status_code=422, detail="分享密码至少需要 4 个字符")
    if len(password) > 256:
        raise HTTPException(status_code=422, detail="分享密码不能超过 256 个字符")

    try:
        get_storage().head_object(object_key)
    except OCIStorageError as exc:
        raise HTTPException(status_code=404, detail=f"无法创建分享：{exc}") from exc

    now = utc_now()
    record, token = get_share_store().create(
        object_key=object_key,
        expires_at=now + timedelta(hours=payload.expires_in_hours),
        download_limit=payload.download_limit,
        password=password,
        now=now,
    )
    share_url = f"{str(request.base_url).rstrip('/')}/s/{quote(token, safe='')}"
    return {
        "ok": True,
        "message": "分享链接已创建。出于安全考虑，该完整链接只返回这一次。",
        "share": public_share(record, now=now),
        "share_url": share_url,
    }


@router.delete("/api/shares/{share_id}")
def revoke_share_api(request: Request, share_id: str):
    require_login(request)
    try:
        record = get_share_store().revoke(share_id)
    except ShareAccessError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {
        "ok": True,
        "message": "分享链接已撤销",
        "share": public_share(record),
    }


@router.get("/api/shares-export")
def export_shares_api(request: Request):
    require_login(request)
    records = sorted(
        get_share_store().list_records(),
        key=lambda item: str(item.get("created_at") or ""),
        reverse=True,
    )
    buffer = StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["ID", "对象路径", "状态", "创建时间", "过期时间", "访问次数", "下载次数", "下载限制", "密码保护"])
    for record in records:
        item = public_share(record)
        writer.writerow(
            [
                item["id"],
                item["object_key"],
                item["status"],
                item["created_at"],
                item["expires_at"],
                item["access_count"],
                item["download_count"],
                item["download_limit"] if item["download_limit"] is not None else "",
                "是" if item["password_protected"] else "否",
            ]
        )
    filename = f"oci-shares-{datetime.now().strftime('%Y%m%d-%H%M%S')}.csv"
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": _content_disposition_attachment(filename)},
    )


@router.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request, prefix: str = ""):
    if not request.session.get("authenticated"):
        return redirect_to_login("/stats")
    try:
        summary = _build_storage_stats(prefix)
        error = None
    except OCIStorageError as exc:
        summary = summarize_objects([], prefix=_normalize_prefix(prefix))
        summary["refreshed_at"] = None
        error = str(exc)
    return templates.TemplateResponse(
        request,
        "stats.html",
        template_context(request, storage_summary=summary, stats_error=error),
        status_code=500 if error else 200,
    )


@router.get("/api/stats/summary")
def storage_stats_api(request: Request, prefix: str = ""):
    require_login(request)
    try:
        summary = _build_storage_stats(prefix)
    except OCIStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "summary": summary}


@router.post("/api/stats/refresh")
def refresh_storage_stats_api(request: Request, prefix: str = ""):
    require_login(request)
    try:
        summary = _build_storage_stats(prefix)
    except OCIStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "message": "存储统计已刷新", "summary": summary}


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    if not request.session.get("authenticated"):
        return redirect_to_login("/settings")
    return templates.TemplateResponse(
        request,
        "settings.html",
        template_context(request),
    )


@router.get("/s/{token}", response_class=HTMLResponse)
def public_share_page(request: Request, token: str):
    store = get_share_store()
    record = store.get_by_token(token)
    if record is None:
        return _public_share_response(
            request,
            token=token,
            record=None,
            error="分享链接不存在或地址不完整",
            status_code=404,
        )
    status_value = share_status(record)
    if status_value != "active":
        message = {
            "revoked": "该分享链接已撤销",
            "expired": "该分享链接已过期",
            "exhausted": "该分享链接的下载次数已用完",
        }.get(status_value, "该分享链接当前不可用")
        return _public_share_response(
            request,
            token=token,
            record=record,
            error=message,
            status_code=410,
        )
    try:
        record = store.record_access(record["id"])
    except ShareAccessError as exc:
        return _public_share_response(
            request,
            token=token,
            record=record,
            error=str(exc),
            status_code=exc.status_code,
        )
    return _public_share_response(request, token=token, record=record)


@router.post("/s/{token}/verify-password", response_class=HTMLResponse)
def verify_public_share_password(request: Request, token: str, password: str = Form(...)):
    store = get_share_store()
    try:
        record = store.require_active(token)
    except ShareAccessError as exc:
        record = store.get_by_token(token)
        return _public_share_response(
            request,
            token=token,
            record=record,
            error=str(exc),
            status_code=exc.status_code,
        )
    if not store.password_matches(record, password):
        return _public_share_response(
            request,
            token=token,
            record=record,
            error="分享密码错误",
            status_code=401,
        )
    _remember_share_authorization(request, record["id"])
    return RedirectResponse(url=f"/s/{quote(token, safe='')}", status_code=303)


@router.get("/s/{token}/download")
def download_public_share(request: Request, token: str):
    store = get_share_store()
    try:
        record = store.require_active(token)
    except ShareAccessError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    if record.get("password_hash") and not _share_is_authorized(request, record["id"]):
        return RedirectResponse(url=f"/s/{quote(token, safe='')}", status_code=303)

    try:
        stream, content_type, upstream_headers = get_storage().open_stream(record["object_key"])
    except OCIStorageError as exc:
        raise HTTPException(status_code=404, detail=f"分享对象读取失败：{exc}") from exc
    try:
        store.reserve_download(record["id"])
    except ShareAccessError as exc:
        stream.close()
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception:
        stream.close()
        raise

    filename = str(record["object_key"]).split("/")[-1] or "download.bin"
    headers = {
        "Content-Disposition": _content_disposition_attachment(filename),
        "Cache-Control": "private, no-store",
    }
    if upstream_headers.get("content-length"):
        headers["Content-Length"] = upstream_headers["content-length"]
    if upstream_headers.get("etag"):
        headers["ETag"] = upstream_headers["etag"]
    return StreamingResponse(stream, media_type=content_type, headers=headers)


@router.get("/api/settings")
def get_app_settings_api(request: Request):
    require_login(request)
    return {"ok": True, "settings": get_app_settings_store().public_snapshot()}


@router.post("/api/settings/validate")
def validate_app_settings_api(request: Request, payload: dict = Body(...)):
    require_login(request)
    try:
        normalized = get_app_settings_store().validate_update(payload)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    normalized["webdav"]["password"] = "" if not normalized["webdav"]["password"] else "configured"
    return {"ok": True, "message": "设置格式有效", "normalized": normalized}


@router.post("/api/settings")
def save_app_settings_api(request: Request, payload: dict = Body(...)):
    require_login(request)
    try:
        saved = get_app_settings_store().update(payload)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "ok": True,
        "message": "设置已保存。只读模式立即生效，存储与上传参数在服务重启后应用。",
        "restart_required": True,
        "settings": saved,
    }


@router.post("/upload")
async def upload(request: Request, file: UploadFile = File(...), overwrite: bool = Form(False)):
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
    if not request.session.get("authenticated"):
        if is_ajax:
            return JSONResponse({"detail": "未登录"}, status_code=401)
        return redirect_to_login(request.url.path)
    require_write_access(request)
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")
    object_name = object_name_from_upload(file.filename)
    storage = get_storage()
    conflict = _ensure_no_upload_conflict(storage, object_name=object_name, overwrite=overwrite)
    if conflict is not None:
        await file.close()
        return conflict
    try:
        await run_in_threadpool(storage.upload_file, object_name, file.file, file.content_type)
    except OCIStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"上传过程中发生异常: {exc}") from exc
    finally:
        await file.close()
    if is_ajax:
        return JSONResponse(
            {
                "ok": True,
                "strategy": "single-put",
                "object_name": object_name,
                "message": f"上传成功：{object_name}",
                "overwritten": overwrite,
            }
        )
    return RedirectResponse(url="/", status_code=303)


@router.post("/api/server-uploads/init")
async def init_server_upload(request: Request, payload: ServerProxyUploadInitRequest = Body(...)):
    require_write_access(request)
    settings = get_settings()
    if not payload.filename.strip():
        raise HTTPException(status_code=400, detail="缺少文件名")
    if payload.file_size <= 0:
        raise HTTPException(status_code=400, detail="文件大小必须大于 0")

    object_name = object_name_from_upload(payload.filename)
    storage = get_storage()
    conflict = _ensure_no_upload_conflict(storage, object_name=object_name, overwrite=payload.overwrite)
    if conflict is not None:
        return conflict
    threshold = settings.upload_single_put_threshold_mb * 1024 * 1024
    strategy = "single-put-server-proxy" if payload.file_size <= threshold else "oci-multipart-server-proxy"
    chunk_size = settings.upload_proxy_chunk_size_mb * 1024 * 1024
    file_fingerprint = (payload.file_fingerprint or f"{payload.filename}:{payload.file_size}:{payload.content_type or ''}").strip()
    temp_store = get_temp_upload_store()
    existing = temp_store.find_active_by_fingerprint(file_fingerprint)
    if existing:
        return {
            "ok": True,
            "reused": True,
            "object_name": existing.object_name,
            "strategy": existing.strategy,
            "proxy_chunk_size": existing.chunk_size,
            "temp_upload_id": existing.temp_upload_id,
            "upload_url": f"/api/server-uploads/staging/{quote(existing.temp_upload_id, safe='')}",
            "uploaded_chunks": existing.uploaded_chunk_indexes,
            "missing_chunks": existing.missing_chunk_indexes,
            "uploaded_bytes": existing.uploaded_bytes,
            "total_chunks": existing.total_chunks,
            "staged_size": Path(existing.staged_path).stat().st_size if Path(existing.staged_path).exists() else 0,
            "message": "已恢复服务器暂存上传会话",
        }

    temp_upload_id = secrets.token_hex(8)
    temp_dir = Path(settings.upload_temp_dir).resolve()
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = temp_dir / f"{temp_upload_id}-{Path(payload.filename).name or 'upload.bin'}"
    temp_path.write_bytes(b"")
    session = temp_store.create(
        temp_upload_id=temp_upload_id,
        filename=payload.filename,
        object_name=object_name,
        content_type=payload.content_type or "application/octet-stream",
        total_size=payload.file_size,
        chunk_size=chunk_size,
        strategy=strategy,
        file_fingerprint=file_fingerprint,
        staged_path=str(temp_path),
    )

    return {
        "ok": True,
        "reused": False,
        "object_name": object_name,
        "strategy": strategy,
        "proxy_chunk_size": chunk_size,
        "temp_upload_id": temp_upload_id,
        "upload_url": f"/api/server-uploads/staging/{quote(temp_upload_id, safe='')}",
        "uploaded_chunks": session.uploaded_chunk_indexes,
        "missing_chunks": session.missing_chunk_indexes,
        "uploaded_bytes": session.uploaded_bytes,
        "total_chunks": session.total_chunks,
        "staged_size": 0,
        "message": "已初始化服务器中转上传",
    }


@router.get("/api/server-uploads/staging/{temp_upload_id}")
async def get_server_upload_staging_status(request: Request, temp_upload_id: str):
    require_login(request)
    session = get_temp_upload_store().get(temp_upload_id)
    if not session:
        raise HTTPException(status_code=404, detail="临时上传不存在")
    staged_path = Path(session.staged_path)
    return {
        "ok": True,
        "temp_upload_id": session.temp_upload_id,
        "filename": session.filename,
        "object_name": session.object_name,
        "content_type": session.content_type,
        "strategy": session.strategy,
        "total_size": session.total_size,
        "chunk_size": session.chunk_size,
        "total_chunks": session.total_chunks,
        "uploaded_chunks": session.uploaded_chunk_indexes,
        "missing_chunks": session.missing_chunk_indexes,
        "uploaded_bytes": session.uploaded_bytes,
        "staged_size": staged_path.stat().st_size if staged_path.exists() else 0,
        "committed": session.committed,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


@router.put("/api/server-uploads/staging/{temp_upload_id}")
async def stage_server_upload_chunk(
    request: Request,
    temp_upload_id: str,
    chunk_index: int = Query(..., ge=0),
    chunk_sha256: str | None = Query(default=None),
    body: bytes = Body(...),
):
    require_write_access(request)
    temp_store = get_temp_upload_store()
    session = temp_store.get(temp_upload_id)
    if not session:
        raise HTTPException(status_code=404, detail="临时上传不存在")
    if session.committed:
        raise HTTPException(status_code=409, detail="临时上传已提交，不能继续写入")

    chunk_size = session.chunk_size
    total_chunks = session.total_chunks
    if chunk_index >= total_chunks:
        raise HTTPException(status_code=400, detail="chunk_index 超出范围")

    expected_size = chunk_size if chunk_index < total_chunks - 1 else session.total_size - chunk_size * (total_chunks - 1)
    if len(body) != expected_size:
        raise HTTPException(status_code=400, detail=f"chunk 大小不匹配，期望 {expected_size}，实际 {len(body)}")

    body_sha256 = hashlib.sha256(body).hexdigest()
    if chunk_sha256 and chunk_sha256.lower() != body_sha256:
        raise HTTPException(status_code=400, detail="chunk 校验失败：sha256 不匹配")

    existing = session.uploaded_chunks.get(chunk_index)
    if existing:
        if existing.size == len(body) and existing.sha256 == body_sha256:
            staged_path = Path(session.staged_path)
            return {
                "ok": True,
                "chunk_index": chunk_index,
                "stored_bytes": len(body),
                "staged_size": staged_path.stat().st_size if staged_path.exists() else session.uploaded_bytes,
                "already_uploaded": True,
                "uploaded_chunks": session.uploaded_chunk_indexes,
                "missing_chunks": session.missing_chunk_indexes,
            }
        raise HTTPException(status_code=409, detail="该 chunk 已存在且内容不一致，请确认是否选择了同一文件")

    staged_path = Path(session.staged_path)
    staged_path.parent.mkdir(parents=True, exist_ok=True)
    if not staged_path.exists():
        staged_path.write_bytes(b"")
    offset = chunk_index * chunk_size
    with open(staged_path, "r+b") as fileobj:
        fileobj.seek(offset)
        fileobj.write(body)

    updated = temp_store.update(
        temp_upload_id,
        lambda s: s.uploaded_chunks.__setitem__(
            chunk_index,
            UploadedChunk(chunk_index=chunk_index, size=len(body), sha256=body_sha256),
        ),
    )
    current_size = staged_path.stat().st_size
    return {
        "ok": True,
        "chunk_index": chunk_index,
        "stored_bytes": len(body),
        "staged_size": current_size,
        "already_uploaded": False,
        "uploaded_chunks": updated.uploaded_chunk_indexes,
        "missing_chunks": updated.missing_chunk_indexes,
    }


@router.post("/api/server-uploads/commit")
async def commit_server_upload(request: Request, payload: ServerProxyCommitRequest = Body(...), temp_upload_id: str = Query(...)):
    require_write_access(request)
    temp_store = get_temp_upload_store()
    session = temp_store.get(temp_upload_id)
    if not session:
        raise HTTPException(status_code=404, detail="临时上传不存在")
    staged_path = Path(session.staged_path)
    if not staged_path.exists():
        raise HTTPException(status_code=404, detail="暂存文件不存在")
    if payload.file_size != session.total_size:
        raise HTTPException(status_code=400, detail=f"文件大小不匹配，期望 {session.total_size}，实际 {payload.file_size}")
    if payload.filename != session.filename:
        raise HTTPException(status_code=400, detail="文件名不匹配，无法提交")
    if session.missing_chunk_indexes:
        raise HTTPException(status_code=400, detail=f"仍有 chunk 未上传完成: {session.missing_chunk_indexes[:20]}")
    actual_size = staged_path.stat().st_size
    if actual_size != payload.file_size:
        raise HTTPException(status_code=400, detail=f"暂存文件大小不匹配，期望 {payload.file_size}，实际 {actual_size}")
    storage = get_storage()
    conflict = _ensure_no_upload_conflict(storage, object_name=session.object_name, overwrite=payload.overwrite)
    if conflict is not None:
        return conflict
    try:
        temp_store.update(
            temp_upload_id,
            lambda current: (
                (_ for _ in ()).throw(HTTPException(status_code=409, detail="临时上传已提交，请勿重复创建后台任务"))
                if current.committed
                else setattr(current, "committed", True)
            ),
        )
    except HTTPException:
        raise
    manager = get_upload_task_manager()
    try:
        task = await run_in_threadpool(
            manager.create_task_from_staged_file,
            filename=payload.filename,
            content_type=payload.content_type or session.content_type,
            staged_path=str(staged_path),
            total_size=payload.file_size,
        )
    except Exception:
        temp_store.update(temp_upload_id, lambda current: setattr(current, "committed", False))
        raise
    return {
        "ok": True,
        "task_id": task.task_id,
        "object_name": task.object_name,
        "strategy": task.strategy,
        "status": task.status,
        "phase": task.phase,
        "message": "文件已上传到服务器，后台入桶任务已创建",
    }


@router.get("/api/server-uploads/tasks")
async def list_server_upload_tasks(request: Request, limit: int = Query(default=20, ge=1, le=100)):
    require_login(request)
    tasks = get_upload_task_manager().task_store.list_recent(limit=limit)
    return {
        "ok": True,
        "summary": summarize_upload_tasks(tasks).to_dict(),
        "tasks": [
            {
                **task.to_api_dict(),
                "progress": 100 if task.total_size <= 0 else round(task.uploaded_bytes * 100 / task.total_size, 1),
            }
            for task in tasks
        ],
    }


@router.get("/api/server-uploads/tasks/{task_id}")
async def get_server_upload_task(request: Request, task_id: str):
    require_login(request)
    task = get_upload_task_manager().task_store.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="上传任务不存在")
    return {
        "ok": True,
        **task.to_api_dict(),
        "progress": 100 if task.total_size <= 0 else round(task.uploaded_bytes * 100 / task.total_size, 1),
    }


@router.delete("/api/server-uploads/tasks/{task_id}")
async def cancel_server_upload_task(request: Request, task_id: str):
    require_login(request)
    task = get_upload_task_manager().cancel(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="上传任务不存在")
    return {"ok": True, "task_id": task_id, "status": task.status, "message": "已请求取消上传任务"}


@router.post("/api/server-uploads/tasks/{task_id}/cancel")
async def cancel_server_upload_task_post(request: Request, task_id: str):
    return await cancel_server_upload_task(request, task_id)


@router.post("/api/server-uploads/tasks/{task_id}/retry")
async def retry_server_upload_task(request: Request, task_id: str):
    require_write_access(request)
    try:
        task = get_upload_task_manager().retry(task_id)
    except (ValueError, FileNotFoundError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not task:
        raise HTTPException(status_code=404, detail="上传任务不存在")
    return {"ok": True, "task_id": task_id, "status": task.status, "message": "失败任务已重新排队"}


@router.post("/api/server-uploads/tasks/cleanup-completed")
async def clear_completed_server_upload_tasks(request: Request):
    require_login(request)
    deleted_count = get_upload_task_manager().clear_completed()
    return {
        "ok": True,
        "deleted_count": deleted_count,
        "message": f"已清理 {deleted_count} 个完成任务",
    }


@router.post("/api/server-uploads/cleanup")
async def run_server_upload_cleanup(request: Request):
    require_login(request)
    manager = get_upload_task_manager()
    result = await run_in_threadpool(run_upload_cleanup, settings=get_settings(), manager=manager)
    return {
        "ok": True,
        "message": "已执行上传临时文件清理",
        **result.to_dict(),
    }


@router.post("/api/uploads/init")
async def init_upload(request: Request, payload: UploadInitRequest = Body(...)):
    require_write_access(request)
    settings = get_settings()
    if not payload.filename.strip():
        raise HTTPException(status_code=400, detail="缺少文件名")
    if payload.file_size <= 0:
        raise HTTPException(status_code=400, detail="文件大小必须大于 0")

    object_name = object_name_from_upload(payload.filename)
    content_type = payload.content_type or "application/octet-stream"
    chunk_size = settings.upload_chunk_size_mb * 1024 * 1024
    threshold = settings.upload_single_put_threshold_mb * 1024 * 1024
    file_fingerprint = (payload.file_fingerprint or f"{payload.filename}:{payload.file_size}").strip()
    strategy = "single-put" if payload.file_size <= threshold else "oci-multipart-browser-chunked"
    fingerprint = build_upload_fingerprint(
        object_name=object_name,
        file_size=payload.file_size,
        chunk_size=chunk_size,
        file_fingerprint=file_fingerprint,
    )

    store = get_upload_store()
    storage = get_storage()
    existing = store.find_active_by_fingerprint(fingerprint)
    if existing and existing.strategy == strategy:
        reconciled = False
        degraded_to_local_state = False
        reconcile_warning = None
        if strategy != "single-put":
            existing, reconciled, degraded_to_local_state, reconcile_warning = await try_reconcile_multipart_session_with_remote(store, storage, existing)
        return {
            "ok": True,
            "reused": True,
            "upload_id": existing.upload_id,
            "object_name": existing.object_name,
            "content_type": existing.content_type,
            "strategy": existing.strategy,
            "chunk_size": existing.chunk_size,
            "parallelism": existing.parallelism,
            "uploaded_parts": existing.uploaded_part_numbers,
            "uploaded_bytes": existing.uploaded_bytes,
            "reconciled_with_remote": reconciled,
            "remote_reconcile_degraded": degraded_to_local_state,
            "remote_reconcile_warning": reconcile_warning,
            "message": (
                "已恢复上传会话，并按 OCI 远端分片状态完成对账"
                if reconciled
                else "已恢复之前未完成的上传会话"
            ),
        }

    multipart_upload_id = None
    if strategy != "single-put":
        multipart_upload_id = await run_in_threadpool(storage.create_multipart_upload, object_name, content_type)

    session = store.create(
        object_name=object_name,
        content_type=content_type,
        total_size=payload.file_size,
        chunk_size=chunk_size,
        parallelism=settings.upload_parallelism,
        strategy=strategy,
        fingerprint=fingerprint,
        multipart_upload_id=multipart_upload_id,
    )
    return {
        "ok": True,
        "reused": False,
        "upload_id": session.upload_id,
        "object_name": session.object_name,
        "content_type": session.content_type,
        "strategy": session.strategy,
        "chunk_size": session.chunk_size,
        "parallelism": session.parallelism,
        "uploaded_parts": session.uploaded_part_numbers,
        "uploaded_bytes": session.uploaded_bytes,
        "reconciled_with_remote": False,
        "message": "已创建上传会话",
    }


@router.put("/api/uploads/{upload_id}/part/{part_num}")
async def upload_part(request: Request, response: Response, upload_id: str, part_num: int, body: bytes = Body(...)):
    require_write_access(request)
    if part_num <= 0:
        raise HTTPException(status_code=400, detail="part_num 必须从 1 开始")

    store = get_upload_store()
    session = store.get(upload_id)
    if not session:
        raise HTTPException(status_code=404, detail="上传会话不存在")
    if session.completed:
        raise HTTPException(status_code=409, detail="上传会话已完成")

    if session.strategy == "single-put":
        raise HTTPException(status_code=400, detail="当前上传会话不支持分片")
    if not session.multipart_upload_id:
        raise HTTPException(status_code=500, detail="缺少 OCI multipart upload id")

    existing = session.uploaded_parts.get(part_num)
    if existing and existing.size == len(body):
        return {
            "ok": True,
            "upload_id": upload_id,
            "part_num": part_num,
            "etag": existing.etag,
            "already_uploaded": True,
        }

    try:
        etag = await run_in_threadpool(
            get_storage().upload_part,
            object_name=session.object_name,
            multipart_upload_id=session.multipart_upload_id,
            part_num=part_num,
            payload=body,
            content_type=session.content_type,
        )
    except OCIStorageError as exc:
        response.status_code = exc.status_code
        return build_upload_error_payload(part_num=part_num, exc=exc)
    except Exception as exc:
        category, retryable, status_code, reason, retry_after_seconds = classify_upload_exception(exc)
        wrapped = OCIStorageError(
            f"上传分片失败（part {part_num}，{'可重试' if retryable else '不可重试'}，{category}）: {reason}",
            category=category,
            retryable=retryable,
            status_code=status_code,
            reason=reason,
            retry_after_seconds=retry_after_seconds,
        )
        response.status_code = wrapped.status_code
        return build_upload_error_payload(part_num=part_num, exc=wrapped)

    try:
        store.update(
            upload_id,
            lambda s: s.uploaded_parts.__setitem__(part_num, UploadedPart(part_num=part_num, etag=etag, size=len(body))),
        )
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="上传会话不存在")
    return {
        "ok": True,
        "upload_id": upload_id,
        "part_num": part_num,
        "etag": etag,
        "already_uploaded": False,
    }


@router.get("/api/uploads/{upload_id}")
async def get_upload_status(request: Request, upload_id: str):
    require_login(request)
    store = get_upload_store()
    session = store.get(upload_id)
    if not session:
        raise HTTPException(status_code=404, detail="上传会话不存在")
    reconciled = False
    degraded_to_local_state = False
    reconcile_warning = None
    if session.strategy != "single-put" and not session.completed:
        session, reconciled, degraded_to_local_state, reconcile_warning = await try_reconcile_multipart_session_with_remote(store, get_storage(), session)
    return {
        "ok": True,
        "upload_id": session.upload_id,
        "object_name": session.object_name,
        "content_type": session.content_type,
        "strategy": session.strategy,
        "total_size": session.total_size,
        "chunk_size": session.chunk_size,
        "parallelism": session.parallelism,
        "uploaded_parts": session.uploaded_part_numbers,
        "uploaded_bytes": session.uploaded_bytes,
        "completed": session.completed,
        "multipart_upload_id": session.multipart_upload_id,
        "reconciled_with_remote": reconciled,
        "remote_reconcile_degraded": degraded_to_local_state,
        "remote_reconcile_warning": reconcile_warning,
    }


@router.post("/api/uploads/{upload_id}/complete")
async def complete_upload(request: Request, upload_id: str):
    require_write_access(request)
    store = get_upload_store()
    session = store.get(upload_id)
    if not session:
        raise HTTPException(status_code=404, detail="上传会话不存在")
    if session.completed:
        return {
            "ok": True,
            "upload_id": session.upload_id,
            "object_name": session.object_name,
            "strategy": session.strategy,
            "message": f"上传完成：{session.object_name}",
        }

    storage = get_storage()
    if session.strategy == "single-put":
        raise HTTPException(status_code=400, detail="single-put 上传无需调用 complete 接口")

    session, _ = await reconcile_multipart_session_with_remote(store, storage, session)
    expected_parts = (session.total_size + session.chunk_size - 1) // session.chunk_size
    missing = [part_num for part_num in range(1, expected_parts + 1) if part_num not in session.uploaded_parts]
    if missing:
        raise HTTPException(status_code=400, detail=f"仍有分片未上传完成: {missing[:10]}")

    await run_in_threadpool(
        storage.commit_multipart_upload,
        object_name=session.object_name,
        multipart_upload_id=session.multipart_upload_id or "",
        parts=[(part_num, session.uploaded_parts[part_num].etag) for part_num in session.uploaded_part_numbers],
    )
    session.completed = True
    store.save(session)
    return {
        "ok": True,
        "upload_id": session.upload_id,
        "object_name": session.object_name,
        "strategy": session.strategy,
        "message": f"上传完成：{session.object_name}，所有分片已合并。",
    }


@router.delete("/api/uploads/{upload_id}")
async def cancel_upload(request: Request, upload_id: str):
    require_login(request)
    store = get_upload_store()
    session = store.get(upload_id)
    if not session:
        raise HTTPException(status_code=404, detail="上传会话不存在")
    if session.multipart_upload_id and not session.completed:
        await run_in_threadpool(
            get_storage().abort_multipart_upload,
            object_name=session.object_name,
            multipart_upload_id=session.multipart_upload_id,
        )
    store.delete(upload_id)
    return {"ok": True, "message": "上传会话已取消"}


@router.get("/download/{object_name:path}")
def download(request: Request, object_name: str):
    if not request.session.get("authenticated"):
        return redirect_to_login(request.url.path)

    storage = get_storage()
    filename = object_name.split("/")[-1] or "download.bin"

    try:
        object_info = storage.head_object(object_name)
    except OCIStorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    total_size = object_info.size or 0
    requested_range = _parse_single_range_header(request.headers.get("range"), total_size=total_size) if object_info.size is not None else None
    range_header = None
    response_headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": _content_disposition_attachment(filename),
    }
    status_code = status.HTTP_200_OK

    if object_info.etag:
        response_headers["ETag"] = object_info.etag

    if requested_range is not None:
        range_header = f"bytes={requested_range.start}-{requested_range.end}"
        response_headers["Content-Range"] = f"bytes {requested_range.start}-{requested_range.end}/{total_size}"
        response_headers["Content-Length"] = str(requested_range.end - requested_range.start + 1)
        status_code = status.HTTP_206_PARTIAL_CONTENT
    elif object_info.size is not None:
        response_headers["Content-Length"] = str(object_info.size)

    try:
        stream, content_type, upstream_headers = storage.open_stream(object_name, range_header=range_header)
    except OCIStorageError as exc:
        detail = str(exc)
        if requested_range is not None and ("Range Not Satisfiable" in detail or "range" in detail.lower()):
            raise HTTPException(
                status_code=status.HTTP_416_REQUESTED_RANGE_NOT_SATISFIABLE,
                detail=detail,
                headers={"Content-Range": f"bytes */{total_size}"},
            ) from exc
        raise HTTPException(status_code=404, detail=detail) from exc

    if status_code == status.HTTP_200_OK:
        upstream_length = upstream_headers.get("content-length")
        if upstream_length:
            response_headers["Content-Length"] = upstream_length
    if object_info.size is not None and "Content-Range" not in response_headers:
        response_headers.setdefault("Content-Range", f"bytes 0-{max(total_size - 1, 0)}/{total_size}")

    return StreamingResponse(stream, media_type=content_type, headers=response_headers, status_code=status_code)


@router.post("/objects/batch-download")
async def batch_download_objects(request: Request, prefix: str = Query(default="")):
    if not request.session.get("authenticated"):
        return JSONResponse({"detail": "未登录"}, status_code=401)

    content_type = (request.headers.get("content-type") or "").lower()
    effective_prefix = prefix
    raw_object_names: list[str] = []

    if "application/json" in content_type:
        try:
            raw_payload = await request.json()
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"批量下载请求体无效：{exc}") from exc
        try:
            payload = BatchDownloadRequest.model_validate(raw_payload)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"批量下载请求体无效：{exc}") from exc
        raw_object_names = payload.object_names
    else:
        form = await request.form()
        effective_prefix = str(form.get("prefix") or prefix)
        raw_object_names = [str(name) for name in form.getlist("object_names")]

    object_names = _normalize_object_names(raw_object_names)
    if not object_names:
        raise HTTPException(status_code=400, detail="至少要选择一个对象")

    storage = get_storage()
    temp_file = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    failed: list[dict[str, str]] = []
    archived_count = 0
    try:
        with zipfile.ZipFile(temp_file, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            manifest = {
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "requested_count": len(object_names),
                "archived_count": 0,
                "failed_count": 0,
                "failed": failed,
            }
            for object_name in object_names:
                try:
                    stream, _content_type, _headers = storage.open_stream(object_name)
                except OCIStorageError as exc:
                    failed.append({"object_name": object_name, "detail": str(exc)})
                    continue
                except Exception as exc:
                    failed.append({"object_name": object_name, "detail": f"异常信息：{exc}"})
                    continue

                with stream:
                    archive.writestr(object_name, stream.read())
                    archived_count += 1

            manifest["archived_count"] = archived_count
            manifest["failed_count"] = len(failed)
            if failed:
                archive.writestr(
                    "_batch_download_failures.json",
                    json.dumps(manifest, ensure_ascii=False, indent=2),
                )
                failure_lines = [
                    "以下对象未能打进本次 ZIP，其他成功对象已正常导出：",
                    "",
                ]
                for item in failed:
                    failure_lines.append(f"- {item['object_name']}: {item['detail']}")
                archive.writestr("_batch_download_failures.txt", "\n".join(failure_lines))

        if archived_count == 0:
            raise HTTPException(status_code=500, detail="批量下载失败：所有对象都未能成功读取，未生成可用 ZIP。")

        temp_file.seek(0)
        filename = _build_batch_download_filename(prefix=effective_prefix, object_count=len(object_names))
        headers = {
            "Content-Disposition": _content_disposition_attachment(filename),
            "X-Batch-Requested-Count": str(len(object_names)),
            "X-Batch-Archived-Count": str(archived_count),
            "X-Batch-Failed-Count": str(len(failed)),
            "X-Batch-Partial": "1" if failed else "0",
        }
        return StreamingResponse(temp_file, media_type="application/zip", headers=headers)
    except HTTPException:
        temp_file.close()
        raise
    except Exception as exc:
        temp_file.close()
        raise HTTPException(status_code=500, detail=f"批量下载打包失败：{exc}") from exc


@router.get("/api/files")
def list_files_api(
    request: Request,
    prefix: str = "",
    query: str = "",
    file_type: str = "all",
    size_min: float | None = Query(default=None, ge=0),
    size_max: float | None = Query(default=None, ge=0),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=25, ge=10, le=100),
):
    require_login(request)
    normalized_prefix = _normalize_prefix(prefix)
    try:
        listed_objects = _list_objects_for_prefix(get_storage(), normalized_prefix)
        folders, files = _split_directory_entries(normalized_prefix, enrich_objects(listed_objects))
        file_page = filter_and_paginate(
            folders,
            files,
            FileBrowserQuery(
                query=query,
                file_type=file_type,
                size_min_mb=size_min,
                size_max_mb=size_max,
                page=page,
                page_size=page_size,
            ),
        )
    except OCIStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "ok": True,
        "prefix": normalized_prefix,
        "current_directory_label": normalized_prefix or "/",
        "parent_prefix": _parent_prefix(normalized_prefix),
        "breadcrumbs": _build_prefix_breadcrumbs(normalized_prefix),
        "pagination": {
            "page": file_page.page,
            "page_size": file_page.page_size,
            "total_items": file_page.total_items,
            "total_pages": file_page.total_pages,
            "start_item": file_page.start_item,
            "end_item": file_page.end_item,
            "has_previous": file_page.has_previous,
            "has_next": file_page.has_next,
        },
        "filters": {
            "query": query,
            "file_type": file_type,
            "size_min": size_min,
            "size_max": size_max,
        },
        "folders": [
            {
                "name": folder.name,
                "full_prefix": folder.full_prefix,
                "item_count": folder.item_count,
                "placeholder_exists": folder.placeholder_exists,
            }
            for folder in file_page.folders
        ],
        "files": [
            {
                "name": obj.name,
                "size": obj.size,
                "size_display": getattr(obj, "size_mb", format_size_display(obj.size)),
                "time_created": obj.time_created,
                "time_display": getattr(obj, "time_display", format_time_to_seconds(obj.time_created)),
                "content_type": obj.content_type,
                "file_type_label": getattr(obj, "file_type_label", file_type_label_for(obj.content_type)),
                "file_type": getattr(obj, "file_type", classify_file_type(obj.content_type, obj.name)),
                "etag": obj.etag,
            }
            for obj in file_page.files
        ],
    }


@router.post("/api/files/folders")
def create_folder(request: Request, payload: CreateFolderRequest = Body(...)):
    require_write_access(request)
    folder_name = (payload.folder_name or "").strip().strip("/")
    if not folder_name:
        raise HTTPException(status_code=400, detail="目录名不能为空")
    if "/" in folder_name:
        raise HTTPException(status_code=400, detail="目录名不能包含 /，请在当前目录下创建")

    prefix = _normalize_prefix(payload.prefix)
    folder_object_name = _ensure_folder_object_name(_join_prefix(prefix, folder_name))
    storage = get_storage()
    conflict = _ensure_no_folder_conflict(storage, folder_object_name=folder_object_name, overwrite=payload.overwrite)
    if conflict is not None:
        return conflict
    try:
        storage.upload_file(folder_object_name, BytesIO(b""), "application/x-directory")
    except OCIStorageError as exc:
        raise HTTPException(status_code=500, detail=f"创建目录失败：{exc}") from exc

    return {
        "ok": True,
        "path": folder_object_name,
        "message": f"已创建目录：{folder_object_name}",
        "overwritten": payload.overwrite,
    }


@router.post("/api/files/rename")
def rename_path(request: Request, payload: RenamePathRequest = Body(...)):
    require_write_access(request)
    source_path = _normalize_path(payload.source_path)
    new_name = (payload.new_name or "").strip().strip("/")
    if not source_path:
        raise HTTPException(status_code=400, detail="源路径不能为空")
    if not new_name:
        raise HTTPException(status_code=400, detail="新名称不能为空")
    if "/" in new_name:
        raise HTTPException(status_code=400, detail="新名称不能包含 /")

    parent_prefix = _parent_prefix_for_path(source_path)
    destination_path = _join_prefix(parent_prefix, new_name)
    storage = get_storage()

    try:
        if source_path.endswith("/"):
            normalized_destination = f"{destination_path}/"
            conflict = _ensure_no_rename_conflict(
                storage,
                source_path=source_path,
                destination_path=normalized_destination,
                is_folder=True,
                overwrite=payload.overwrite,
            )
            if conflict is not None:
                return conflict
            result = _rename_prefix(storage, source_prefix=source_path, destination_prefix=normalized_destination)
            return {
                "ok": True,
                "kind": "folder",
                "source_path": source_path,
                "destination_path": normalized_destination,
                "moved_count": result["moved_count"],
                "message": f"目录已重命名为：{normalized_destination}",
                "overwritten": payload.overwrite,
            }
        conflict = _ensure_no_rename_conflict(
            storage,
            source_path=source_path,
            destination_path=destination_path,
            is_folder=False,
            overwrite=payload.overwrite,
        )
        if conflict is not None:
            return conflict
        _rename_single_object(storage, source_name=source_path, destination_name=destination_path)
        return {
            "ok": True,
            "kind": "file",
            "source_path": source_path,
            "destination_path": destination_path,
            "message": f"文件已重命名为：{destination_path}",
            "overwritten": payload.overwrite,
        }
    except HTTPException:
        raise
    except OCIStorageError as exc:
        raise HTTPException(status_code=500, detail=f"重命名失败：{exc}") from exc


@router.post("/api/files/delete")
def delete_path(request: Request, payload: DeletePathRequest = Body(...)):
    require_write_access(request)
    path = (payload.path or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="路径不能为空")
    storage = get_storage()
    try:
        if path.endswith("/"):
            result = _delete_prefix(storage, path_prefix=path, deleted_by="web-ui")
            recycled = result["recycled_count"] > 0
            return {
                "ok": True,
                "kind": "folder",
                "path": _normalize_prefix(path),
                "deleted_count": result["deleted_count"],
                "recycled_count": result["recycled_count"],
                "message": (
                    f"目录已移入回收站：{_normalize_prefix(path)}"
                    if recycled
                    else f"目录已删除：{_normalize_prefix(path)}"
                ),
            }
        result = _delete_object_with_policy(
            storage,
            object_name=_normalize_path(path),
            deleted_by="web-ui",
        )
        return {
            "ok": True,
            "kind": "file",
            "path": _normalize_path(path),
            "recycled": result["recycled"],
            "trash_key": result["trash_record"]["trash_key"] if result["trash_record"] else None,
            "message": (
                f"文件已移入回收站：{_normalize_path(path)}"
                if result["recycled"]
                else f"文件已删除：{_normalize_path(path)}"
            ),
        }
    except HTTPException:
        raise
    except OCIStorageError as exc:
        raise HTTPException(status_code=500, detail=f"删除失败：{exc}") from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"删除失败：{exc}") from exc


@router.post("/objects/batch-delete")
def batch_delete_objects(request: Request, payload: BatchDeleteRequest = Body(...)):
    if not request.session.get("authenticated"):
        return JSONResponse({"detail": "未登录"}, status_code=401)
    require_write_access(request)

    object_names = _normalize_object_names(payload.object_names)

    if not object_names:
        raise HTTPException(status_code=400, detail="至少要选择一个对象")

    confirmation_required = get_app_settings_store().batch_delete_confirmation_required()
    if confirmation_required and payload.confirmation_count != len(object_names):
        raise HTTPException(
            status_code=400,
            detail=f"批量删除需要输入所选对象数量 {len(object_names)} 进行确认",
        )

    storage = get_storage()
    deleted = []
    recycled = []
    failed = []

    for object_name in object_names:
        try:
            result = _delete_object_with_policy(storage, object_name=object_name, deleted_by="web-ui-batch")
            deleted.append(object_name)
            if result["recycled"]:
                recycled.append(
                    {
                        "object_name": object_name,
                        "trash_key": result["trash_record"]["trash_key"],
                    }
                )
        except OCIStorageError as exc:
            failed.append({"object_name": object_name, "detail": str(exc)})
        except Exception as exc:
            failed.append({"object_name": object_name, "detail": f"异常信息：{exc}"})

    deleted_count = len(deleted)
    failed_count = len(failed)
    requested_count = len(object_names)

    if failed_count == 0:
        message = f"批量删除成功：共删除 {deleted_count} 个对象。"
        detail = f"已删除所选 {deleted_count} 个对象，当前前缀过滤上下文保持不变。"
        return {
            "ok": True,
            "requested_count": requested_count,
            "deleted_count": deleted_count,
            "failed_count": failed_count,
            "deleted": deleted,
            "recycled": recycled,
            "failed": failed,
            "message": message,
            "detail": detail,
        }

    failed_names = "、".join(item["object_name"] for item in failed[:5])
    if failed_count == requested_count:
        message = f"批量删除失败：{requested_count} 个对象均未删除。"
        detail = f"失败对象：{failed_names}" if failed_names else "所选对象均删除失败。"
        status_code = 500
    else:
        message = f"批量删除部分完成：成功 {deleted_count} 个，失败 {failed_count} 个。"
        detail = f"失败对象：{failed_names}" if failed_names else "部分对象删除失败。"
        status_code = 207

    return JSONResponse(
        {
            "ok": False,
            "requested_count": requested_count,
            "deleted_count": deleted_count,
            "failed_count": failed_count,
            "deleted": deleted,
            "recycled": recycled,
            "failed": failed,
            "message": message,
            "detail": detail,
        },
        status_code=status_code,
    )


@router.delete("/objects/{object_name:path}")
def delete_object(request: Request, object_name: str):
    if not request.session.get("authenticated"):
        return JSONResponse({"detail": "未登录"}, status_code=401)
    require_write_access(request)

    try:
        result = _delete_object_with_policy(
            get_storage(),
            object_name=object_name,
            deleted_by="web-ui",
        )
    except OCIStorageError as exc:
        detail = str(exc)
        raise HTTPException(
            status_code=404,
            detail=f"删除对象失败：{object_name}。{detail}",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"删除对象失败：{object_name}。异常信息：{exc}",
        ) from exc

    return {
        "ok": True,
        "object_name": object_name,
        "recycled": result["recycled"],
        "trash_key": result["trash_record"]["trash_key"] if result["trash_record"] else None,
        "message": (
            f"已移入回收站：{object_name}"
            if result["recycled"]
            else f"已删除对象：{object_name}"
        ),
        "detail": (
            f"对象“{object_name}”已复制到回收站并从原路径移除。"
            if result["recycled"]
            else f"对象“{object_name}”已从 bucket 中移除。"
        ),
    }


@router.get("/thumb/{object_name:path}")
def thumb(request: Request, object_name: str):
    if not request.session.get("authenticated"):
        return redirect_to_login(request.url.path)
    try:
        preview = get_storage().get_preview(object_name)
    except OCIStorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if preview.kind != "image" or not preview.bytes_data:
        raise HTTPException(status_code=404, detail="该对象不支持缩略图")

    return StreamingResponse(BytesIO(preview.bytes_data), media_type=preview.content_type)


@router.get("/view/{object_name:path}", response_class=HTMLResponse)
def view_object(request: Request, object_name: str):
    if not request.session.get("authenticated"):
        return redirect_to_login(request.url.path)
    try:
        preview = get_storage().get_preview(object_name)
    except OCIStorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    context = template_context(
        request,
        object_name=object_name,
        preview=preview,
        data_url=None,
    )
    if preview.bytes_data and preview.kind in {"image", "pdf"}:
        context["data_url"] = to_data_url(preview.content_type, preview.bytes_data)
    return templates.TemplateResponse(request, "view.html", context)
