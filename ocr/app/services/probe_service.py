from app.schemas import ProbeRequest, ProbeReport, ProbeCaseResult
import time

def run_probe(request: ProbeRequest) -> ProbeReport:
    start = time.time()
    
    # Stubbed probe results
    cases = [
        ProbeCaseResult(
            name="image_loader_test",
            ok=True,
            message="Image loader initialized",
            elapsed_ms=5
        ),
        ProbeCaseResult(
            name="stub_engine_test",
            ok=True,
            message="Stub engine returned marker: OCR PROBE",
            elapsed_ms=2
        )
    ]
    
    return ProbeReport(
        ok=all(c.ok for c in cases),
        cases=cases
    )
