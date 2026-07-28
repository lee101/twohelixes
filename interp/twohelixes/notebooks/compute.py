"""Remote compute for notebooks and long-running agents.

Two providers, chosen for what they are good at rather than for parity:

* **Hetzner** - cheap, fast, plain CPU boxes. The right home for a notebook
  that is doing pandas work, which is nearly all of them.
* **RunPod** - GPU pods, for the cases that actually need one.

Both are behind one interface so `runner.py` does not care which is in use,
and so the local path is the same shape as the remote one.

A note carried over from Codex Infinity's experience with Hetzner: **do not
put a large cloud-init payload in `user_data`.** Hetzner rejects it above
~32 KB and the failure surfaces as an opaque provisioning error. Keep the
script to a fetch-and-run stub, and pull the real payload over HTTPS. Prefer
keeping a warm instance over cold-provisioning per session - boot plus package
install is a minute or more, which is far too long to make a user wait.
"""

from __future__ import annotations

import json
import logging
import secrets
import time
from typing import Any

from twohelixes import config

log = logging.getLogger("twohelixes.notebooks.compute")

HETZNER_API = "https://api.hetzner.cloud/v1"
RUNPOD_API = "https://rest.runpod.io/v1"

# Small and cheap by default: a notebook is usually pandas over a few hundred
# megabytes, not a training run.
HETZNER_DEFAULT_TYPE = "cpx21"  # 3 vCPU, 4 GB
HETZNER_DEFAULT_IMAGE = "ubuntu-24.04"
HETZNER_DEFAULT_LOCATION = "nbg1"

RUNPOD_DEFAULT_GPU = "NVIDIA GeForce RTX 4090"
RUNPOD_IMAGE = "runpod/pytorch:2.4.0-py3.11-cuda12.4.1-devel-ubuntu22.04"

BOOT_TIMEOUT = 300


class ComputeError(Exception):
    pass


class NotConfigured(ComputeError):
    pass


def _client():
    import httpx

    return httpx.Client(timeout=60)


def available_providers() -> dict[str, bool]:
    return {
        "local": True,
        "hetzner": bool(config.get("HETZNER_API_TOKEN")),
        "runpod": bool(config.get("RUNPOD_API_KEY")),
    }


# --------------------------------------------------------------------------
# Cloud-init
# --------------------------------------------------------------------------


def _bootstrap_script(token: str, payload_url: str = "") -> str:
    """A deliberately tiny cloud-init.

    Hetzner rejects a large `user_data`, and the error it returns does not say
    so. Everything heavy is fetched at boot instead of inlined here.
    """
    fetch = (
        f"curl -fsSL '{payload_url}' -o /opt/nb/notebook.py || true"
        if payload_url
        else "true"
    )
    return f"""#!/bin/bash
set -eux
mkdir -p /opt/nb
{fetch}
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-pip python3-venv >/dev/null
python3 -m venv /opt/nb/venv
/opt/nb/venv/bin/pip install -q --upgrade pip
/opt/nb/venv/bin/pip install -q marimo pandas pyarrow duckdb plotly
cat >/etc/systemd/system/nb.service <<'UNIT'
[Unit]
Description=twoHelixes notebook
After=network-online.target
[Service]
ExecStart=/opt/nb/venv/bin/marimo edit /opt/nb/notebook.py --headless \\
  --host 0.0.0.0 --port 8080 --token-password {token} --no-sandbox
Restart=always
WorkingDirectory=/opt/nb
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now nb.service
"""


# --------------------------------------------------------------------------
# Hetzner
# --------------------------------------------------------------------------


