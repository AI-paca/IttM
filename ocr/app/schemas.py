from typing import List, Dict, Any
from pydantic import BaseModel

class ConvertMeta(BaseModel):
    engine: str
    chunks: int
    pages: int
    elapsed_ms: int

class ConvertResponse(BaseModel):
    markdown: str
    meta: ConvertMeta

class HealthResponse(BaseModel):
    ok: bool
    service: str

class CapabilityReport(BaseModel):
    runtime: Dict[str, Any]
    hardware: Dict[str, Any]
    engines: Dict[str, Any]
    loaders: Dict[str, Any]

class ProbeCaseResult(BaseModel):
    name: str
    ok: bool
    message: str
    elapsed_ms: int

class ProbeReport(BaseModel):
    ok: bool
    cases: List[ProbeCaseResult]

class ProbeRequest(BaseModel):
    modes: List[str]
    engines: List[str]
