"""Workbook persistence and the bounded Sheets agent protocol."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from twohelixes import auth, llm, router, store
from twohelixes.routes import sheets


@pytest.fixture(autouse=True)
def fresh_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TWOHELIXES_DATA_DIR", str(tmp_path))
    store.close()
    store._initialised = False
    store.init()
    yield
    store.close()
    store._initialised = False


def identity(email: str = "") -> auth.Identity:
    email = email or f"sheets-{store.new_id()}@test.local"
    user = store.create_user(email)
    return auth.Identity(user_id=user["id"], email=email, plan="free")


def context(
    method: str,
    path: str,
    user: auth.Identity,
    body: dict[str, Any] | None = None,
    **params: str,
) -> router.Context:
    ctx = router.build_context(method, path, "", json.dumps(body or {}), "{}")
    ctx.user = user
    ctx.params = params
    return ctx


def test_workbooks_are_persistent_and_owned() -> None:
    owner = identity()
    created = sheets.create_sheet(
        context(
            "POST",
            "/v1/sheets",
            owner,
            {"title": "Forecast", "workbook": sheets.DEFAULT_WORKBOOK},
        )
    )
    assert created.status == 201
    sheet_id = created.body["sheet"]["id"]

    fetched = sheets.get_sheet(
        context("GET", f"/v1/sheets/{sheet_id}", owner, sheet_id=sheet_id)
    )
    assert fetched.body["sheet"]["title"] == "Forecast"
    assert fetched.body["sheet"]["workbook"]["sheets"][0]["name"] == "Sheet1"

    stranger = identity("stranger@test.local")
    denied = sheets.get_sheet(
        context("GET", f"/v1/sheets/{sheet_id}", stranger, sheet_id=sheet_id)
    )
    assert denied.status == 404


def test_invalid_workbooks_are_rejected() -> None:
    result = sheets.create_sheet(
        context(
            "POST",
            "/v1/sheets",
            identity(),
            {"workbook": {"version": 1, "sheets": []}},
        )
    )
    assert result.status == 400
    assert result.body["error"] == "workbook_requires_sheets"


def test_views_are_persisted_and_bounded() -> None:
    workbook = {
        "version": 1,
        "sheets": [
            {
                "name": "Leads",
                "rows": 20,
                "cols": 4,
                "cells": {},
                "charts": [],
                "views": [{"name": "Open", "filter": "open", "sort": "B1", "direction": "desc"}],
            }
        ],
    }
    result = sheets.create_sheet(
        context("POST", "/v1/sheets", identity(), {"workbook": workbook})
    )
    assert result.status == 201
    saved = result.body["sheet"]["workbook"]["sheets"][0]["views"][0]
    assert saved == {"name": "Open", "filter": "open", "sort": "B1", "direction": "desc"}


def test_agent_output_is_reduced_to_safe_bounded_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = identity()

    def answer(*args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "reply": "Added totals and a chart.",
            "ops": [
                {
                    "op": "setCells",
                    "sheet": "missing",
                    "cells": [
                        {"ref": "$B$2", "formula": "=SUM(B3:B8)"},
                        {"ref": "not-a-cell", "value": "ignored"},
                        {"ref": "C3", "value": {"nested": "ignored"}},
                    ],
                },
                {
                    "op": "addChart",
                    "sheet": "Sheet1",
                    "type": "made-up",
                    "range": "$A$1:$B$8",
                    "title": "Revenue",
                },
                {"op": "deleteWorkbook"},
            ],
        }

    monkeypatch.setattr(llm, "json_call", answer)
    result = sheets.sheets_agent(
        context(
            "POST",
            "/v1/sheets/agent",
            user,
            {"instruction": "Add totals", "workbook": sheets.DEFAULT_WORKBOOK},
        )
    )

    assert result.status == 200
    assert result.body["ops"] == [
        {
            "op": "setCells",
            "sheet": "Sheet1",
            "cells": [{"ref": "B2", "formula": "SUM(B3:B8)"}],
        },
        {
            "op": "addChart",
            "sheet": "Sheet1",
            "type": "bar",
            "range": "A1:B8",
            "title": "Revenue",
        },
    ]


def test_agent_is_not_charged_when_the_model_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    user = identity()
    before = store.one("SELECT COUNT(*) AS n FROM rate_events")["n"]

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise llm.LLMError("offline")

    monkeypatch.setattr(llm, "json_call", fail)
    result = sheets.sheets_agent(
        context(
            "POST",
            "/v1/sheets/agent",
            user,
            {"instruction": "Add totals", "workbook": sheets.DEFAULT_WORKBOOK},
        )
    )
    assert result.status == 503
    usage = store.one("SELECT plan_usage FROM users WHERE id = ?", (user.user_id,))
    assert store.load_json(usage["plan_usage"], {}) == {}
    # Admission slots are reserved before model work. A failed call is not
    # billed, but it still counts against the short-lived bot/burst windows.
    row = store.one("SELECT COUNT(*) AS n FROM rate_events")
    assert row["n"] - before == 4
