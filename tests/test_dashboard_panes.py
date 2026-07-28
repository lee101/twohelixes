"""Building a dashboard pane by pane.

The dashboard grid could already draw tiles; what it could not do was gain one.
These are the parts that make a board assemblable without a model call:
attaching a chart to it, and keeping a chart built by hand rebuildable so its
tile can still be reconfigured and refreshed. Plus the theming rule that
decides whether a board saved in daylight is readable at night.
"""

from __future__ import annotations

import json
import time
from typing import Any

import pytest

from twohelixes import auth, router, store
from twohelixes.routes import builder as builder_routes
from twohelixes.routes import dashboards as dashboard_routes
from twohelixes.routes import query as query_routes


@pytest.fixture(autouse=True)
def schema():
    store.init()


def _identity(email: str) -> auth.Identity:
    user = store.create_user(email)
    return auth.Identity(user_id=user["id"], email=email, plan="free")


def _ctx(
    identity: auth.Identity | None,
    *,
    body: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
    query: str = "",
) -> router.Context:
    context = router.build_context(
        "POST", "/", query, json.dumps(body) if body is not None else "", ""
    )
    context.user = identity
    context.params = params or {}
    return context


def _dashboard(identity: auth.Identity, title: str = "Board") -> str:
    result = dashboard_routes.create_dashboard(_ctx(identity, body={"title": title}))
    return result.body["id"]


def _chart(identity: auth.Identity, title: str = "Chart") -> str:
    chart_id = store.new_id()
    now = time.time()
    store.execute(
        "INSERT INTO charts (id, user_id, title, spec, graph_args, created_at,"
        " updated_at) VALUES (?, ?, ?, '{}', '{}', ?, ?)",
        (chart_id, identity.user_id, title, now, now),
    )
    return chart_id


# -- attaching a chart -----------------------------------------------------


def test_attaching_a_chart_puts_it_in_the_layout():
    identity = _identity("attach@test.local")
    board, chart = _dashboard(identity), _chart(identity)

    result = dashboard_routes.add_chart(
        _ctx(identity, body={"chart_id": chart, "w": 2}, params={"dashboard_id": board})
    )
    assert result.status == 200

    payload = dashboard_routes.get_dashboard(
        _ctx(identity, params={"dashboard_id": board})
    ).body
    assert [tile["chart_id"] for tile in payload["layout"]] == [chart]
    assert payload["layout"][0]["w"] == 2
    assert [tile["id"] for tile in payload["charts"]] == [chart]


def test_attaching_twice_does_not_make_two_tiles():
    """"Add to dashboard" is a button people press again when unsure."""
    identity = _identity("twice@test.local")
    board, chart = _dashboard(identity), _chart(identity)
    body = {"chart_id": chart}

    dashboard_routes.add_chart(_ctx(identity, body=body, params={"dashboard_id": board}))
    dashboard_routes.add_chart(_ctx(identity, body=body, params={"dashboard_id": board}))

    payload = dashboard_routes.get_dashboard(
        _ctx(identity, params={"dashboard_id": board})
    ).body
    assert len(payload["layout"]) == 1


def test_attaching_someone_elses_chart_is_a_404():
    mine = _identity("mine@test.local")
    theirs = _identity("theirs@test.local")
    board = _dashboard(mine)
    result = dashboard_routes.add_chart(
        _ctx(mine, body={"chart_id": _chart(theirs)}, params={"dashboard_id": board})
    )
    assert result.status == 404


def test_a_board_stops_at_the_tile_limit():
    identity = _identity("limit@test.local")
    board = _dashboard(identity)
    for _ in range(dashboard_routes.MAX_TILES):
        dashboard_routes.add_chart(
            _ctx(
                identity,
                body={"chart_id": _chart(identity)},
                params={"dashboard_id": board},
            )
        )
    result = dashboard_routes.add_chart(
        _ctx(identity, body={"chart_id": _chart(identity)}, params={"dashboard_id": board})
    )
    assert result.status == 400
    assert result.body["error"] == "too_many_tiles"


