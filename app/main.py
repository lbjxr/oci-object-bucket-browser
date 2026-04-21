from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.routes import router


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="OCI Object Bucket Browser", version="0.2.0")
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
