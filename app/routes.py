from __future__ import annotations

import hashlib
import json
import secrets
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from fastapi import APIRouter, Body, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.config import get_settings
from app.oci_client import OCIStorageError, OCIStorageService, classify_upload_exception
from app.temp_uploads import TempUploadSessionStore, UploadedChunk
from app.upload_cleanup import run_upload_cleanup
from app.upload_sessions import UploadSession, UploadedPart, UploadSessionStore
from app.upload_tasks import get_upload_task_manager
from app.utils import is_image_type, is_pdf_type, is_text_type, object_name_from_upload, to_data_url

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
    return objects


def get_storage() -> OCIStorageService:
    return OCIStorageService()


def get_upload_store() -> UploadSessionStore:
    settings = get_settings()
    return UploadSessionStore(settings.upload_session_dir)


def get_temp_upload_store() -> TempUploadSessionStore:
    settings = get_settings()
    return TempUploadSessionStore(settings.upload_temp_dir)


def require_login(request: Request) -> None:
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=401, detail="未登录")


def redirect_to_login(next_path: str = "/") -> RedirectResponse:
    return RedirectResponse(url=f"/login?next={quote(next_path, safe='/?:=&')}", status_code=303)


def template_context(request: Request, **extra: object) -> dict[str, object]:
    settings = get_settings()
    return {
        "request": request,
        "app_title": "OCI Object Bucket Browser",
        "is_authenticated": bool(request.session.get("authenticated")),
        "auth_username": settings.auth_username,
        "upload_chunk_size_mb": settings.upload_chunk_size_mb,
        "upload_single_put_threshold_mb": settings.upload_single_put_threshold_mb,
        "upload_parallelism": settings.upload_parallelism,
        "upload_proxy_chunk_size_mb": settings.upload_proxy_chunk_size_mb,
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


class BatchDownloadRequest(BaseModel):
    object_names: list[str]


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
    storage.upload_file(destination_name, stream, content_type)


def _list_objects_for_prefix(storage: OCIStorageService, prefix: str) -> list[object]:
    return storage.list_objects(prefix=_normalize_prefix(prefix))


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


def _delete_prefix(storage: OCIStorageService, *, path_prefix: str) -> dict[str, int]:
    normalized_prefix = _normalize_prefix(path_prefix)
    if not normalized_prefix:
        raise HTTPException(status_code=400, detail="不允许删除根目录")
    objects = _list_objects_for_prefix(storage, normalized_prefix)
    if not objects:
        raise HTTPException(status_code=404, detail="目录不存在或目录下没有对象")
    deleted = 0
    for obj in objects:
        storage.delete_object(obj.name)
        deleted += 1
    return {"deleted_count": deleted}


def _content_disposition_attachment(filename: str) -> str:
    ascii_fallback = filename.encode("ascii", errors="ignore").decode("ascii") or "download.bin"
    quoted = quote(filename)
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quoted}"


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
def index(request: Request, prefix: str = ""):
    if not request.session.get("authenticated"):
        return redirect_to_login(request.url.path + (f"?prefix={quote(prefix)}" if prefix else ""))
    normalized_prefix = _normalize_prefix(prefix)
    breadcrumbs = _build_prefix_breadcrumbs(normalized_prefix)
    current_directory_label = normalized_prefix or "/"
    try:
        listed_objects = _list_objects_for_prefix(get_storage(), normalized_prefix)
        folders, files = _split_directory_entries(normalized_prefix, enrich_objects(listed_objects))
        return templates.TemplateResponse(
            request,
            "index.html",
            template_context(
                request,
                objects=files,
                folders=folders,
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
        return templates.TemplateResponse(
            request,
            "index.html",
            template_context(
                request,
                objects=[],
                folders=[],
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


@router.post("/upload")
async def upload(request: Request, file: UploadFile = File(...), overwrite: bool = Form(False)):
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
    if not request.session.get("authenticated"):
        if is_ajax:
            return JSONResponse({"detail": "未登录"}, status_code=401)
        return redirect_to_login(request.url.path)
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
    require_login(request)
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
    require_login(request)
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
    require_login(request)
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
    manager = get_upload_task_manager()
    task = await run_in_threadpool(
        manager.create_task_from_staged_file,
        filename=payload.filename,
        content_type=payload.content_type or session.content_type,
        staged_path=str(staged_path),
        total_size=payload.file_size,
    )
    temp_store.update(temp_upload_id, lambda s: setattr(s, "committed", True))
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
    require_login(request)
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
    require_login(request)
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
    require_login(request)
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
def list_files_api(request: Request, prefix: str = ""):
    require_login(request)
    normalized_prefix = _normalize_prefix(prefix)
    try:
        listed_objects = _list_objects_for_prefix(get_storage(), normalized_prefix)
        folders, files = _split_directory_entries(normalized_prefix, enrich_objects(listed_objects))
    except OCIStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "ok": True,
        "prefix": normalized_prefix,
        "current_directory_label": normalized_prefix or "/",
        "parent_prefix": _parent_prefix(normalized_prefix),
        "breadcrumbs": _build_prefix_breadcrumbs(normalized_prefix),
        "folders": [
            {
                "name": folder.name,
                "full_prefix": folder.full_prefix,
                "item_count": folder.item_count,
                "placeholder_exists": folder.placeholder_exists,
            }
            for folder in folders
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
            }
            for obj in files
        ],
    }


@router.post("/api/files/folders")
def create_folder(request: Request, payload: CreateFolderRequest = Body(...)):
    require_login(request)
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
    require_login(request)
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
    require_login(request)
    path = (payload.path or "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="路径不能为空")
    storage = get_storage()
    try:
        if path.endswith("/"):
            result = _delete_prefix(storage, path_prefix=path)
            return {
                "ok": True,
                "kind": "folder",
                "path": _normalize_prefix(path),
                "deleted_count": result["deleted_count"],
                "message": f"目录已删除：{_normalize_prefix(path)}",
            }
        storage.delete_object(_normalize_path(path))
        return {
            "ok": True,
            "kind": "file",
            "path": _normalize_path(path),
            "message": f"文件已删除：{_normalize_path(path)}",
        }
    except HTTPException:
        raise
    except OCIStorageError as exc:
        raise HTTPException(status_code=500, detail=f"删除失败：{exc}") from exc


@router.post("/objects/batch-delete")
def batch_delete_objects(request: Request, payload: BatchDeleteRequest = Body(...)):
    if not request.session.get("authenticated"):
        return JSONResponse({"detail": "未登录"}, status_code=401)

    object_names = _normalize_object_names(payload.object_names)

    if not object_names:
        raise HTTPException(status_code=400, detail="至少要选择一个对象")

    storage = get_storage()
    deleted = []
    failed = []

    for object_name in object_names:
        try:
            storage.delete_object(object_name)
            deleted.append(object_name)
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

    try:
        get_storage().delete_object(object_name)
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
        "message": f"已删除对象：{object_name}",
        "detail": f"对象“{object_name}”已从 bucket 中移除。",
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
