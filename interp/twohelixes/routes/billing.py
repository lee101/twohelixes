"""Stripe checkout, webhooks and the credit balance.

Reuses the existing app.nz Stripe account and price IDs. Credits are granted
only from a verified webhook, never from the browser's success redirect - a
user who reloads the success page must not mint credits.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from twohelixes import auth, config, credits, entitlements, metering, router, store

log = logging.getLogger("twohelixes.routes.billing")

# Prepaid credit packs. 1 credit == 1 US cent, so `credits` == amount paid.
# Credits never expire: an expiry date earns a little revenue and costs the
# trust of exactly the customers who prepay.
CREDIT_PACKS = [
    {"id": "pack_10", "label": "$10", "amount_cents": 1000, "credits": 1100, "bonus": "10% bonus"},
    {"id": "pack_25", "label": "$25", "amount_cents": 2500, "credits": 2900, "bonus": "16% bonus"},
    {"id": "pack_100", "label": "$100", "amount_cents": 10000, "credits": 12500, "bonus": "25% bonus"},
]


def _plan_card(plan_id: str, name: str, blurb: str, features: list[str],
               cta: str, highlight: bool = False) -> dict[str, Any]:
    """A plan as the pricing page needs it, priced from one source.

    The allowances live in `config.PLAN_ALLOWANCES` because the gate reads them
    there; a page that hard-codes "500 charts" beside a gate that allows 300 is
    the kind of discrepancy that ends in a refund.
    """
    allowance = config.PLAN_ALLOWANCES[plan_id]
    cents = int(allowance["price_cents"])
    return {
        "id": plan_id,
        "name": name,
        "blurb": blurb,
        "price": "$0" if cents == 0 else f"${cents // 100}",
        "cadence": "" if cents == 0 else "per month",
        "included": {
            "chat_query": allowance["chat_query"],
            "deep_research": allowance["deep_research"],
            "notebook_minute": allowance["notebook_minute"],
            "seats": allowance["seats"],
        },
        "features": features,
        "cta": cta,
        "highlight": highlight,
    }


PLANS = [
    _plan_card(
        "free", "Free",
        "Enough to answer real questions, every month.",
        [
            f"{config.PLAN_ALLOWANCES['free']['chat_query']} AI charts a month, renewing",
            "Every chart form, every connector",
            "Unlimited manual edits - changing a chart never costs anything",
            "SVG, PNG, CSV and notebook export",
        ],
        "Start free",
    ),
    _plan_card(
        "plus", "Plus",
        "For one person who charts things most days.",
        [
            f"{config.PLAN_ALLOWANCES['plus']['chat_query']} AI charts a month",
            f"{config.PLAN_ALLOWANCES['plus']['notebook_minute'] // 60} hours of hosted "
            "machine time included, every month",
            "Dashboards and share links that need no account to read",
            "API access with your own key",
            f"Then {config.CREDIT_COST['chat_query']}c a chart from credits - never cut off mid-month",
        ],
        "Choose Plus",
        highlight=True,
    ),
    _plan_card(
        "pro", "Pro",
        "For the person the questions get forwarded to.",
        [
            f"{config.PLAN_ALLOWANCES['pro']['chat_query']} AI charts a month",
            f"{config.PLAN_ALLOWANCES['pro']['deep_research']} deep-research runs",
            f"{config.PLAN_ALLOWANCES['pro']['notebook_minute'] // 60} hours of hosted "
            "machine time, and GPUs by the minute",
            "Scheduled dashboard refresh",
            "Priority model routing",
        ],
        "Choose Pro",
    ),
    _plan_card(
        "team", "Team",
        "One workspace, one bill, five people.",
        [
            f"{config.PLAN_ALLOWANCES['team']['seats']} seats",
            f"{config.PLAN_ALLOWANCES['team']['chat_query']} AI charts a month, pooled",
            f"{config.PLAN_ALLOWANCES['team']['deep_research']} deep-research runs",
            f"{config.PLAN_ALLOWANCES['team']['notebook_minute'] // 60} hours of hosted "
            "machine time, pooled across the workspace",
            "Shared sources, dashboards and history",
            "Volume pricing on anything above the allowance",
        ],
        "Choose Team",
    ),
]


@router.get("/v1/billing/plans")
def plans(ctx: router.Context) -> router.Result:
    from twohelixes import machines

    identity = auth.identify(ctx)
    plan = getattr(identity, "plan", "") if identity else ""
    return router.json_result(
        {
            "plans": PLANS,
            "packs": CREDIT_PACKS,
            "publishable_key": config.stripe_publishable_key() or "",
            "costs": config.CREDIT_COST,
            # The machine table is served from the same place as the plans so
            # the included hours and the per-minute rate beside them cannot
            # disagree - they are read from one catalogue.
            "machines": machines.catalog_for(plan),
            "included_class": config.MACHINE_INCLUDED_CLASS,
        }
    )


@router.get("/v1/billing/balance")
def balance(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    summary = entitlements.summary(identity)
    included = summary["included"]["chat_query"]
    return router.json_result(
        {
            "credits": credits.balance(identity.user_id),
            "plan": identity.plan,
            "ledger": credits.ledger(identity.user_id, limit=50),
            "included": summary["included"],
            "period_end": summary["period_end"],
            "renews_in_days": summary["renews_in_days"],
            # Kept under the old name because the app header reads it: what a
            # free user has left this month is the same question it was asking.
            "free_queries_left": max(0, included["limit"] - included["used"]),
        }
    )


@router.get("/v1/usage")
def usage(ctx: router.Context) -> router.Result:
    """What the metered work is costing, itemised and auditable.

    Per-request charges are already visible in the ledger. Metered work is not
    self-explanatory there - eight rows of "notebook_minute" say nothing about
    which notebook, for how long, or whether it is still running - so the
    meters are reported as themselves, including anything open right now and
    the rate it is burning.
    """
    identity = auth.require(ctx)
    report = metering.usage(identity.user_id)
    plan = entitlements.summary(identity)
    return router.json_result(
        {
            **report,
            **plan,
            "rates": plan["prices"],
            "list_rates": dict(config.CREDIT_COST),
            # A balance that will not outlast what is already running is the
            # one number a caller needs before starting anything else.
            "minutes_of_headroom": (
                int(credits.balance(identity.user_id) / report["credits_per_minute_open"])
                if report["credits_per_minute_open"]
                else None
            ),
        }
    )


def _price_for_plan(plan_id: str) -> str:
    """The Stripe price for a plan, from the environment.

    Prices are not in the repository: the same code runs against test and live
    keys, and a hard-coded price id is the classic way to charge a real card a
    test amount.
    """
    if plan_id not in config.PAID_PLANS:
        return ""
    return config.get(f"TWOHELIXES_STRIPE_PRICE_{plan_id.upper()}", "") or ""


def _stripe() -> Any:
    import stripe

    key = config.stripe_secret_key()
    if not key:
        raise RuntimeError("Stripe is not configured")
    stripe.api_key = key
    return stripe


@router.post("/v1/billing/checkout")
def checkout(ctx: router.Context) -> router.Result:
    """Create a Checkout session.

    Defaults to Stripe's *embedded* mode, which mounts the payment form inside
    our own page: no bounce to stripe.com and back, so the user never loses
    context mid-purchase. Pass `embedded: false` for the hosted redirect, which
    is the fallback when Stripe.js cannot load.
    """
    identity = auth.require(ctx)
    pack_id = str(ctx.field("pack") or "")
    plan_id = str(ctx.field("plan") or "")
    price_id = str(ctx.field("price_id") or _price_for_plan(plan_id))
    embedded = bool(ctx.field("embedded", True))

    if plan_id and not price_id:
        return router.error(
            503,
            "plan_unavailable",
            f"No Stripe price is configured for the {plan_id} plan.",
        )

    try:
        stripe = _stripe()
    except RuntimeError as exc:
        return router.error(503, "billing_unavailable", str(exc))

    customer_id = _ensure_customer(stripe, identity)
    site = config.site_url()

    if price_id:
        mode = "subscription"
        line_items = [{"price": price_id, "quantity": 1}]
        # The plan travels on the session, so the webhook does not have to
        # reverse-engineer which subscription it just received from a price id.
        metadata = {
            "user_id": identity.user_id,
            "kind": "subscription",
            "plan": plan_id or "pro",
        }
    else:
        pack = next((p for p in CREDIT_PACKS if p["id"] == pack_id), None)
        if pack is None:
            return router.error(400, "unknown_pack")
        mode = "payment"
        line_items = [
            {
                "price_data": {
                    "currency": "usd",
                    "unit_amount": pack["amount_cents"],
                    "product_data": {
                        "name": f"twoHelixes API credits ({pack['credits']:,})"
                    },
                },
                "quantity": 1,
            }
        ]
        metadata = {
            "user_id": identity.user_id,
            "kind": "credits",
            "credits": str(pack["credits"]),
        }

    params: dict[str, Any] = {
        "mode": mode,
        "customer": customer_id,
        "line_items": line_items,
        "client_reference_id": identity.user_id,
        "metadata": metadata,
    }

    try:
        if embedded:
            params["ui_mode"] = "embedded"
            # Embedded mode takes a return_url instead of success/cancel; the
            # placeholder is substituted by Stripe.
            params["return_url"] = (
                f"{site}/billing/complete?session_id={{CHECKOUT_SESSION_ID}}"
            )
            session = stripe.checkout.Session.create(**params)
            return router.json_result(
                {
                    "mode": "embedded",
                    "client_secret": session.client_secret,
                    "id": session.id,
                }
            )

        params["success_url"] = f"{site}/app?billing=success"
        params["cancel_url"] = f"{site}/pricing?billing=cancelled"
        session = stripe.checkout.Session.create(**params)
        return router.json_result({"mode": "hosted", "url": session.url, "id": session.id})

    except Exception as exc:  # noqa: BLE001 - Stripe raises many shapes
        log.exception("checkout failed")
        return router.error(502, "checkout_failed", str(exc))


@router.get("/v1/billing/session/{session_id}")
def session_status(ctx: router.Context) -> router.Result:
    """Report on a finished embedded checkout.

    Credits are granted by the webhook, never here - a user who reloads this
    page must not be able to mint more. This only reports what happened so the
    UI can say something useful while the webhook lands.
    """
    identity = auth.require(ctx)
    try:
        stripe = _stripe()
        session = stripe.checkout.Session.retrieve(ctx.params["session_id"])
    except RuntimeError as exc:
        return router.error(503, "billing_unavailable", str(exc))
    except Exception as exc:  # noqa: BLE001
        return router.error(404, "unknown_session", str(exc))

    if (session.get("metadata") or {}).get("user_id") != identity.user_id:
        return router.error(403, "not_your_session")

    return router.json_result(
        {
            "status": session.get("status"),
            "payment_status": session.get("payment_status"),
            "credits": credits.balance(identity.user_id),
        }
    )


def _ensure_customer(stripe: Any, identity: Any) -> str:
    row = store.get_user(identity.user_id) or {}
    if row.get("stripe_id"):
        return str(row["stripe_id"])

    customer = stripe.Customer.create(
        email=identity.email or None, metadata={"user_id": identity.user_id}
    )
    store.touch_user(identity.user_id, stripe_id=customer.id)
    return customer.id


@router.post("/v1/billing/webhook")
def webhook(ctx: router.Context) -> router.Result:
    """Grant credits and activate plans. Signature is mandatory."""
    secret = config.stripe_webhook_secret()
    if not secret:
        return router.error(503, "webhook_not_configured")

    signature = ctx.header("stripe-signature")
    if not signature:
        return router.error(400, "missing_signature")

    try:
        import stripe

        event = stripe.Webhook.construct_event(ctx.raw_body, signature, secret)
    except Exception as exc:  # noqa: BLE001
        log.warning("webhook signature rejected: %s", exc)
        return router.error(400, "bad_signature")

    kind = event["type"]
    obj = event["data"]["object"]

    if kind == "checkout.session.completed":
        metadata = obj.get("metadata") or {}
        user_id = metadata.get("user_id") or obj.get("client_reference_id")
        if not user_id:
            return router.json_result({"ignored": "no user"})

        if metadata.get("kind") == "credits":
            amount = int(metadata.get("credits") or 0)
            if amount:
                # The session id is the idempotency key: Stripe can deliver a
                # webhook more than once.
                already = store.one(
                    "SELECT id FROM credit_ledger WHERE ref = ?", (obj["id"],)
                )
                if already is None:
                    credits.grant(user_id, amount, reason="purchase", ref=obj["id"])
        else:
            plan = str(metadata.get("plan") or "pro")
            _activate(user_id, plan, subscription=obj.get("subscription"))

    elif kind in ("customer.subscription.deleted", "customer.subscription.paused"):
        row = store.one(
            "SELECT id FROM users WHERE stripe_id = ?", (obj.get("customer"),)
        )
        if row:
            # Downgrade, do not wipe: unused credits were paid for separately
            # and survive the end of a subscription.
            _activate(str(row["id"]), "free")

    elif kind == "customer.subscription.updated":
        row = store.one(
            "SELECT id FROM users WHERE stripe_id = ?", (obj.get("customer"),)
        )
        if row:
            active = obj.get("status") in ("active", "trialing")
            plan = _plan_from_subscription(obj) if active else "free"
            _activate(str(row["id"]), plan, subscription=obj.get("id"))

    elif kind == "invoice.paid":
        # A renewal. The allowance period restarts here rather than drifting
        # from whenever the account was created, so what a subscriber gets each
        # month lines up with what they are billed for each month.
        row = store.one(
            "SELECT id, plan FROM users WHERE stripe_id = ?", (obj.get("customer"),)
        )
        if row:
            _activate(str(row["id"]), str(row["plan"] or "pro"), renew=True)

    return router.json_result({"received": True})


def _plan_from_subscription(obj: Any) -> str:
    """Which plan a subscription object represents.

    By price id, matched against the environment - the same mapping that
    created the checkout session, read in reverse.
    """
    try:
        price_id = obj["items"]["data"][0]["price"]["id"]
    except (KeyError, IndexError, TypeError):
        return "pro"
    for plan_id in config.PAID_PLANS:
        if _price_for_plan(plan_id) == price_id:
            return plan_id
    return "pro"


def _activate(user_id: str, plan: str, subscription: Any = None, renew: bool = False) -> None:
    """Put a user on a plan and start their allowance period now."""
    fields: dict[str, Any] = {"plan": plan}
    if subscription:
        fields["stripe_subscription"] = str(subscription)
    store.touch_user(user_id, **fields)
    store.execute(
        "UPDATE users SET plan_period_start = ?, plan_usage = ? WHERE id = ?",
        (time.time(), store.dump_json({}), user_id),
    )
    log.info("user %s is now on %s (renew=%s)", user_id, plan, renew)


@router.post("/v1/billing/api-key")
def api_key(ctx: router.Context) -> router.Result:
    """Issue the caller's API key.

    Any paid account: a subscriber's included charts are usable from the API
    as well as the app. Making the API credits-only meant a Plus customer who
    wanted to script one report had to buy a second product first.
    """
    identity = auth.require(ctx)
    if not identity.paid:
        return router.error(
            402,
            "upgrade_required",
            "The API needs a paid plan or credits. Both are self-serve.",
        )
    return router.json_result(
        {
            "api_key": auth.api_key_for(identity.user_id),
            "usage": "/v1/usage",
            "prices": entitlements.summary(identity)["prices"],
        }
    )
