from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.routers import convert, health, probe

def create_app() -> FastAPI:
    app = FastAPI(title="OCR Service", version="1.0.0")
    
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
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
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
