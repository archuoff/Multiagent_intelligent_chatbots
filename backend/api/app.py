"""Shared FastAPI application entry point for the JLR knowledge platform."""

from fastapi import FastAPI

from backend.api.auth_routes import router as auth_router


def create_application() -> FastAPI:
    """Creates the shared API and registers authentication before agent routes are added."""
    application = FastAPI(title="JLR Multi-Agent Knowledge Platform", version="0.1.0")
    application.include_router(auth_router)
    return application


app = create_application()
