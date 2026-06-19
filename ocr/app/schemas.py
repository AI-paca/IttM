from typing import Any, Dict, List

from pydantic import BaseModel, Field


class ConvertMeta(BaseModel):
    engine: str
    engine_chain: List[str] = Field(default_factory=list)
    chunks: int
    cards_found: int = 0
    tables_found: int = 0
    table_cells: int = 0
    pages: int
    empty_pages: List[int] = Field(default_factory=list)
    pipeline: str = ""
    pdf_mode: str = "auto"
    flags: List[str] = Field(default_factory=list)
    preprocess_steps: List[str] = Field(default_factory=list)
    layout_steps: List[str] = Field(default_factory=list)
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
