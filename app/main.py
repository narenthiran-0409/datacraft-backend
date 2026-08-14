import logging

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.api.v1.router import api_v1_router
from app.core.config import settings
from app.core.correlation import CorrelationIdMiddleware
from app.core.database import engine
from app.core.exceptions import register_exception_handlers
from app.core.logging import configure_logging
from app.core.redis_client import get_redis_client

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    configure_logging()

    app = FastAPI(title=settings.APP_NAME, version="0.1.0")

    app.add_middleware(CorrelationIdMiddleware)
    register_exception_handlers(app)

    app.include_router(api_v1_router, prefix=settings.API_V1_PREFIX)

    @app.get("/health", tags=["health"])
    async def health() -> dict:
        """Liveness only: process is up. No dependency checks."""
        return {"status": "ok"}

    @app.get("/readyz", tags=["health"])
    async def readyz() -> JSONResponse:
        dependencies = {}

        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            dependencies["postgres"] = {"status": "up"}
        except Exception as exc:  # noqa: BLE001
            dependencies["postgres"] = {"status": "down", "error": str(exc)}

        try:
            get_redis_client().ping()
            dependencies["redis"] = {"status": "up"}
        except Exception as exc:  # noqa: BLE001
            dependencies["redis"] = {"status": "down", "error": str(exc)}

        healthy = all(dep["status"] == "up" for dep in dependencies.values())
        body = {"status": "ok" if healthy else "unavailable", "dependencies": dependencies}
        return JSONResponse(status_code=200 if healthy else 503, content=body)

    return app


app = create_app()
