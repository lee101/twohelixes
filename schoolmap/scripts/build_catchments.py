#!/usr/bin/env python3
"""Convert NSW official intake zones, preserving separate overlapping records.

Build-only dependencies: pyshp, shapely, pyproj, pandas, openpyxl.
"""
import argparse
import csv
import hashlib
import io
import json
from collections import defaultdict, Counter
from datetime import datetime, timezone
from pathlib import Path
import zipfile

import pandas as pd
import shapefile
from pyproj import Transformer
from shapely.geometry import shape, mapping
from shapely.ops import transform

SOURCE = 'https://data.nsw.gov.au/data/dataset/nsw-education-school-intake-zones-catchment-areas-for-nsw-government-schools'


def build(archive, raw, schools_path, output):
    schools = {s['school_id']: s for s in json.loads(schools_path.read_text())['schools']}
    locations = pd.read_excel(raw/'acara-locations-2025.xlsx', sheet_name='SchoolLocations 2025')
    age_to_sml = defaultdict(set)
    for _, row in locations.iterrows():
        if pd.notna(row['Location AGE ID']) and pd.notna(row['ACARA SML ID']):
            sid = str(int(row['ACARA SML ID']))
            if sid in schools and schools[sid]['sector'] == 'Government':
                age_to_sml[str(int(row['Location AGE ID']))].add(sid)
    codes = {}
    with (raw/'nsw-government.csv').open(encoding='utf-8-sig') as handle:
        for row in csv.DictReader(handle):
            ids = sorted(age_to_sml[row['AgeID']])
            # Ambiguous identifier mappings remain unmatched, never guessed.
            codes[row['School_code']] = (ids if len(ids) == 1 else [], row['School_name'])
    features, preview, invalid = [], [], []
    with zipfile.ZipFile(archive) as z:
        enrolment_year = json.loads(z.read('catchment_sf_info.json'))['current_enrolment_year']
        source_dates = []
        for category in ('primary', 'secondary', 'future'):
            stem = 'catchments_' + category
            source_dates.append(z.getinfo(stem+'.shp').date_time[:3])
            projection = z.read(stem+'.prj').decode()
            converter = Transformer.from_crs(projection, 'EPSG:4326', always_xy=True)
            reader = shapefile.Reader(**{ext: io.BytesIO(z.read(stem+'.'+ext)) for ext in ('shp', 'dbf', 'shx')})
            for index, record in enumerate(reader.iterShapeRecords()):
                attrs = record.record.as_dict()
                geometry = transform(converter.transform, shape(record.shape.__geo_interface__))
                if geometry.is_empty or not geometry.is_valid or geometry.geom_type not in ('Polygon','MultiPolygon'):
                    invalid.append(f'{category}:{index}')
                    continue
                code = attrs['USE_ID'].strip()
                ids, name = codes.get(code, ([], attrs['USE_DESC']))
                props = {'catchment_id': f'nsw:{category}:{code}:{index}', 'school_code': code,
                         'school_ids': ids, 'school_name': name, 'source_name': attrs['USE_DESC'],
                         'category': category, 'catchment_type': attrs['CATCH_TYPE'],
                         'year_levels': [y for y in range(13) if attrs.get('KINDERGART' if y==0 else f'YEAR{y}')=='Y'],
                         'priority': attrs['PRIORITY'], 'source_add_date': attrs['ADD_DATE'],
                         'enrolment_year': enrolment_year, 'future': category=='future',
                         'join_method': 'school_code_to_age_id_to_acara_sml_id' if ids else 'unmatched'}
                feature = {'type':'Feature','id':props['catchment_id'],'properties':props,
                           'bbox':list(geometry.bounds),'geometry':mapping(geometry)}
                features.append(feature)
                def rounded(value):
                    if isinstance(value,(list,tuple)):
                        return [rounded(v) for v in value]
                    return round(value,6)
                display_geometry=mapping(geometry.simplify(0.0001, preserve_topology=True))
                display_geometry['coordinates']=rounded(display_geometry['coordinates'])
                preview.append({**feature, 'geometry':display_geometry})
    if invalid:
        raise ValueError(f'Invalid source polygons require review: {invalid}')
    metadata = {'source_url':SOURCE, 'attribution':'NSW Department of Education; modified by Twohelixes',
                'license':'Creative Commons Attribution', 'license_url':'http://www.opendefinition.org/licenses/cc-by',
                'retrieved_at':datetime.now(timezone.utc).isoformat(), 'enrolment_year':enrolment_year,
                'source_file_date':'-'.join(map(lambda n:f'{n:02d}',max(source_dates))),
                'source_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
                'crs':'EPSG:4326', 'source_crs':'EPSG:4283',
                'display_simplification_degrees':0.0001,
                'feature_count':len(features), 'categories':dict(Counter(f['properties']['category'] for f in features)),
                'matched_school_count':len({s for f in features for s in f['properties']['school_ids']}),
                'unmatched_features':sum(not f['properties']['school_ids'] for f in features),
                'notice':'2026 source snapshot, not an enrolment guarantee. Confirm with NSW School Finder. Overlapping zones, year levels and priority are retained. Future zones are excluded by default. No boundary does not mean ineligible.'}
    output.mkdir(parents=True, exist_ok=True)
    for suffix, rows in (('',features),('.display',preview)):
        (output/f'nsw_catchments{suffix}.geojson').write_text(json.dumps({'type':'FeatureCollection','metadata':metadata,'features':rows},separators=(',',':'),allow_nan=False))
    print(json.dumps(metadata, indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('--archive',type=Path,required=True)
    p.add_argument('--raw',type=Path,required=True)
    p.add_argument('--schools',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    build(a.archive,a.raw,a.schools,a.output)
