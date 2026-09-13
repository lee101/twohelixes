"""Teams and sharing.

Access control is the one area where a passing test is not enough: the tests
that matter are the ones asserting someone *cannot* do something.
"""

from __future__ import annotations

import time

import pytest

from twohelixes import auth, config, router, store
from twohelixes.routes import pages, teams


@pytest.fixture(autouse=True)
def schema():
    store.init()
    teams.ensure_schema()


def _user(email: str) -> str:
    return store.create_user(email)["id"]


def _chart(owner_id: str, title: str = "Chart") -> str:
    chart_id = store.new_id()
    store.execute(
        "INSERT INTO charts (id, user_id, title, spec, graph_args, created_at, updated_at) "
        "VALUES (?, ?, ?, '{}', '{}', ?, ?)",
        (chart_id, owner_id, title, time.time(), time.time()),
    )
    return chart_id


def _dashboard(owner_id: str) -> str:
    dashboard_id = store.new_id()
    store.execute(
        "INSERT INTO dashboards (id, user_id, title, layout, created_at, updated_at) "
        "VALUES (?, ?, 'Board', '[]', ?, ?)",
        (dashboard_id, owner_id, time.time(), time.time()),
    )
    return dashboard_id


def _team(owner_id: str, name: str = "Team") -> str:
    team_id = store.new_id()
    store.execute(
        "INSERT INTO teams (id, name, owner_id, created_at) VALUES (?, ?, ?, ?)",
        (team_id, name, owner_id, time.time()),
    )
    store.execute(
        "INSERT INTO team_members (team_id, user_id, role, added_at) "
        "VALUES (?, ?, 'owner', ?)",
        (team_id, owner_id, time.time()),
    )
    return team_id


# -- ownership -------------------------------------------------------------


def test_owner_can_read_and_write_their_own():
    owner = _user("owner-a@test.local")
    chart = _chart(owner)
    assert teams.can_read(owner, "chart", chart)
    assert teams.can_write(owner, "chart", chart)


def test_a_stranger_can_do_neither():
    owner = _user("owner-b@test.local")
    stranger = _user("stranger-b@test.local")
    chart = _chart(owner)
    assert not teams.can_read(stranger, "chart", chart)
    assert not teams.can_write(stranger, "chart", chart)


def test_anonymous_can_do_neither():
    owner = _user("owner-c@test.local")
    chart = _chart(owner)
    assert not teams.can_read(None, "chart", chart)
    assert not teams.can_write(None, "chart", chart)


# -- teams -----------------------------------------------------------------


def test_team_members_can_read_shared_work():
    owner = _user("owner-d@test.local")
    mate = _user("mate-d@test.local")
    team = _team(owner)
    chart = _chart(owner)

    assert not teams.can_read(mate, "chart", chart)

    store.execute(
        "INSERT INTO team_members (team_id, user_id, role, added_at) VALUES (?, ?, 'member', ?)",
        (team, mate, time.time()),
    )
    store.execute(
        "INSERT INTO team_objects (team_id, kind, object_id, added_at) VALUES (?, 'chart', ?, ?)",
        (team, chart, time.time()),
    )
    assert teams.can_read(mate, "chart", chart)
    assert teams.can_write(mate, "chart", chart)


def test_team_seat_limit_matches_the_plan():
    assert teams.TEAM_SEAT_LIMIT == config.PLAN_ALLOWANCES["team"]["seats"]


def test_a_viewer_can_read_but_not_write():
    """Membership is not write access; the role decides."""
    owner = _user("owner-e@test.local")
    viewer = _user("viewer-e@test.local")
    team = _team(owner)
    chart = _chart(owner)

    store.execute(
        "INSERT INTO team_members (team_id, user_id, role, added_at) VALUES (?, ?, 'viewer', ?)",
        (team, viewer, time.time()),
    )
    store.execute(
        "INSERT INTO team_objects (team_id, kind, object_id, added_at) VALUES (?, 'chart', ?, ?)",
        (team, chart, time.time()),
    )
    assert teams.can_read(viewer, "chart", chart)
    assert not teams.can_write(viewer, "chart", chart)


def test_membership_of_another_team_grants_nothing():
    owner = _user("owner-f@test.local")
    outsider = _user("outsider-f@test.local")
    team_a = _team(owner, "A")
    team_b = _team(outsider, "B")
    chart = _chart(owner)

    store.execute(
        "INSERT INTO team_objects (team_id, kind, object_id, added_at) VALUES (?, 'chart', ?, ?)",
        (team_a, chart, time.time()),
    )
    assert teams.role_in(team_b, outsider) == "owner"
    assert not teams.can_read(outsider, "chart", chart)


