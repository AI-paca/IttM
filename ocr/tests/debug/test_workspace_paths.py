import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
HELPER = ROOT / "scripts/debug/workspace-paths.sh"


@pytest.fixture
def helper(tmp_path):
    root = tmp_path / "workspace"
    script = root / "scripts/debug/workspace-paths.sh"
    script.parent.mkdir(parents=True)
    script.write_text(HELPER.read_text())
    (root / "debug/fixtures").mkdir(parents=True)
    return script


def resolve_debug_path(value, helper):
    return subprocess.run(
        ["bash", "-c", 'source "$1"; debug_data_path "$2"', "bash", str(helper), str(value)],
        capture_output=True,
        text=True,
    )


def test_current_fixture_is_repository_relative(helper):
    result = resolve_debug_path("debug/fixtures/SAMPLE_4k.png", helper)
    assert result.returncode == 0
    assert result.stdout.strip() == "debug/fixtures/SAMPLE_4k.png"


@pytest.mark.parametrize("value", ["../outside.png", "/tmp/outside.png", "debug/../../outside", "ocr/app"])
def test_debug_rejects_paths_outside_debug(value, helper):
    result = resolve_debug_path(value, helper)
    assert result.returncode != 0
    assert not result.stdout


def test_debug_rejects_symlink_escape(tmp_path):
    # The helper itself can be copied to a temporary workspace to avoid writing
    # into read-only debug fixture mounts in CI.
    root = tmp_path / "workspace"
    script = root / "scripts/debug/workspace-paths.sh"
    script.parent.mkdir(parents=True)
    script.write_text(HELPER.read_text())
    (root / "debug").symlink_to(tmp_path, target_is_directory=True)
    result = subprocess.run(
        ["bash", "-c", 'source "$1"; debug_data_path debug/escape', "bash", str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "leaves the current workspace" in result.stderr
