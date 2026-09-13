#!/usr/bin/env python3
"""Start the built server with its matching Python runtime and isolated dev data."""
import json
import os
from pathlib import Path
import subprocess

root = Path(__file__).resolve().parents[1]
runtime = root / ".venv/bin/python"
if not runtime.exists() or not (root / "build/twohelixes-server").exists():
    raise SystemExit("Run scripts/setup-venvs.sh and build the Mojo server first.")
prefix, libdir, library, packages = json.loads(subprocess.check_output([
    str(runtime), "-c", "import json,sys,sysconfig; print(json.dumps([sys.base_prefix,sysconfig.get_config_var('LIBDIR'),sysconfig.get_config_var('LDLIBRARY'),sysconfig.get_paths()['purelib']]))"
], text=True))
env = dict(os.environ)
env.update(PYTHONHOME=prefix, LD_PRELOAD=str(Path(libdir) / library),
           MOJO_PYTHON_LIBRARY=str(Path(libdir) / library),
           TWOHELIXES_INTERP=str(root / "interp"), TWOHELIXES_SITE_PACKAGES=packages,
           TWOHELIXES_DEV="1", TWOHELIXES_DISABLE_EMAIL="1",
           TWOHELIXES_DATA_DIR=str(root / "var/dev"), TWOHELIXES_WORKERS="2")
env["TWOHELIXES_PORT"] = os.environ.get("TWOHELIXES_PORT", "7485")
os.chdir(root)
os.execve(str(root / "build/twohelixes-server"), ["twohelixes-server"], env)
