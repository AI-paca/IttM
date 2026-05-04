from fastapi import APIRouter
from app.schemas import HealthResponse

router = APIRouter()

@router.get("/health", response_model=HealthResponse)
def health_endpoint():
    return HealthResponse(ok=True, service="Python OCR Service")

@router.get("/readiness")
def readiness_endpoint():
    # If a real engine check was needed to be 'ready'
    # For now, it's just basic service readiness
    return {"ready": True}
