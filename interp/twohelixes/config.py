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

# The small, structured, well-bounded decisions - which chart form, which
# columns on which channels, what a one-line edit meant - run here. It is
# 0.14/0.28 per million against luna's 1.00/6.00, so on a query whose other
# calls write pandas and plan joins this is most of the saving and none of the
# risk: the output is a small JSON object that `figures.validate_config`
# repairs against the real frame anyway, so a worse answer degrades into the
# heuristic rather than into a wrong chart.
MODEL_MINI = "deepseek-v4-flash"

DEFAULT_BASE_URL = "https://openpaths.io/v1"

# --------------------------------------------------------------------------
# What things cost us, measured rather than guessed.
#
# `llm.measure()` prices every gateway call, and a run of the real pipeline
# over a 5,000-row frame gives:
#
#     simple question      2 calls   ~0.9 cents
#     with a join          3 calls   ~1.4 cents
#     with a transform     3 calls   ~2.1 cents
#
# So the p50 chart costs us about a cent and the p90 about two. The old price
# of 2 credits was under water on exactly the queries this product is proud of
# - the ones that join tables and write pandas. Prices below are set against
# the p90, not the median, because a bill that only works for easy questions
# is not a business.
#
# Deep research runs on sol at 5/30 per million and takes several steps; a
# typical run is 30-60 cents of model spend, a long one more, which is why it
# is a base fee plus metered minutes rather than a flat charge.
# --------------------------------------------------------------------------
MEASURED_COST_CENTS = {
    "chat_query_p50": 0.9,
    "chat_query_p90": 2.1,
    "dashboard_build": 4.0,
    "sql_generate": 0.3,
    "deep_research_minute": 4.0,
}

# Cost in API credits (1 credit == 1 US cent) for each billable operation.
CREDIT_COST = {
    "chat_query": 4,
    "dashboard_build": 12,
    "sql_generate": 1,
    "sql_autocomplete": 0,
    "structure_import": 1,
    "deep_research": 60,
    "notebook_minute": 1,
    "long_agent_minute": 8,
}

# --------------------------------------------------------------------------
# Hosted machines.
#
# `machines.py` derives every machine price from what the provider charges us,
# multiplied by this. The second copy of the cost is not profit: it pays for
# boot time the customer does not see, the minutes between a crashed owner and
# the reclaim sweep, provisions that fail after the instance exists, egress,
# and Stripe's cut of the credits that bought the minutes.
#
# `CREDIT_COST["notebook_minute"]` above stays as the price of the *included*
# class only, because the pricing page and older clients quote it.
# --------------------------------------------------------------------------
MACHINE_MARGIN = 2.0

# The class a plan's included machine minutes are sized against. Included hours
# are priced as this class; anything dearer is credits-only.
MACHINE_INCLUDED_CLASS = "cpu-small"

# --------------------------------------------------------------------------
# Plans.
#
# Included usage rather than pure metering, because metering anxiety is the
# thing that stops people asking the next question - and the next question is
# the product. The allowances are sized so a plan is comfortable at typical
# use and still profitable at full use: Plus at 500 charts costs us about $5
# against $19, Pro at its ceiling about $21 against $49.
#
# `notebook_minute` is the machine time included with the plan - the "machine
# in the subscription". It is denominated in minutes of the CPU class in
# `MACHINE_INCLUDED_CLASS`, which costs about 1.3c an hour, so 20 hours is 26
# cents: generous to read on a pricing page and immaterial next to the model
# spend. Idle sessions suspend, so an open tab does not drain it, and GPU
# classes never draw on it - one GPU hour costs more than the Plus plan.
#
# Overage does not cut anyone off. It draws on credits, and a plan without
# credits simply stops until the next period or a top-up, which is the
# behaviour people expect from a subscription rather than a surprise invoice.
# --------------------------------------------------------------------------
PLAN_ALLOWANCES = {
    "free": {
        "chat_query": 15,
        "deep_research": 0,
        "notebook_minute": 0,
        "price_cents": 0,
        "seats": 1,
    },
    "plus": {
        "chat_query": 500,
        "deep_research": 0,
        "notebook_minute": 1200,  # 20 hours
        "price_cents": 1900,
        "seats": 1,
    },
    "pro": {
        "chat_query": 1200,
        "deep_research": 10,
        "notebook_minute": 3600,  # 60 hours
        "price_cents": 4900,
        "seats": 1,
    },
    "team": {
        "chat_query": 5000,
        "deep_research": 40,
        "notebook_minute": 12000,  # 200 hours, pooled across seats
        "price_cents": 14900,
        "seats": 5,
    },
}

PAID_PLANS = ("plus", "pro", "team")

# The free allowance renews. A lifetime allowance of three meant a new user hit
# a wall on their first afternoon and never came back; a monthly one costs us
# about fifteen cents a head and gives the habit somewhere to form.
FREE_PERIOD_DAYS = 30

# --------------------------------------------------------------------------
# The trial, and the bot gate.
#
# Anonymous callers used to get nothing that costs a model call. That is a
# sound gate and a bad funnel: it asks for an account before the product has
# ever done anything. One question, on our sample data only, rate limited by
# address, is enough to show the thing working and far too little to farm.
# --------------------------------------------------------------------------
ANON_QUERIES_PER_DAY = 1
ANON_SAMPLE_DATA_ONLY = True

# Heavy per-identity limits for free users. Paid callers bypass these.
FREE_RATE_PER_MINUTE = 3
FREE_RATE_PER_HOUR = 12
FREE_RATE_PER_DAY = 25

# Global safety valve, applied to every identity including paid, to bound a
# single account's ability to saturate the box.
HARD_RATE_PER_MINUTE = 120

# --------------------------------------------------------------------------
# API volume pricing.
#
# The same credits, discounted automatically by trailing-30-day spend, so a
# growing customer's unit price falls without a negotiation, a new SKU or a
# sales call. Applied at charge time and reported by /v1/usage, so a caller can
# always see the tier they are in and what the next one is worth.
# --------------------------------------------------------------------------
VOLUME_TIERS = (
    (0, 0.0),
    (5_000, 0.10),
    (25_000, 0.20),
    (100_000, 0.30),
)


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
