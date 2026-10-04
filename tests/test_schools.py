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