def hetzner_create(name: str, token: str, payload_url: str = "",
                   server_type: str = "") -> dict[str, Any]:
    api_token = config.get("HETZNER_API_TOKEN")
    if not api_token:
        raise NotConfigured("HETZNER_API_TOKEN is not set")

    # The machine class picks the server type; the environment is only the
    # fallback for callers that have no class, because a class whose type came
    # from the environment would be billed at one size and provisioned at
    # another.
    body = {
        "name": name,
        "server_type": server_type or config.get("HETZNER_SERVER_TYPE", HETZNER_DEFAULT_TYPE),
        "image": config.get("HETZNER_IMAGE", HETZNER_DEFAULT_IMAGE),
        "location": config.get("HETZNER_LOCATION", HETZNER_DEFAULT_LOCATION),
        "start_after_create": True,
        "user_data": _bootstrap_script(token, payload_url),
        "labels": {"app": "twohelixes", "role": "notebook"},
    }

    # The 32 KB ceiling is why the bootstrap is a stub. Check before sending so
    # the failure is legible rather than an opaque 400 from the API.
    if len(body["user_data"].encode()) > 30_000:
        raise ComputeError("cloud-init payload is too large for Hetzner user_data")

    with _client() as client:
        response = client.post(
            f"{HETZNER_API}/servers",
            headers={"Authorization": f"Bearer {api_token}"},
            json=body,
        )
        if response.status_code >= 400:
            raise ComputeError(f"Hetzner rejected the server: {response.text[:300]}")
        data = response.json()

    server = data["server"]
    return {
        "provider": "hetzner",
        "id": str(server["id"]),
        "ip": server.get("public_net", {}).get("ipv4", {}).get("ip", ""),
        "status": server.get("status"),
    }


def hetzner_delete(server_id: str) -> None:
    api_token = config.get("HETZNER_API_TOKEN")
    if not api_token:
        return
    with _client() as client:
        client.delete(
            f"{HETZNER_API}/servers/{server_id}",
            headers={"Authorization": f"Bearer {api_token}"},
        )


def hetzner_list() -> list[dict[str, Any]]:
    """Our servers, normalised to `{id, name, status, created_at}`.

    `created_at` is epoch seconds because the orphan reaper compares it to
    `time.time()`; Hetzner returns ISO 8601, and a string there would make
    every instance look infinitely old and get reaped mid-provision.
    """
    api_token = config.get("HETZNER_API_TOKEN")
    if not api_token:
        raise NotConfigured("HETZNER_API_TOKEN is not set")
    with _client() as client:
        response = client.get(
            f"{HETZNER_API}/servers?label_selector=app=twohelixes",
            headers={"Authorization": f"Bearer {api_token}"},
        )
        response.raise_for_status()
        servers = response.json().get("servers", [])
    return [
        {
            "id": str(s.get("id", "")),
            "name": s.get("name", ""),
            "status": s.get("status", ""),
            "created_at": _epoch(s.get("created")),
            "raw": s,
        }
        for s in servers
    ]


def _epoch(value: Any) -> float:
    """Provider timestamps, in seconds. Unparseable means "now", which is the
    safe direction: a machine we cannot date is treated as too new to reap."""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value:
        from datetime import datetime

        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time()


# --------------------------------------------------------------------------
# RunPod
# --------------------------------------------------------------------------


def runpod_create(name: str, token: str, gpu_type: str = "") -> dict[str, Any]:
    api_key = config.get("RUNPOD_API_KEY")
    if not api_key:
        raise NotConfigured("RUNPOD_API_KEY is not set")

    body = {
        "name": name,
        "imageName": config.get("RUNPOD_IMAGE", RUNPOD_IMAGE),
        "gpuTypeIds": [gpu_type or config.get("RUNPOD_GPU_TYPE", RUNPOD_DEFAULT_GPU)],
        "cloudType": "SECURE",
        "containerDiskInGb": 20,
        "ports": ["8080/http"],
        "env": {"NB_TOKEN": token},
        "dockerStartCmd": [
            "bash", "-lc",
            "pip install -q marimo pandas pyarrow duckdb plotly && "
            "marimo edit /workspace/notebook.py --headless --host 0.0.0.0 "
            f"--port 8080 --token-password {token} --no-sandbox",
        ],
    }

    with _client() as client:
        response = client.post(
            f"{RUNPOD_API}/pods",
            headers={"Authorization": f"Bearer {api_key}"},
            json=body,
        )
        if response.status_code >= 400:
            raise ComputeError(f"RunPod rejected the pod: {response.text[:300]}")
        data = response.json()

    pod_id = data.get("id") or data.get("pod", {}).get("id", "")
    return {
        "provider": "runpod",
        "id": str(pod_id),
        # RunPod fronts HTTP ports on a predictable proxy host.
        "url": f"https://{pod_id}-8080.proxy.runpod.net/?access_token={token}",
        "status": data.get("desiredStatus", "PENDING"),
    }


