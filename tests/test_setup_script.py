"""The top-level bootstrap is a supported entry point, not shell folklore."""

from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "setup.sh"


def _run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SETUP), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_setup_script_has_valid_shell_syntax() -> None:
    result = subprocess.run(
        ["bash", "-n", str(SETUP)], text=True, capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr


def test_dry_run_describes_the_complete_bootstrap_without_mutating() -> None:
    result = _run("--dry-run")
    assert result.returncode == 0, result.stderr
    assert "scripts/setup-venvs.sh" in result.stdout
    assert "bun install --frozen-lockfile" in result.stdout
    assert "bun run build" in result.stdout
    assert "playwright install chromium" in result.stdout


def test_runtime_setup_does_not_install_a_test_browser() -> None:
    result = _run("--dry-run", "--runtime")
    assert result.returncode == 0, result.stderr
    assert "setup-venvs.sh --runtime" in result.stdout
    assert "playwright install" not in result.stdout


def test_unknown_options_fail_with_a_useful_message() -> None:
    result = _run("--not-an-option")
    assert result.returncode == 2
    assert "unknown option" in result.stderr
    assert "Usage: ./setup.sh" in result.stderr