def test_dashboard_members_inherit_access_to_its_charts():
    owner = _user("owner-board@test.local")
    member = _user("member-board@test.local")
    viewer = _user("viewer-board@test.local")
    team = _team(owner)
    board = _dashboard(owner)
    chart = _chart(owner)
    store.execute("UPDATE charts SET dashboard_id = ? WHERE id = ?", (board, chart))
    store.execute(
        "INSERT INTO team_members (team_id, user_id, role, added_at) "
        "VALUES (?, ?, 'member', ?), (?, ?, 'viewer', ?)",
        (team, member, time.time(), team, viewer, time.time()),
    )
    store.execute(
        "INSERT INTO team_objects (team_id, kind, object_id, added_at) "
        "VALUES (?, 'dashboard', ?, ?)",
        (team, board, time.time()),
    )

    assert teams.can_read(member, "dashboard", board)
    assert teams.can_write(member, "dashboard", board)
    assert teams.can_read(member, "chart", chart)
    assert teams.can_write(member, "chart", chart)
    assert teams.can_read(viewer, "chart", chart)
    assert not teams.can_write(viewer, "chart", chart)


# -- share links -----------------------------------------------------------


def _share(owner_id: str, chart_id: str, expires_at=None, revoked=0) -> str:
    token = store.new_id()
    store.execute(
        "INSERT INTO shares (token, kind, object_id, owner_id, created_at, "
        "expires_at, revoked) VALUES (?, 'chart', ?, ?, ?, ?, ?)",
        (token, chart_id, owner_id, time.time(), expires_at, revoked),
    )
    return token


def test_a_share_token_grants_read_to_anyone():
    owner = _user("owner-g@test.local")
    chart = _chart(owner)
    token = _share(owner, chart)
    assert teams.can_read(None, "chart", chart, token=token)


def test_a_share_token_never_grants_write():
    """This is the whole point of separating read from write."""
    owner = _user("owner-h@test.local")
    chart = _chart(owner)
    token = _share(owner, chart)
    assert not teams.can_write(None, "chart", chart)
    stranger = _user("stranger-h@test.local")
    assert not teams.can_write(stranger, "chart", chart)


def test_a_revoked_token_stops_working():
    owner = _user("owner-i@test.local")
    chart = _chart(owner)
    token = _share(owner, chart, revoked=1)
    assert teams.resolve_share(token) is None
    assert not teams.can_read(None, "chart", chart, token=token)


def test_an_expired_token_stops_working():
    owner = _user("owner-j@test.local")
    chart = _chart(owner)
    token = _share(owner, chart, expires_at=time.time() - 60)
    assert teams.resolve_share(token) is None
    assert not teams.can_read(None, "chart", chart, token=token)


def test_a_token_only_opens_the_object_it_was_minted_for():
    """A token for one chart must not read another."""
    owner = _user("owner-k@test.local")
    mine = _chart(owner, "mine")
    other = _chart(owner, "other")
    token = _share(owner, mine)

    assert teams.can_read(None, "chart", mine, token=token)
    assert not teams.can_read(None, "chart", other, token=token)


def test_a_token_does_not_cross_object_kinds():
    owner = _user("owner-l@test.local")
    chart = _chart(owner)
    token = _share(owner, chart)
    # Same id, different kind: must not resolve.
    assert not teams.can_read(None, "dashboard", chart, token=token)


def test_a_made_up_token_resolves_to_nothing():
    assert teams.resolve_share("not-a-real-token") is None


def test_chart_share_page_opens_and_api_does_not_leak_kept_rows():
    owner = _user("owner-shared-page@test.local")
    chart = _chart(owner, "Shared graph")
    store.execute(
        "UPDATE charts SET rows_json = ?, trace = ? WHERE id = ?",
        ('[{"private_dimension":"secret"}]', '[{"thought":"private"}]', chart),
    )
    token = _share(owner, chart)
    ctx = router.build_context("GET", f"/share/{token}", "", "", "")
    ctx.params = {"token": token}

    page = pages.shared_page(ctx)
    assert page.status == 200
    assert f'data-share="{token}"' in page.body
    assert 'data-share-kind="chart"' in page.body

    payload = teams.read_share(ctx).body["object"]
    assert payload["title"] == "Shared graph"
    assert "rows_json" not in payload
    assert "trace" not in payload


def test_invite_link_reaches_the_app_and_acceptance_is_idempotent():
    owner = _user("invite-owner@test.local")
    invited = _user("invite-member@test.local")
    team = _team(owner, "Invitations")
    token = store.new_id()
    store.execute(
        "INSERT INTO team_invites (token, team_id, email, role, invited_by, "
        "created_at, expires_at) VALUES (?, ?, ?, 'member', ?, ?, ?)",
        (
            token,
            team,
            "invite-member@test.local",
            owner,
            time.time(),
            time.time() + 86400,
        ),
    )
    follow = router.build_context("GET", f"/join/{token}", "", "", "")
    follow.params = {"token": token}
    redirect = teams.follow_invite(follow)
    assert redirect.status == 302
    assert redirect.headers["Location"] == f"/app?join={token}"

    join = router.build_context("POST", f"/v1/teams/join/{token}", "", "{}", "")
    join.params = {"token": token}
    join.user = auth.Identity(user_id=invited, email="invite-member@test.local")
    assert teams.accept_invite(join).body["joined"] is True
    again = teams.accept_invite(join)
    assert again.body == {"joined": True, "already": True, "team_id": team}
