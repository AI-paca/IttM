#!/usr/bin/env python3
"""Keep the native compatibility profile catalog equal to the Python API contract."""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ocr"))
from app.pipeline_config import OCR_PIPELINE_PROFILES
from app.pipeline_flags import pipeline_flags_payload, profile_flags

payload = {
    "profiles": {
        name: {
            "name": name,
            "psm": [p.text_region_psm, p.document_region_psm, p.wide_text_region_psm],
            "structural_output": p.structural_output,
            "flags": sorted(profile_flags(p)),
        }
        for name, p in OCR_PIPELINE_PROFILES.items()
    },
    "api": pipeline_flags_payload(),
}
path = ROOT / "ocr-runtime" / "profiles.json"
if "--write" in sys.argv:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
else:
    assert json.loads(path.read_text()) == payload, "Run scripts/ci/verify-native-profiles.py --write"
    print("Native/Python profile catalog matches")
