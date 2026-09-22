from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from backend.api.auth_routes import router as auth_router
from backend.api.routes import router as chat_router


def create_app() -> FastAPI:
    app = FastAPI(title="JLR Multi-Agent Knowledge API", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:3000", "http://localhost:5178"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.router.include_router(chat_router)
    app.router.include_router(auth_router)

    @app.get("/")
    def root():
        return {"service": "JLR Multi-Agent Knowledge API", "status": "ok", "docs": "/docs"}

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app()
