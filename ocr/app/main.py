import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routers import convert, health, probe


def _cors_origins() -> list[str]:
    origins = [
        origin.strip().rstrip("/") for origin in os.environ.get("OCR_CORS_ORIGINS", "").split(",") if origin.strip()
    ]
    if "*" in origins:  # nosemgrep: python.fastapi.security.wildcard-cors.wildcard-cors
        raise ValueError("OCR_CORS_ORIGINS must contain explicit origins; wildcard CORS is disabled.")
    return list(dict.fromkeys(origins))


def create_app() -> FastAPI:
    app = FastAPI(title="OCR Service", version="1.0.0")

    cors_origins = _cors_origins()
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # No prefix - routes are defined with full paths in routers
    app.include_router(convert.router)
    app.include_router(health.router)
    app.include_router(probe.router)

    try:
        from app.routers import install

        app.include_router(install.router)
    except ImportError:
        pass

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        reload=True,
    )
