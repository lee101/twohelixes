"""Published boundaries and deterministic overlap-preserving spatial joins.

Coordinates are WGS84 longitude/latitude, never inferred from column order.
School ownership is an identifier join, distinct from point containment.
"""
import json
import math
from functools import lru_cache
from pathlib import Path

DIRECTORY = Path(__file__).parent/'fixtures'


@lru_cache(maxsize=2)
def _dataset(display=False):
    suffix = '.display' if display else ''
    return json.loads((DIRECTORY/f'nsw_catchments{suffix}.geojson').read_text())


def school_catchments(school_id=None, *, state='nsw', include_future=False, display=False):
    """GeoJSON for a school's OWN zones, or all NSW zones. IDs are ACARA SML IDs."""
    if state.lower() != 'nsw':
        raise ValueError('Catchment data is available for NSW only')
    data = _dataset(display)
    features = [f for f in data['features'] if (include_future or not f['properties']['future'])
                and (school_id is None or str(school_id) in f['properties']['school_ids'])]
    # Do not let callers mutate the process-wide cached dataset.
    return json.loads(json.dumps({'type':'FeatureCollection','metadata':data['metadata'],'features':features}))


def _ring(x,y,ring):
    """Return outside=0, inside=1, or boundary=2 (holes handled separately)."""
    inside = False
    for a,b in zip(ring,ring[1:]):
        ax,ay=a[:2]; bx,by=b[:2]
        cross=(x-ax)*(by-ay)-(y-ay)*(bx-ax)
        if abs(cross)<=1e-12 and min(ax,bx)-1e-12<=x<=max(ax,bx)+1e-12 and min(ay,by)-1e-12<=y<=max(ay,by)+1e-12:
            return 2
        if (ay>y)!=(by>y) and x<(bx-ax)*(y-ay)/(by-ay)+ax:
            inside=not inside
    return int(inside)


def geometry_covers(geometry, longitude, latitude):
    """Polygon/MultiPolygon coverage including edges, excluding hole interiors."""
    kind=geometry.get('type')
    if kind not in ('Polygon','MultiPolygon'):
        raise ValueError('Spatial joins require Polygon or MultiPolygon GeoJSON')
    polygons=[geometry['coordinates']] if kind=='Polygon' else geometry['coordinates']
    for polygon in polygons:
        outer=_ring(longitude,latitude,polygon[0])
        if outer==2:
            return True
        if outer:
            holes=[_ring(longitude,latitude,hole) for hole in polygon[1:]]
            if 2 in holes or 1 not in holes:
                return True
    return False


def spatial_join(points, polygons, *, longitude='longitude', latitude='latitude', how='left', year_level=None):
    """Join a DataFrame to WGS84 GeoJSON. Keep ALL overlaps, holes, and unmatched rows.

    Right-hand properties are prefixed geo_; point_row is the positional input
    row identifier. Invalid coordinates are explicitly marked, not geocoded.
    year_level filters published year_levels when supplied (0=Kindergarten).
    """
    import pandas as pd
    if how not in ('left','inner'):
        raise ValueError('how must be left or inner')
    if longitude not in points or latitude not in points:
        raise ValueError('Explicit longitude and latitude columns are required')
    if len(points)>10000:
        raise ValueError('Join at most 10000 points per call')
    if any(str(c).startswith('geo_') or c=='point_row' for c in points.columns):
        raise ValueError('geo_ and point_row are reserved output columns')
    if polygons.get('type')!='FeatureCollection' or polygons.get('crs'):
        raise ValueError('Supply WGS84 GeoJSON FeatureCollection without another CRS')
    features=polygons['features']
    if len(features)>10000:
        raise ValueError('Join at most 10000 features per call')
    candidates=[]
    keys=set()
    for feature in features:
        properties=feature.get('properties') or {}
        if year_level is not None and year_level not in properties.get('year_levels',[]):
            continue
        geom=feature['geometry']
        if geom['type'] not in ('Polygon','MultiPolygon'):
            raise ValueError('Only polygon geometry is supported')
        rings=geom['coordinates'] if geom['type']=='Polygon' else [r for p in geom['coordinates'] for r in p]
        coords=[c for ring in rings for c in ring]
        if not coords or any(len(c)<2 or not math.isfinite(c[0]) or not math.isfinite(c[1]) or not -180<=c[0]<=180 or not -90<=c[1]<=90 for c in coords):
            raise ValueError('Polygon coordinates must be finite WGS84 longitude/latitude')
        bbox=(min(c[0] for c in coords),min(c[1] for c in coords),max(c[0] for c in coords),max(c[1] for c in coords))
        candidates.append((feature,bbox)); keys.update(properties)
    output=[]
    for index,row in enumerate(points.to_dict('records')):
        try:
            x,y=float(row[longitude]),float(row[latitude])
            valid=math.isfinite(x) and math.isfinite(y) and -180<=x<=180 and -90<=y<=90
        except (TypeError,ValueError):
            valid=False
        hits=[]
        if valid:
            for feature,(west,south,east,north) in candidates:
                if west<=x<=east and south<=y<=north and geometry_covers(feature['geometry'],x,y):
                    hits.append(feature)
        for feature in hits:
            output.append({**row,'point_row':index,**{'geo_'+k:v for k,v in feature.get('properties',{}).items()},'geo_match_status':'matched'})
        if not hits and how=='left':
            output.append({**row,'point_row':index,'geo_match_status':'unmatched' if valid else 'invalid_coordinates'})
    columns=list(points.columns)+['point_row']+['geo_'+k for k in sorted(keys)]+['geo_match_status']
    return pd.DataFrame(output,columns=list(dict.fromkeys(columns)))
