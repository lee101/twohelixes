import json
import pandas as pd
import pytest
from twohelixes.datasets import catchments
from twohelixes.interpreter import tools
from twohelixes import router


def polygon(coords, name):
    return {'type':'Feature','properties':{'name':name,'year_levels':[3]},'geometry':{'type':'Polygon','coordinates':coords}}


def test_spatial_join_keeps_overlap_holes_edges_and_unmatched():
    outer=[[0,0],[4,0],[4,4],[0,4],[0,0]]
    hole=[[1,1],[2,1],[2,2],[1,2],[1,1]]
    other=[[3,0],[5,0],[5,4],[3,4],[3,0]]
    collection={'type':'FeatureCollection','features':[polygon([outer,hole],'A'),polygon([other],'B')]}
    points=pd.DataFrame({'longitude':[3.5,1.5,1,9,None], 'latitude':[2,1.5,1,9,0]})
    joined=catchments.spatial_join(points,collection)
    assert joined[joined.point_row==0].geo_name.tolist()==['A','B']
    assert joined[joined.point_row==1].geo_match_status.tolist()==['unmatched']
    assert joined[joined.point_row==2].geo_name.tolist()==['A']
    assert joined[joined.point_row==3].geo_match_status.tolist()==['unmatched']
    assert joined[joined.point_row==4].geo_match_status.tolist()==['invalid_coordinates']
    assert catchments.spatial_join(points,collection,year_level=7,how='inner').empty


def test_multipolygon_and_coordinate_validation():
    geom={'type':'MultiPolygon','coordinates':[[[[0,0],[1,0],[1,1],[0,1],[0,0]]],[[[5,5],[6,5],[6,6],[5,6],[5,5]]]]}
    assert catchments.geometry_covers(geom,5.5,5.5)
    assert not catchments.geometry_covers(geom,3,3)
    with pytest.raises(ValueError):
        catchments.spatial_join(pd.DataFrame({'longitude':[0],'latitude':[0]}),{'type':'FeatureCollection','crs':{'name':'EPSG:3857'},'features':[]})


def test_official_dataset_integrity_and_school_id_join():
    data=catchments.school_catchments(include_future=True)
    assert len(data['features'])==2191
    assert len({f['id'] for f in data['features']})==2191
    assert data['metadata']['matched_school_count']==2029
    assert all(f['geometry']['type'] in ('Polygon','MultiPolygon') for f in data['features'])
    active=catchments.school_catchments()
    assert len(active['features'])==2100
    assert not any(f['properties']['future'] for f in active['features'])
    # Abbotsford: My School SML ID is NOT NSW's school code 1001.
    schools=json.loads((catchments.DIRECTORY/'nsw_schools.json').read_text())['schools']
    school=next(s for s in schools if s['school_name']=='Abbotsford Public School')
    linked=catchments.school_catchments(school['school_id'])['features']
    assert linked and all(f['properties']['school_code']=='1001' for f in linked)
    assert not catchments.school_catchments('not-a-school')['features']


def test_geo_tools_available_to_agent_and_routes():
    assert tools.namespace()['spatial_join'] is catchments.spatial_join
    router.build()
    status,_,_,body=router.handle('GET','/v1/schools/nsw/catchments','school_id=invalid','','{}')
    assert int(status)==200 and json.loads(body)['features']==[]
    status,_,_,_=router.handle('GET','/v1/schools/nsw/catchments/lookup','longitude=nan&latitude=-33','','{}')
    assert int(status)==400
    status,_,_,body=router.handle('GET','/v1/schools/nsw/catchments.js','','','{}')
    assert int(status)==200 and 'highlightSchool' in body
