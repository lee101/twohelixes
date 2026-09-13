#!/usr/bin/env python3
"""Publish a complete build to the configured R2 bucket; retain rollback chunks."""
import argparse
import mimetypes
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from twohelixes import config
from twohelixes.storage import r2


def publish(path, root):
    key = path.relative_to(root).as_posix()
    content_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
    url = r2.presign(key, method="PUT", content_type=content_type)
    immutable = path.name.startswith(("chunk-", "reshape-chunk-", "asset-"))
    headers = {"Content-Type": content_type,
               "Cache-Control": "public, max-age=31536000, immutable" if immutable else "public, max-age=60, must-revalidate"}
    with path.open("rb") as content:
        response = requests.put(url, data=content, headers=headers, timeout=180)
    if not response.ok:
        # Presigned URLs contain credentials; never print the request URL.
        raise RuntimeError(f"Upload failed for {key}: HTTP {response.status_code}")
    return key


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=config.REPO_ROOT / "static")
    args = parser.parse_args()
    root = args.directory.resolve()
    for required in ("app.js", "marketing.js", "styles.css"):
        if not (root / required).is_file():
            raise SystemExit(f"Incomplete build: {required} is missing")
    files = sorted(p for p in root.rglob("*") if p.is_file())
    # Upload dependencies before the mutable entry points refer to them.
    entries = {"app.js", "marketing.js", "styles.css", "app.css", "th.js"}
    with ThreadPoolExecutor(max_workers=4) as pool:
        for group in ([p for p in files if p.name not in entries], [p for p in files if p.name in entries]):
            for key in pool.map(lambda p: publish(p, root), group):
                print(f"Published {key}")
