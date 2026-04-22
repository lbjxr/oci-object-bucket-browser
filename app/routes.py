from __future__ import annotations

import hashlib
import json
import secrets
import tempfile
import zipfile
from datetime import datetime
from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.config import get_settings
from app.oci_client import OCIStorageError, OCIStorageService, classify_upload_exception
from app.upload_sessions import UploadSession, UploadedPart, UploadSessionStore
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


def build_prefix_navigation(prefix: str, objects: list[object]) -> dict[str, object]:
    current_prefix = (prefix or "").strip()
    stripped = current_prefix.strip("/")
    segments = [segment for segment in stripped.split("/") if segment]

    breadcrumbs = []
    running_prefix = ""
    for segment in segments:
        running_prefix = f"{running_prefix}{segment}/"
        breadcrumbs.append(
            {
                "label": segment,
                "prefix": running_prefix,
                "href": f"/?prefix={quote(running_prefix)}",
            }
        )

    parent_prefix = ""
    if segments:
        parent_prefix = "/".join(segments[:-1])
        if parent_prefix:
            parent_prefix += "/"

    child_prefix_map: dict[str, dict[str, object]] = {}
    child_base = ""
    if current_prefix:
        child_base = current_prefix if current_prefix.endswith("/") else f"{current_prefix}/"

    for obj in objects:
        name = (getattr(obj, "name", "") or "").strip()
        if not name:
            continue
        if current_prefix and not name.startswith(current_prefix):
            continue

        remainder = name[len(current_prefix):] if current_prefix else name
        remainder = remainder.lstrip("/")
        if not remainder or "/" not in remainder:
            continue

        child_label = remainder.split("/", 1)[0]
        if not child_label:
            continue

        child_prefix = f"{child_base}{child_label}/"
        item = child_prefix_map.setdefault(
            child_prefix,
            {
                "label": child_label,
                "prefix": child_prefix,
                "href": f"/?prefix={quote(child_prefix)}",
                "object_count": 0,
            },
        )
        item["object_count"] += 1

    child_prefixes = sorted(child_prefix_map.values(), key=lambda item: str(item["label"]).lower())

    return {
        "current_prefix": current_prefix,
        "breadcrumbs": breadcrumbs,
        "parent_prefix": parent_prefix,
        "parent_href": f"/?prefix={quote(parent_prefix)}" if parent_prefix else "/",
        "child_prefixes": child_prefixes,
    }


def get_storage() -> OCIStorageService:
    return OCIStorageService()


def get_upload_store() -> UploadSessionStore:
    settings = get_settings()
    return UploadSessionStore(settings.upload_session_dir)


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


class SingleRangeRequest(BaseModel):
    start: int
    end: int


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


def _build_batch_download_filename(prefix: str, object_count: int) -> str:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    prefix_label = (prefix or "").strip().strip("/")
    if prefix_label:
        prefix_label = prefix_label.replace("/", "-").replace(" ", "-")[:48]
        return f"oci-batch-{prefix_label}-{object_count}items-{timestamp}.zip"
    return f"oci-batch-{object_count}items-{timestamp}.zip"


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
    try:
        objects = enrich_objects(get_storage().list_objects(prefix=prefix))
        prefix_navigation = build_prefix_navigation(prefix, objects)
        return templates.TemplateResponse(
            request,
            "index.html",
            template_context(request, objects=objects, prefix=prefix, prefix_navigation=prefix_navigation, error=None),
        )
    except OCIStorageError as exc:
        return templates.TemplateResponse(
            request,
            "index.html",
            template_context(
                request,
                objects=[],
                prefix=prefix,
                prefix_navigation=build_prefix_navigation(prefix, []),
                error=str(exc),
            ),
            status_code=500,
        )


@router.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    is_ajax = request.headers.get("x-requested-with") == "XMLHttpRequest"
    if not request.session.get("authenticated"):
        if is_ajax:
            return JSONResponse({"detail": "未登录"}, status_code=401)
        return redirect_to_login(request.url.path)
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")
    object_name = object_name_from_upload(file.filename)
    try:
        await run_in_threadpool(get_storage().upload_file, object_name, file.file, file.content_type)
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
            }
        )
    return RedirectResponse(url="/", status_code=303)


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
def batch_download_objects(request: Request, payload: BatchDownloadRequest = Body(...), prefix: str = ""):
    if not request.session.get("authenticated"):
        return JSONResponse({"detail": "未登录"}, status_code=401)

    object_names = _normalize_object_names(payload.object_names)
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
        filename = _build_batch_download_filename(prefix=prefix, object_count=len(object_names))
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
