"""Stripe checkout, webhooks and the credit balance.

Reuses the existing app.nz Stripe account and price IDs. Credits are granted
only from a verified webhook, never from the browser's success redirect - a
user who reloads the success page must not mint credits.
"""

from __future__ import annotations

import logging
from typing import Any

from twohelixes import auth, config, credits, router, store

log = logging.getLogger("twohelixes.routes.billing")

# Prepaid credit packs. 1 credit == 1 US cent, so `credits` == amount paid.
CREDIT_PACKS = [
    {"id": "pack_10", "label": "$10", "amount_cents": 1000, "credits": 1100, "bonus": "10% bonus"},
    {"id": "pack_25", "label": "$25", "amount_cents": 2500, "credits": 2900, "bonus": "16% bonus"},
    {"id": "pack_100", "label": "$100", "amount_cents": 10000, "credits": 12500, "bonus": "25% bonus"},
]

PLANS = [
    {
        "id": "free",
        "name": "Free",
        "price": "$0",
        "cadence": "",
        "features": [
            f"{config.FREE_QUERIES_PER_USER} AI chart queries",
            "Manual dashboards and SQL editor",
            "All data connectors",
            "CSV, SVG and PNG export",
        ],
        "cta": "Start free",
    },
    {
        "id": "credits",
        "name": "Pay as you go",
        "price": "from $10",
        "cadence": "one-off",
        "features": [
            "Unlimited chat charts while credits last",
            "No rate limits",
            "Long-running agents and deep research",
            "API access with your own key",
        ],
        "cta": "Buy credits",
        "highlight": True,
    },
    {
        "id": "pro",
        "name": "Pro",
        "price": "$29",
        "cadence": "per month",
        "features": [
            "Everything in pay as you go",
            "Scheduled dashboard refresh",
            "Shared dashboards and team links",
            "Priority model routing",
        ],
        "cta": "Go Pro",
    },
]


@router.get("/v1/billing/plans")
def plans(ctx: router.Context) -> router.Result:
    return router.json_result(
        {
            "plans": PLANS,
            "packs": CREDIT_PACKS,
            "publishable_key": config.stripe_publishable_key() or "",
            "costs": config.CREDIT_COST,
        }
    )


@router.get("/v1/billing/balance")
def balance(ctx: router.Context) -> router.Result:
    identity = auth.require(ctx)
    return router.json_result(
        {
            "credits": credits.balance(identity.user_id),
            "plan": identity.plan,
            "ledger": credits.ledger(identity.user_id, limit=50),
            "free_queries_left": max(
                0, config.FREE_QUERIES_PER_USER - identity.free_queries_used
            ),
        }
    )


def _stripe() -> Any:
    import stripe

    key = config.stripe_secret_key()
    if not key:
        raise RuntimeError("Stripe is not configured")
    stripe.api_key = key
    return stripe


@router.post("/v1/billing/checkout")
def checkout(ctx: router.Context) -> router.Result:
    """Create a Checkout session for a credit pack or a subscription."""
    identity = auth.require(ctx)
    pack_id = str(ctx.field("pack") or "")
    price_id = str(ctx.field("price_id") or "")

    try:
        stripe = _stripe()
    except RuntimeError as exc:
        return router.error(503, "billing_unavailable", str(exc))

    customer_id = _ensure_customer(stripe, identity)

    try:
        if price_id:
            session = stripe.checkout.Session.create(
                mode="subscription",
                customer=customer_id,
                line_items=[{"price": price_id, "quantity": 1}],
                success_url=f"{config.site_url()}/app?billing=success",
                cancel_url=f"{config.site_url()}/pricing?billing=cancelled",
                client_reference_id=identity.user_id,
                metadata={"user_id": identity.user_id, "kind": "subscription"},
            )
        else:
            pack = next((p for p in CREDIT_PACKS if p["id"] == pack_id), None)
            if pack is None:
                return router.error(400, "unknown_pack")
            session = stripe.checkout.Session.create(
                mode="payment",
                customer=customer_id,
                line_items=[
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
                ],
                success_url=f"{config.site_url()}/app?billing=success",
                cancel_url=f"{config.site_url()}/pricing?billing=cancelled",
                client_reference_id=identity.user_id,
                metadata={
                    "user_id": identity.user_id,
                    "kind": "credits",
                    "credits": str(pack["credits"]),
                },
            )
    except Exception as exc:  # noqa: BLE001 - Stripe raises many shapes
        log.exception("checkout failed")
        return router.error(502, "checkout_failed", str(exc))

    return router.json_result({"url": session.url, "id": session.id})


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
            store.touch_user(user_id, plan="pro")

    elif kind in ("customer.subscription.deleted", "customer.subscription.paused"):
        row = store.one(
            "SELECT id FROM users WHERE stripe_id = ?", (obj.get("customer"),)
        )
        if row:
            store.touch_user(row["id"], plan="free")

    elif kind == "customer.subscription.updated":
        row = store.one(
            "SELECT id FROM users WHERE stripe_id = ?", (obj.get("customer"),)
        )
        if row:
            active = obj.get("status") in ("active", "trialing")
            store.touch_user(row["id"], plan="pro" if active else "free")

    return router.json_result({"received": True})


@router.post("/v1/billing/api-key")
def api_key(ctx: router.Context) -> router.Result:
    """Issue the caller's API key. Paid callers only - it bypasses free limits."""
    identity = auth.require(ctx)
    if not identity.paid:
        return router.error(
            402, "credits_required", "Add credits to use the API."
        )
    return router.json_result({"api_key": auth.api_key_for(identity.user_id)})
