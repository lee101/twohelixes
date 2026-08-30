"""The top-level bootstrap is a supported entry point, not shell folklore."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETUP = ROOT / "setup.sh"


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(SETUP), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        env=env,
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


def test_check_rejects_a_broken_version_manager_shim(tmp_path: Path) -> None:
    bun = tmp_path / "bun"
    bun.write_text("#!/bin/sh\nexit 1\n")
    bun.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}

    result = _run("--check", "--skip-python", "--skip-browser", env=env)

    assert result.returncode == 1
    assert "missing  usable command bun" in result.stdout


def test_browser_check_verifies_the_executable_not_install_metadata() -> None:
    source = SETUP.read_text()
    assert "chromium.executable_path" in source
    assert "executable.is_file()" in source
    assert "playwright install --dry-run" not in source
