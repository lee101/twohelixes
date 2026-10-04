import json
import pytest
from twohelixes import router
from twohelixes.datasets import world_schools as world, samples


def test_source_counts_identifiers_and_missing_values():
    for key in world.DATASETS:
        d=world.dataset(key);rows=d['schools'];m=d['metadata']
        assert len(rows)==m['school_count']
        assert len({r['school_id'] for r in rows})==len(rows)
        assert all(r['achievement_score'] is None and r['rank'] is None for r in rows)
        assert sum(r['latitude'] is not None and r['longitude'] is not None for r in rows)==m['mapped_count']
        assert 'world_schools_'+key.replace('-','_') in samples.BY_KEY
    assert world.dataset('us')['metadata']['school_count']==102178
    assert world.dataset('nz')['metadata']['school_count']==2578
    assert world.dataset('au-nt')['metadata']['school_count']==272
    assert world.dataset('ca')['metadata']['school_count']>15000


@pytest.mark.parametrize('key',['us','us-ca','ca','au-nt','au-vic','nz'])
def test_clusters_account_for_every_location(key):
    for zoom in (2,6,12):
        d=world.view(key,zoom=zoom)
        assert sum(m['count'] for m in d['markers'])==d['in_view']==d['mapped_matches']
        assert len(d['markers'])<=2000
        assert len(d['schools'])<=40
    d=world.view(key,bbox=[0,0,1,1])
    assert d['in_view']==0 and d['markers']==[]


def test_search_and_api_validation():
    d=world.view('au-nt',query='Acacia Hill')
    assert d['total_matches']==1 and d['schools'][0]['school_id']=='acacisch'
    assert len(world.regions())>70
    router.build()
    for q in ('region=bad','region=us&bbox=nan,0,1,1','region=us&zoom=999','region=us&bbox=1,0,2'):
        status,_,_,_=router.handle('GET','/v1/schools/world/data',q,'','{}')
        assert int(status)==400
    status,_,_,body=router.handle('GET','/v1/schools/world/data','region=au-nt&q=Acacia','','{}')
    assert int(status)==200 and json.loads(body)['total_matches']==1
    status,_,_,body=router.handle('GET','/v1/schools/world/download/au-nt','format=csv','','{}')
    assert int(status)==200 and 'Acacia Hill School' in body
