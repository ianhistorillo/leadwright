"""FastAPI application entrypoint."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.errors import register_exception_handlers
from app.api.middleware import LimitUploadSizeMiddleware, RequestContextMiddleware
from app.api.routes import companies, exports, health, jobs, people, ui
from app.config import get_settings

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app() -> FastAPI:
    """Build and configure the FastAPI application."""
    settings = get_settings()
    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        description=(
            "Discover employees by company domain, enrich contacts via "
            "official APIs, verify emails, and export clean lead data.\n\n"
            "Open `/app` for the local upload UI. "
            "When `API_KEY` is set, protected endpoints require header "
            "`X-API-Key: <key>`."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(LimitUploadSizeMiddleware)
    app.add_middleware(RequestContextMiddleware)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    register_exception_handlers(app)

    if WEB_DIR.is_dir():
        app.mount("/web", StaticFiles(directory=str(WEB_DIR)), name="web")

    app.include_router(health.router)
    app.include_router(ui.router)
    app.include_router(companies.router)
    app.include_router(people.router)
    app.include_router(jobs.router)
    app.include_router(exports.router)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        """Send operators to the local UI."""
        return RedirectResponse(url="/app")

    def custom_openapi() -> dict:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            description=app.description,
            routes=app.routes,
        )
        schema.setdefault("components", {}).setdefault("securitySchemes", {})
        schema["components"]["securitySchemes"]["ApiKeyAuth"] = {
            "type": "apiKey",
            "in": "header",
            "name": "X-API-Key",
            "description": "Required when API_KEY is configured in the environment.",
        }
        public_paths = {
            "/health",
            "/",
            "/app",
            "/docs",
            "/redoc",
            "/openapi.json",
        }
        for path, path_item in schema.get("paths", {}).items():
            if path in public_paths or path.startswith("/web"):
                continue
            for method_data in path_item.values():
                if isinstance(method_data, dict):
                    method_data.setdefault("security", [{"ApiKeyAuth": []}])
        app.openapi_schema = schema
        return app.openapi_schema

    app.openapi = custom_openapi  # type: ignore[method-assign]
    return app


app = create_app()
