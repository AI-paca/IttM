from __future__ import annotations

import concurrent.futures
import subprocess
import threading
import time
from pathlib import Path

import pytest

import app.sparse_pipeline.ocr_adapters as adapters
from app.sparse_pipeline.ocr_adapter_contracts import OcrExecutableUnavailableError


@pytest.fixture(autouse=True)
def _empty_capability_cache() -> None:
    with adapters._TESSERACT_CAPABILITY_CACHE_LOCK:
        adapters._TESSERACT_CAPABILITY_CACHE.clear()
        adapters._TESSERACT_CAPABILITY_PROBES.clear()
    yield
    with adapters._TESSERACT_CAPABILITY_CACHE_LOCK:
        adapters._TESSERACT_CAPABILITY_CACHE.clear()
        adapters._TESSERACT_CAPABILITY_PROBES.clear()


def _completed(
    command: object,
    stdout: str,
    *,
    returncode: int = 0,
) -> subprocess.CompletedProcess[object]:
    return subprocess.CompletedProcess(command, returncode, stdout, "")


def _successful_probe(
    calls: list[tuple[str, ...]],
    lock: threading.Lock | None = None,
):
    def run(command: object, **_kwargs: object) -> subprocess.CompletedProcess[object]:
        values = tuple(command)  # type: ignore[arg-type]
        if lock is None:
            calls.append(values)
        else:
            with lock:
                calls.append(values)
        if "--version" in values:
            return _completed(command, "tesseract 5.5.2\n")
        return _completed(
            command,
            "List of available languages (3):\nrus\neng\nchi_sim\n",
        )

    return run


def test_workers_reuse_one_process_local_capability_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(adapters.shutil, "which", lambda _name: "/fake/tesseract")
    monkeypatch.setattr(adapters.subprocess, "run", _successful_probe(calls))

    first = adapters.TesseractWorker(adapters.TesseractConfig())
    second = adapters.TesseractWorker(
        adapters.TesseractConfig(languages=("chi_sim",))
    )

    assert first.capabilities is second.capabilities
    assert len(calls) == 2


def test_capability_cache_separates_different_tessdata_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_tessdata = tmp_path / "first"
    second_tessdata = tmp_path / "second"
    first_tessdata.mkdir()
    second_tessdata.mkdir()
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(adapters.shutil, "which", lambda _name: "/fake/tesseract")
    monkeypatch.setattr(adapters.subprocess, "run", _successful_probe(calls))

    adapters.TesseractWorker(
        adapters.TesseractConfig(tessdata_directory=first_tessdata)
    )
    adapters.TesseractWorker(
        adapters.TesseractConfig(tessdata_directory=second_tessdata)
    )

    assert len(calls) == 4
    assert {call[-1] for call in calls if "--list-langs" in call} == {
        str(first_tessdata),
        str(second_tessdata),
    }


def test_concurrent_workers_share_one_inflight_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worker_count = 8
    start_barrier = threading.Barrier(worker_count)
    probe_started = threading.Event()
    release_probe = threading.Event()
    calls: list[tuple[str, ...]] = []
    calls_lock = threading.Lock()
    thread_state = threading.local()

    def which(_name: str) -> str:
        if not getattr(thread_state, "entered", False):
            thread_state.entered = True
            start_barrier.wait(timeout=2.0)
        return "/fake/tesseract"

    successful = _successful_probe(calls, calls_lock)

    def run(command: object, **kwargs: object) -> subprocess.CompletedProcess[object]:
        values = tuple(command)  # type: ignore[arg-type]
        if "--version" in values:
            probe_started.set()
            assert release_probe.wait(timeout=2.0)
        return successful(command, **kwargs)

    monkeypatch.setattr(adapters.shutil, "which", which)
    monkeypatch.setattr(adapters.subprocess, "run", run)

    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(adapters.TesseractWorker, adapters.TesseractConfig())
            for _ in range(worker_count)
        ]
        assert probe_started.wait(timeout=2.0)
        time.sleep(0.05)
        release_probe.set()
        capabilities = [future.result(timeout=2.0).capabilities for future in futures]

    assert all(item is capabilities[0] for item in capabilities)
    assert len(calls) == 2


def test_failed_probe_is_not_cached_permanently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    failing = True
    monkeypatch.setattr(adapters.shutil, "which", lambda _name: "/fake/tesseract")

    def run(command: object, **kwargs: object) -> subprocess.CompletedProcess[object]:
        nonlocal failing
        values = tuple(command)  # type: ignore[arg-type]
        calls.append(values)
        if failing:
            return _completed(command, "probe failed\n", returncode=1)
        return _successful_probe([], None)(command, **kwargs)

    monkeypatch.setattr(adapters.subprocess, "run", run)

    with pytest.raises(OcrExecutableUnavailableError):
        adapters.TesseractWorker(adapters.TesseractConfig())
    failing = False
    worker = adapters.TesseractWorker(adapters.TesseractConfig())

    assert worker.capabilities.version == "tesseract 5.5.2"
    assert len(calls) == 4
