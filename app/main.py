import logging
from contextlib import asynccontextmanager

from urllib.parse import urlparse
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from app.config import get_settings, is_production_env, validate_single_instance_runtime, validate_startup_security
from app.routes import router
from app.upload_cleanup import UploadCleanupScheduler, UploadCleanupService
from app.upload_tasks import get_upload_task_manager


def _same_origin_request(request: Request) -> bool:
    origin = request.headers.get("origin")
    expected = str(request.base_url).rstrip("/")
    if origin:
        return origin.rstrip("/") == expected
    referer = request.headers.get("referer")
    if referer:
        parsed = urlparse(referer)
        return f"{parsed.scheme}://{parsed.netloc}" == expected
    return False


async def _csrf_guard(request: Request, call_next):
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return await call_next(request)
    if request.url.path == "/webdav" or request.url.path.startswith("/webdav/"):
        return await call_next(request)
    if not _same_origin_request(request):
        return JSONResponse({"detail": "请求来源校验失败，请从本站重新操作。"}, status_code=403)
    return await call_next(request)

async def _security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    settings = get_settings()
    if is_production_env(settings.app_env) and settings.session_https_only:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


@asynccontextmanager
async def _app_lifespan(app: FastAPI):
    settings = get_settings()
    manager = get_upload_task_manager()
    cleanup_service = UploadCleanupService(settings=settings, manager=manager)
    cleanup_scheduler = UploadCleanupScheduler(cleanup_service)
    app.state.upload_cleanup_service = cleanup_service
    app.state.upload_cleanup_scheduler = cleanup_scheduler

    if settings.upload_cleanup_enabled and settings.upload_cleanup_startup_enabled:
        cleanup_service.run_once()
    cleanup_scheduler.start()
    try:
        yield
    finally:
        cleanup_scheduler.stop()


def create_app() -> FastAPI:
    settings = get_settings()
    validate_startup_security(settings)
    validate_single_instance_runtime(settings)
    if is_production_env(settings.app_env) and not settings.session_https_only:
        logging.getLogger(__name__).warning(
            "生产环境 APP_SESSION_HTTPS_ONLY=false，Session Cookie 将不带 Secure；请仅在 HTTPS 终止并明确接受风险时使用。"
        )
    app = FastAPI(title="OCI Object Bucket Browser", version="0.2.0", lifespan=_app_lifespan)
    app.middleware("http")(_security_headers)
    app.middleware("http")(_csrf_guard)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie=settings.session_cookie_name,
        same_site="lax",
        https_only=settings.session_https_only,
        max_age=60 * 60 * 12,
    )
    app.mount("/static", StaticFiles(directory="app/static"), name="static")
    app.include_router(router)
    return app


app = create_app()
