from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.routes import router
from app.upload_cleanup import UploadCleanupScheduler, UploadCleanupService
from app.upload_tasks import get_upload_task_manager


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
    app = FastAPI(title="OCI Object Bucket Browser", version="0.2.0", lifespan=_app_lifespan)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie=settings.session_cookie_name,
        same_site="lax",
        https_only=False,
        max_age=60 * 60 * 12,
    )
    app.mount("/static", StaticFiles(directory="app/static"), name="static")
    app.include_router(router)
    return app


app = create_app()
