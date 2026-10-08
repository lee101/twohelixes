import json
import time

from test_analytics import SITE, collect, dispatch, fresh_db
from twohelixes import store
from twohelixes.analytics_api import compact_summary


def test_compact_empty_window():
    status, payload = dispatch('GET', '/v1/analytics/summary', query=f"site_id={SITE['id']}&days=30&compact=1")
    assert status == 200
    assert payload['totals']['sessions'] == payload['totals']['users'] == 0
    assert payload['totals']['bounce_rate'] == 0
    assert 'top_pages' not in payload


def test_compact_sessions_do_not_merge_different_clients():
    assert collect([
        {'event': 'page_view', 'client_id': 'one', 'session_id': 'shared'},
        {'event': 'page_view', 'client_id': 'one', 'session_id': 'shared'},
        {'event': 'page_view', 'client_id': 'two', 'session_id': 'shared'},
    ]) == 204
    payload = compact_summary(SITE['id'], time.time() - 86400, time.time() + 1)
    totals = payload['totals']
    assert totals['sessions'] == 2
    assert totals['engaged_sessions'] == 1
    assert totals['bounces'] == 1
    assert totals['bounce_rate'] == 0.5
    assert totals['page_views'] == 3


def test_compact_identity_stitching_and_sampling_weights(monkeypatch):
    assert collect([
        {'event': 'page_view', 'client_id': 'one', 'session_id': 'first', 'user_id': 'member'},
        {'event': 'page_view', 'client_id': 'two', 'session_id': 'second', 'user_id': 'member'},
    ]) == 204
    store.execute("UPDATE analytics_events SET sample_weight = 3 WHERE site_id = ? AND event_name = 'page_view'", (SITE['id'],))
    monkeypatch.setattr(store, 'rows_to_dicts', lambda *a: (_ for _ in ()).throw(AssertionError('compact reports must not fetch raw session rows')))
    totals = compact_summary(SITE['id'], time.time() - 86400, time.time() + 1)['totals']
    assert totals['users'] == 1
    assert totals['page_views'] == 6
    assert totals['sessions'] == 2


def test_compact_reporting_requires_account_authentication():
    status, _ = dispatch('GET', '/v1/analytics/summary', query=f"site_id={SITE['id']}&compact=1",
                         headers={'authorization': ''})
    assert status == 401


def test_compact_excludes_outside_window():
    assert collect([{'event': 'page_view', 'client_id': 'old', 'session_id': 'old'}]) == 204
    store.execute('UPDATE analytics_events SET ts = ? WHERE site_id = ?', (time.time() - 40 * 86400, SITE['id']))
    totals = compact_summary(SITE['id'], time.time() - 30 * 86400, time.time())['totals']
    assert totals['users'] == 0
    assert totals['observed_events'] == 0
