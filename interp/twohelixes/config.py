"""Runtime configuration.

Secrets are read from the environment first, then from a small set of dotenv
files shared with the other app.nz services so a dev box does not need a
separate copy of every key.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Searched in order; the first file that defines a key wins.
DOTENV_CANDIDATES = (
    REPO_ROOT / ".env",
    REPO_ROOT.parent / "askfelix" / ".env",
    REPO_ROOT.parent / "openpaths" / ".env",
    REPO_ROOT.parent / "homesyai" / ".env",
    Path.home() / ".secretbashrc",
)

# --------------------------------------------------------------------------
# Models. Luna is the default route for interactive chart work: it is the
# cheap tier and every interactive query is rate limited anyway. Sol is the
# escalation used by long-running agents, which are paid-only.
# --------------------------------------------------------------------------
MODEL_DEFAULT = "gpt-5.6-luna"
MODEL_ESCALATE = "gpt-5.6-terra"
MODEL_DEEP = "gpt-5.6-sol"
MODEL_FAST = "auto-easy-task"

DEFAULT_BASE_URL = "https://openpaths.io/v1"

# --------------------------------------------------------------------------
# Free-tier policy: signed-in users get a small number of full pipeline runs,
# then must buy credits. Anonymous users get none - that is the abuse gate.
# --------------------------------------------------------------------------
FREE_QUERIES_PER_USER = 3
ANON_QUERIES_PER_USER = 0

# Heavy per-identity limits for free users. Paid API-credit spend bypasses
# these entirely (see ratelimit.check).
FREE_RATE_PER_MINUTE = 3
FREE_RATE_PER_HOUR = 10
FREE_RATE_PER_DAY = 20

# Global safety valve, applied to every identity including paid, to bound a
# single account's ability to saturate the box.
HARD_RATE_PER_MINUTE = 120

# Cost in API credits (1 credit == 1 US cent) for each billable operation.
CREDIT_COST = {
    "chat_query": 2,
    "dashboard_build": 6,
    "sql_generate": 1,
    "sql_autocomplete": 0,
    "deep_research": 150,
    "long_agent_minute": 10,
}


@lru_cache(maxsize=1)
def _dotenv() -> dict[str, str]:
    values: dict[str, str] = {}
    for path in DOTENV_CANDIDATES:
        try:
            if not path.is_file():
                continue
            for line in path.read_text(errors="ignore").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:]
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip("'\"")
                if key and key not in values:
                    values[key] = val
        except OSError:
            continue
    return values


def get(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val:
        return val
    val = _dotenv().get(name)
    if val:
        return val
    return default


def get_bool(name: str, default: bool = False) -> bool:
    raw = get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def get_int(name: str, default: int) -> int:
    raw = get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def llm_credentials() -> tuple[str | None, str | None, str]:
    """Return (api_key, base_url, provider).

    The key and the base URL must be chosen together. Sending an OpenAI key to
    the OpenPaths gateway earns a 401, which the circuit breaker then reads as
    a gateway outage - so pair them here rather than resolving each separately.
    """
    gateway_key = get("OPENPATHS_API_KEY")
    if gateway_key:
        base = get("OPENPATHS_BASE_URL", DEFAULT_BASE_URL) or DEFAULT_BASE_URL
        if base.rstrip("/") == "https://api.openpaths.io/v1":
            base = DEFAULT_BASE_URL
        return gateway_key, base, "openpaths"

    openai_key = get("OPENAI_API_KEY")
    if openai_key:
        # Direct OpenAI: its own base URL, and the gateway's model aliases
        # will not resolve, so callers fall back to public model names.
        return openai_key, get("OPENAI_BASE_URL"), "openai"

    return None, None, "none"


def openpaths_key() -> str | None:
    return llm_credentials()[0]


def openpaths_base_url() -> str:
    return llm_credentials()[1] or DEFAULT_BASE_URL


def using_gateway() -> bool:
    return llm_credentials()[2] == "openpaths"


def stripe_secret_key() -> str | None:
    return get("STRIPE_SECRET_KEY") or get("STRIPE_API_KEY")


def stripe_publishable_key() -> str | None:
    return get("STRIPE_PUBLISHABLE_KEY")


def stripe_webhook_secret() -> str | None:
    return get("STRIPE_WEBHOOK_SECRET")


def session_secret() -> str:
    return get("TWOHELIXES_SESSION_SECRET") or get("SESSION_SECRET") or "dev-insecure"


def data_dir() -> Path:
    path = Path(get("TWOHELIXES_DATA_DIR") or (REPO_ROOT / "var"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_dev() -> bool:
    return get_bool("TWOHELIXES_DEV", default=not bool(get("TWOHELIXES_PROD")))


def site_url() -> str:
    return get("TWOHELIXES_SITE_URL") or "https://twohelixes.com"
