"""FastAPI application.

Run with: uvicorn financial_analyst.main:create_app --factory
"""

from importlib.metadata import version

from fastapi import FastAPI
from pydantic import BaseModel

from financial_analyst.config import Settings

APP_VERSION = version("financial-analyst")


class HealthResponse(BaseModel):
    status: str
    version: str


def create_app(settings: Settings | None = None) -> FastAPI:
    # Loading settings here makes misconfiguration fail at startup, not on first use.
    app = FastAPI(title="Enterprise Financial AI Analyst", version=APP_VERSION)
    app.state.settings = settings or Settings()

    @app.get("/health")
    def health() -> HealthResponse:
        """Liveness check. Deliberately touches no external dependency."""
        return HealthResponse(status="ok", version=APP_VERSION)

    return app