def test_removing_a_tile_detaches_the_chart():
    """Dropping the layout entry alone is not a removal.

    The payload returns every chart whose `dashboard_id` points at the board,
    and the view places any it finds without a layout entry - so a tile that
    was removed and saved came straight back on the next load.
    """
    identity = _identity("remove@test.local")
    board, chart = _dashboard(identity), _chart(identity)
    dashboard_routes.add_chart(
        _ctx(identity, body={"chart_id": chart}, params={"dashboard_id": board})
    )

    dashboard_routes.remove_chart(
        _ctx(identity, params={"dashboard_id": board, "chart_id": chart})
    )

    payload = dashboard_routes.get_dashboard(
        _ctx(identity, params={"dashboard_id": board})
    ).body
    assert payload["layout"] == []
    assert payload["charts"] == []
    # The chart itself survives: it is only off this board.
    assert store.one("SELECT id FROM charts WHERE id = ?", (chart,)) is not None


def test_removing_a_tile_from_a_board_that_is_not_yours_is_a_404():
    mine = _identity("keep-mine@test.local")
    theirs = _identity("keep-theirs@test.local")
    board, chart = _dashboard(mine), _chart(mine)
    dashboard_routes.add_chart(
        _ctx(mine, body={"chart_id": chart}, params={"dashboard_id": board})
    )

    result = dashboard_routes.remove_chart(
        _ctx(theirs, params={"dashboard_id": board, "chart_id": chart})
    )
    assert result.status == 404
    payload = dashboard_routes.get_dashboard(
        _ctx(mine, params={"dashboard_id": board})
    ).body
    assert len(payload["layout"]) == 1


# -- a built chart stays editable ------------------------------------------


def _build_and_save(identity: auth.Identity, **extra: Any) -> str:
    """Save a chart through the builder, the way a manual pane is made."""
    source = {
        "sample": "iris",
        "steps": [],
        "config": {"chart_type": "bar", "x": "species", "y": "petal_length_cm", "agg": "mean"},
    }
    preview = builder_routes.preview(_ctx(identity, body=source))
    assert preview.body["ok"], preview.body
    saved = builder_routes.save(
        _ctx(identity, body={**source, "figure": preview.body["figure"], **extra})
    )
    return saved.body["chart_id"]


def test_a_built_chart_can_be_rebuilt_from_its_plan():
    """The descriptor, not a copy of the rows.

    Without it a chart built by hand could be looked at and nothing else: the
    manual controls, the tile editor and dashboard refresh all need a frame,
    and there was nothing left to rebuild one from.
    """
    identity = _identity("rebuild@test.local")
    chart_id = _build_and_save(identity)

    row = store.one("SELECT * FROM charts WHERE id = ?", (chart_id,))
    plan = store.load_json(row["trace"], {})
    assert plan["source"] == {"sample": "iris"}

    frame = query_routes._frame_for_chart(identity, row)
    assert frame is not None
    assert "species" in frame.columns


def test_a_built_chart_offers_its_columns_for_manual_control():
    identity = _identity("columns@test.local")
    chart_id = _build_and_save(identity)
    payload = query_routes.get_chart(_ctx(identity, params={"chart_id": chart_id})).body
    assert "petal_length_cm" in payload["columns"]
    # The kept rows are up to five thousand records; the response carries the
    # names, not the table.
    assert "rows_json" not in payload


def test_saving_into_a_dashboard_puts_the_chart_on_it():
    identity = _identity("into@test.local")
    board = _dashboard(identity)
    chart_id = _build_and_save(identity, dashboard_id=board)
    row = store.one("SELECT dashboard_id FROM charts WHERE id = ?", (chart_id,))
    assert row["dashboard_id"] == board


# -- theming ---------------------------------------------------------------


def test_a_tile_is_restyled_for_the_viewers_theme():
    """A board built in daylight must not be white plates on a dark page."""
    light = dashboard_routes._themed(
        {
            "data": [{"type": "bar", "x": ["a"], "y": [1]}],
            "layout": {"paper_bgcolor": "#ffffff", "plot_bgcolor": "#ffffff"},
        },
        "dark",
    )
    assert light["layout"]["paper_bgcolor"] != "#ffffff"
    assert light["layout"]["plot_bgcolor"] == light["layout"]["paper_bgcolor"]


def test_restyling_keeps_the_data_untouched():
    figure = {
        "data": [{"type": "bar", "x": ["a", "b"], "y": [1, 2]}],
        "layout": {"paper_bgcolor": "#ffffff"},
    }
    out = dashboard_routes._themed(figure, "dark")
    assert out["data"][0]["x"] == ["a", "b"]
    assert out["data"][0]["y"] == [1, 2]


def test_a_figure_with_no_data_is_returned_as_is():
    assert dashboard_routes._themed({"data": [], "layout": {}}, "dark") == {
        "data": [],
        "layout": {},
    }
