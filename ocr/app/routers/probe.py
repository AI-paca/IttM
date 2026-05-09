from fastapi import APIRouter
from app.schemas import ProbeRequest, ProbeReport
from app.services import probe_service

router = APIRouter()

@router.post("/probe", response_model=ProbeReport)
@router.post("/v1/probe", response_model=ProbeReport)
def probe_endpoint(request: ProbeRequest):
    return probe_service.run_probe(request)
