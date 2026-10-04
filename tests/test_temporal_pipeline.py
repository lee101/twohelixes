"""Real query/edit handlers with deterministic model responses, no external services."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bench'))
from temporal_cases import assert_frame, cases
from twohelixes import llm, router
from twohelixes.pipeline import orchestrator, prompts, route, transform
from twohelixes.routes import query


@pytest.mark.parametrize('case', cases(), ids=lambda c: c.name)
@pytest.mark.parametrize('endpoint', ['query', 'edit'])
def test_temporal_query_and_edit(case, endpoint, monkeypatch):
    identity = SimpleNamespace(user_id='test', paid=False, signed_in=False, to_public=lambda: {})
    calls = []

    def model(prompt, *, system, **kwargs):
        calls.append((prompt, system))
        if system == prompts.TRANSFORM_SYSTEM:
            return {'code': case.code, 'expected_columns': ['date', 'value']}
        if system == prompts.EDIT_SYSTEM:
            return {'config_updates': case.config}
        return case.config

    monkeypatch.setattr(llm, 'json_call', model)
    monkeypatch.setattr(route, 'enabled', lambda: False)
    monkeypatch.setattr(query, '_guard', lambda *a: (identity, None))
    monkeypatch.setattr(query, '_charge', lambda *a: None)
    monkeypatch.setattr(query, '_safe_catalog', lambda *a: [])
    monkeypatch.setattr(query.auth, 'require', lambda *a: identity)
    monkeypatch.setattr(query.teams, 'can_write', lambda *a: True)
    monkeypatch.setattr(query.ratelimit, 'check', lambda *a: SimpleNamespace(allowed=True))
    monkeypatch.setattr(query.store, 'execute', lambda *a: None)
    row = {'query': 'Show raw amount over time', 'graph_args': json.dumps({'chart_type': 'line', 'x': 'date', 'y': 'amount'})}
    monkeypatch.setattr(query.store, 'one', lambda *a: row)
    monkeypatch.setattr(query, '_frame_for_chart', lambda *a: pd.DataFrame(case.data))
    ctx = router.Context('POST', '/v1/query', '', json.dumps({'q': case.question, 'data': case.data}), {}, user=identity)
    if endpoint == 'query':
        response = query.run_query(ctx)
    else:
        ctx.raw_body = json.dumps({'edit': case.question})
        ctx.params['chart_id'] = 'existing-chart'
        response = query.edit_chart(ctx)
    assert response.status == 200, response.body
    assert not response.body['warnings'], response.body['warnings']
    frame = pd.DataFrame(response.body['preview'])
    assert_frame(case, frame)
    assert response.body['config']['y'] == 'value'
    assert response.body['figure']['data']
    transform_prompt = next(p for p, s in calls if s == prompts.TRANSFORM_SYSTEM)
    assert case.question in transform_prompt
    if endpoint == 'edit':
        assert 'Requested change:' in transform_prompt
        assert response.body['chart_id'] == 'existing-chart'


def test_window_declarative_and_notebook_parity():
    frame = pd.DataFrame({'date': ['2024-01-02', '2024-01-01'], 'amount': [-10, 50]})
    steps = transform.normalise([{'type': 'window', 'params': {'time_column': 'date', 'column': 'amount', 'as': 'balance', 'opening_balance': 100}}])
    assert transform.validate(steps, list(frame)) == []
    actual, _ = transform.apply(frame, steps)
    namespace = {'df': frame.copy(), 'pd': pd}
    exec(transform.to_python(steps), namespace)
    pd.testing.assert_frame_equal(actual, namespace['result'])
    assert actual.balance.tolist() == [150, 140]


def test_saved_chart_retains_transactions_not_calculated_balances():
    source = pd.DataFrame({'date': ['2024-01-01', '2024-01-02'], 'amount': [100, -40]})
    result = orchestrator.PipelineResult(frame=pd.DataFrame({'value': [1100, 1060]}), source_frame=source)
    ctx = router.Context('POST', '/v1/query', '', '{}', {})
    assert json.loads(query._keepable_rows(ctx, result)) == source.to_dict('records')
