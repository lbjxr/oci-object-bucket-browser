from __future__ import annotations

import secrets
from datetime import datetime
from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.oci_client import OCIStorageError, OCIStorageService
from app.utils import is_image_type, is_pdf_type, is_text_type, object_name_from_upload, to_data_url

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def format_size_mb(size: int | None) -> str:
    if size is None:
        return ""
    return f"{size / 1024 / 1024:.2f} MB"


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


def enrich_objects(objects):
    for obj in objects:
        setattr(obj, "size_mb", format_size_mb(obj.size))
        setattr(obj, "time_display", format_time_to_seconds(obj.time_created))
        setattr(obj, "is_image", is_image_type(obj.content_type or ""))
        setattr(obj, "file_icon", file_icon_for(obj.content_type))
    return objects


def get_storage() -> OCIStorageService:
    return OCIStorageService()


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
        **extra,
    }


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
        return templates.TemplateResponse(
            request,
            "index.html",
            template_context(request, objects=objects, prefix=prefix, error=None),
        )
    except OCIStorageError as exc:
        return templates.TemplateResponse(
            request,
            "index.html",
            template_context(request, objects=[], prefix=prefix, error=str(exc)),
            status_code=500,
        )


@router.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    if not request.session.get("authenticated"):
        return redirect_to_login(request.url.path)
    if not file.filename:
        raise HTTPException(status_code=400, detail="缺少文件名")
    object_name = object_name_from_upload(file.filename)
    try:
        data = await file.read()
        get_storage().upload_file(object_name, BytesIO(data), content_type=file.content_type)
    except OCIStorageError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return RedirectResponse(url="/", status_code=303)


@router.get("/download/{object_name:path}")
def download(request: Request, object_name: str):
    if not request.session.get("authenticated"):
        return redirect_to_login(request.url.path)
    try:
        stream, content_type = get_storage().open_stream(object_name)
        headers = {"Content-Disposition": f'attachment; filename="{object_name.split("/")[-1]}"'}
        return StreamingResponse(stream, media_type=content_type, headers=headers)
    except OCIStorageError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


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