def runpod_list() -> list[dict[str, Any]]:
    """Every pod on the account, normalised like `hetzner_list`.

    RunPod has no label selector, so this returns everything the key can see -
    which is what the reaper wants anyway: a pod we cannot account for is a pod
    costing us money whether or not we tagged it.
    """
    api_key = config.get("RUNPOD_API_KEY")
    if not api_key:
        raise NotConfigured("RUNPOD_API_KEY is not set")
    with _client() as client:
        response = client.get(
            f"{RUNPOD_API}/pods",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        response.raise_for_status()
        payload = response.json()
    pods = payload if isinstance(payload, list) else payload.get("pods", [])
    return [
        {
            "id": str(p.get("id", "")),
            "name": p.get("name", ""),
            "status": p.get("desiredStatus", ""),
            "created_at": _epoch(p.get("createdAt") or p.get("created_at")),
            "raw": p,
        }
        for p in pods
    ]


def runpod_delete(pod_id: str) -> None:
    api_key = config.get("RUNPOD_API_KEY")
    if not api_key:
        return
    with _client() as client:
        client.delete(
            f"{RUNPOD_API}/pods/{pod_id}",
            headers={"Authorization": f"Bearer {api_key}"},
        )


# --------------------------------------------------------------------------
# Uniform interface
# --------------------------------------------------------------------------


def start_remote(provider: str, user_id: str, session_id: str, source: str,
                 machine: Any = None) -> dict[str, Any]:
    """Provision a notebook on `provider` and return connection details.

    The notebook source goes to R2 and the instance fetches it, rather than
    riding along in cloud-init: that is what keeps `user_data` under Hetzner's
    ceiling, and it means the payload can be any size.

    `machine` is a `machines.MachineClass`. It decides the size, and the size is
    what the caller is being billed for, so it must not come from anywhere else.
    """
    token = secrets.token_urlsafe(24)
    payload_url = ""

    try:
        from twohelixes.storage import r2

        if r2.is_configured():
            key = f"notebooks/{user_id}/{session_id}.py"
            r2.put(key, source.encode(), "text/x-python")
            payload_url = r2.presign(key, "GET", 3600)
    except Exception:  # noqa: BLE001
        log.warning("could not stage notebook source to R2; booting empty")

    name = f"th-nb-{session_id[:10]}"

    provider_ref = getattr(machine, "provider_ref", "") or ""

    if provider == "hetzner":
        info = hetzner_create(name, token, payload_url, server_type=provider_ref)
        info["url"] = f"http://{info['ip']}:8080/?access_token={token}" if info.get("ip") else ""
    elif provider == "runpod":
        info = runpod_create(name, token, gpu_type=provider_ref)
    else:
        raise ComputeError(f"unknown provider '{provider}'")

    info.update({
        "token": token, "running": True, "session_id": session_id,
        "machine": getattr(machine, "id", ""),
    })
    log.info("started %s notebook %s (%s)", provider, session_id, info.get("id"))
    return info


def stop_remote(provider: str, remote: dict[str, Any]) -> None:
    identifier = str(remote.get("id") or "")
    if not identifier:
        return
    if provider == "hetzner":
        hetzner_delete(identifier)
    elif provider == "runpod":
        runpod_delete(identifier)
    log.info("stopped %s instance %s", provider, identifier)
