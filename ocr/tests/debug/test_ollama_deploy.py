import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
RUNNER_DIR = ROOT / "scripts" / "ollama-deploy"
RUNNER = RUNNER_DIR / "ollama_deploy.py"
CATALOG = RUNNER_DIR / "models.json"

pytestmark = pytest.mark.skipif(
    not RUNNER.exists() or not CATALOG.exists(),
    reason="local Ollama deployment scripts are not part of the repository",
)


def run_runner(*args):
    return subprocess.run(
        [sys.executable, str(RUNNER), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )


def test_manifest_groups_flags_by_pipeline_responsibility():
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))

    groups = {flag["group"] for model in catalog["models"] for flag in model["flag_contract"]}
    requirements = {flag["requirement"] for model in catalog["models"] for flag in model["flag_contract"]}

    assert {"alignment", "positioning", "composition"} <= groups
    assert {"required", "quality"} <= requirements


def test_runner_lists_supported_models():
    result = run_runner("--list")

    assert "glm-ocr" in result.stdout
    assert "paddle-ocrv6-medium" in result.stdout
    assert "qianfan-ocr" in result.stdout
    assert "nemotron-ocr-v2" in result.stdout


def test_runner_dry_run_glm_ollama_api():
    result = run_runner(
        "--model",
        "glm-ocr",
        "--backend",
        "ollama",
        "--dry-run",
        "--show-flags",
    )

    assert "ollama pull glm-ocr" in result.stdout
    assert "ollama serve" in result.stdout
    assert "Local API: http://127.0.0.1:11434/v1/chat/completions" in result.stdout
    assert "[required] positioning:layout_detector" in result.stdout


def test_runner_dry_run_qianfan_vllm_docker_api():
    result = run_runner(
        "--model",
        "qianfan-ocr",
        "--backend",
        "vllm",
        "--docker",
        "--dry-run",
        "--skip-download",
        "--port",
        "18081",
    )

    assert "docker run" in result.stdout
    assert "baidu/Qianfan-OCR" in result.stdout
    assert "InternVLChatModel" in result.stdout
    assert "Local API: http://127.0.0.1:18081/v1/chat/completions" in result.stdout


def test_runner_dry_run_nemotron_uses_external_source_cache_for_docker():
    result = run_runner(
        "--model",
        "nemotron-ocr-v2",
        "--backend",
        "nemotron",
        "--docker",
        "--dry-run",
        "--skip-download",
        "--port",
        "18083",
    )

    assert "nvcr.io/nvidia/pytorch:25.09-py3" in result.stdout
    assert "/nemotron-ocr-src" in result.stdout
    assert "local_ocr_api.py --backend nemotron" in result.stdout
