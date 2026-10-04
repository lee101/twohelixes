import json
from twohelixes import router
from twohelixes.datasets import samples


def test_school_map_and_data():
    router.build()
    status, _, _, body = router.handle("GET", "/schools", "", "", "{}")
    assert int(status) == 200 and 'id="map"' in body
    status, _, _, body = router.handle("GET", "/v1/schools/data", "", "", "{}")
    assert int(status) == 200
    data = json.loads(body)
    assert len(data["schools"]) == 1774
    assert len({s["school_id"] for s in data["schools"]}) == 1774
    assert all(s["rank"] is None and s["achievement_score"] is None for s in data["schools"])
    assert samples.BY_KEY["queensland_schools"].build().shape[0] == 1774


def test_nsw_dataset_campus_coverage_and_agent_map():
    router.build()
    for url in ('/schools/nsw','/v1/schools/nsw/data','/v1/schools/nsw/results','/v1/schools/nsw/agent-map'):
        status, _, _, body = router.handle('GET',url,'','','{}')
        assert int(status) == 200
        if url.endswith('/data'):
            data=json.loads(body)
            assert len(data['schools'])==3402
            assert data['metadata']['result_school_count']==3198
            assert all(s['achievement_score'] is None for s in data['schools'] if s['head_campus_id'] != s['school_id'])
        if url.endswith('agent-map'):
            data=json.loads(body)
            assert sum(len(t.get('lat',[])) for t in data['figure']['data'])==3402
            assert data['figure']['layout']['map']['style']=='open-street-map'
    assert samples.BY_KEY['nsw_schools'].build().shape[0]==3402


def test_missing_school_fields_produce_browser_valid_preview_json():
    import pandas as pd
    from twohelixes.pipeline.orchestrator import _preview
    preview=_preview(pd.DataFrame({'address':[None,'A'], 'mean':[float('nan'),float('inf')]}))
    assert preview==[{'address':None,'mean':None},{'address':'A','mean':None}]
    json.dumps(preview,allow_nan=False)


def test_saved_naplan_map_is_strict_json_with_continuous_scores():
    from twohelixes.datasets import schools
    def invalid(value):raise AssertionError('Non-JSON numeric constant: '+value)
    data=json.loads((schools.DIRECTORY/'nsw_agent_naplan_map.json').read_text(),parse_constant=invalid)
    frame=schools.frame('nsw')
    traces=data['figure']['data']
    assert sum(len(t['lat']) for t in traces)==len(frame)
    scored=[t for t in traces if isinstance(t.get('marker',{}).get('color'),list)]
    neutral=[t for t in traces if not isinstance(t.get('marker',{}).get('color'),list)]
    assert sum(len(t['lat']) for t in scored)==int(frame['reading_y3'].notna().sum())
    assert sum(len(t['lat']) for t in neutral)==int(frame['reading_y3'].isna().sum())


def test_public_map_colour_controls_keep_full_directory():
    from twohelixes.datasets import schools
    router.build()
    frame=schools.frame('nsw')
    for colour in ('sector', 'reading_y3', 'numeracy_y3'):
        status, _, _, body=router.handle('POST','/v1/schools/nsw/map-config','',json.dumps({
            'chart_type':'map','x':'longitude','y':'latitude','color':colour}),'{}')
        assert int(status)==200,body
        data=json.loads(body)
        traces=data['figure']['data']
        assert sum(len(t['lat']) for t in traces)==len(frame)
        if colour=='sector':
            assert len({t['marker']['color'] for t in traces})==3
        else:
            assert traces[0]['name']=='No selected score'
            assert traces[0]['marker']['size']<traces[-1]['marker']['size']
            values=[v for t in traces if isinstance(t['marker'].get('color'),list) for v in t['marker']['color']]
            assert sorted(values)==sorted(frame[colour].dropna().tolist())
