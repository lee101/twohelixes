"""Execute the tracker against browser API doubles, without network access."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_tracker_behavior():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the tracker behavior tests")
    subprocess.run(
        [node, "--test", str(Path(__file__).with_suffix(".mjs"))],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
