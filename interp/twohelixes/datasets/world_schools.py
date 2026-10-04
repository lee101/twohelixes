"""Regional directory registry and viewport clustering for large public datasets."""
import json
import math
import re
from functools import lru_cache
from pathlib import Path

ROOT=Path(__file__).parent/'fixtures'/'world_schools'
DATASETS={'us':'United States · public schools','nz':'New Zealand','ca':'Canada · K–12 facilities',
          'au-nt':'Northern Territory','au-vic':'Victoria'}


@lru_cache(maxsize=5)
def dataset(key):
    if key not in DATASETS:raise ValueError('Unknown school dataset')
    return json.loads((ROOT/(key+'.json')).read_text())


def slug(value):
    return re.sub('[^a-z0-9]+','-',value.lower()).strip('-')


@lru_cache(maxsize=5)
def metadata(key):
    if key not in DATASETS:raise ValueError('Unknown school dataset')
    return json.loads((ROOT/(key+'.metadata.json')).read_text())


@lru_cache(maxsize=1)
def regions():
    rows=[]
    for key,title in DATASETS.items():
        m=metadata(key)
        rows.append({'key':key,'name':title,'dataset':key,'filter_region':None,**{k:m[k] for k in ('school_count','mapped_count','data_year','source_url','license','scope')},'map_url':'/schools/region/'+key,'sample_key':'world_schools_'+key.replace('-','_')})
        if key in ('us','ca','nz'):
            for region,count in m['regions'].items():
                if not region:continue
                subkey=key+'-'+slug(region)
                rows.append({'key':subkey,'name':title+' / '+region,'dataset':key,'filter_region':region,'school_count':count,'data_year':m['data_year'],'source_url':m['source_url'],'license':m['license'],'scope':m['scope'],'map_url':'/schools/region/'+subkey,'sample_key':'world_schools_'+key.replace('-','_')})
    return rows


def region(key):
    result=next((r for r in regions() if r['key']==key),None)
    if result is None:raise ValueError('Unknown school region')
    return result


def frame(key):
    import pandas as pd
    return pd.DataFrame(dataset(key)['schools'])


def rows_for(key):
    r=region(key)
    return [s for s in dataset(r['dataset'])['schools'] if not r['filter_region'] or s['region']==r['filter_region']]


def view(key,bbox=None,zoom=5,query='',sector=''):
    r=region(key);rows=rows_for(key)
    if query:
        q=query.casefold()
        rows=[s for s in rows if q in ' '.join(str(s.get(k) or '') for k in ('school_name','address','suburb','postcode','school_id')).casefold()]
    if sector:rows=[s for s in rows if s.get('sector')==sector]
    located=[s for s in rows if s['latitude'] is not None and s['longitude'] is not None]
    bounds=[min(s['longitude'] for s in located),min(s['latitude'] for s in located),max(s['longitude'] for s in located),max(s['latitude'] for s in located)] if located else None
    # Search bounds span the region, while marker/list results respect a supplied
    # viewport. The client omits bbox for the initial fit after changing a filter.
    visible=located
    if bbox:
        w,s,e,n=bbox
        visible=[r for r in located if s<=r['latitude']<=n and ((w<=r['longitude']<=e) if w<=e else (r['longitude']>=w or r['longitude']<=e))]
    groups={};scale=256*2**max(0,min(18,zoom));projected=[]
    for school in visible:
        lon=school['longitude'];lat=max(-85,min(85,school['latitude']))
        x=(lon+180)/360;y=(1-math.asinh(math.tan(math.radians(lat)))/math.pi)/2
        projected.append((x,y,school))
    while True:
        groups={}
        for x,y,school in projected:
            groups.setdefault((int(x*scale//48),int(y*scale//48)),[]).append(school)
        if len(groups)<=2000:break
        scale/=2
    markers=[]
    for group in groups.values():
        if len(group)==1:
            markers.append({'count':1,'latitude':group[0]['latitude'],'longitude':group[0]['longitude'],'school':group[0]})
        else:
            markers.append({'count':len(group),'latitude':sum(s['latitude'] for s in group)/len(group),'longitude':sum(s['longitude'] for s in group)/len(group)})
    return {'region':r,'total_matches':len(rows),'mapped_matches':len(located),'in_view':len(visible),
            'markers':markers,'schools':(visible if bbox else rows)[:40],'bounds':bounds,
            'sectors':sorted({s.get('sector','') for s in dataset(r['dataset'])['schools']}),
            'notice':'Location directory only. No achievement scores or school catchments in this dataset. Cluster counts include all matching locations; the list shows the first 40 records.'}
