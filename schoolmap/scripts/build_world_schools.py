"""Normalize independently licensed school directories without inventing scores."""
import argparse
import csv
import hashlib
import io
import json
import math
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from collections import Counter


def number(v):
    try:
        n=float(v)
        return n if math.isfinite(n) else None
    except (ValueError,TypeError):
        return None


def row(sid,name,lat,lon,**extra):
    lat,lon=number(lat),number(lon)
    if lat is not None and not -90<=lat<=90:lat=None
    if lon is not None and not -180<=lon<=180:lon=None
    if lat==0 and lon==0:lat=lon=None
    return {'school_id':str(sid),'school_name':name,'latitude':lat,'longitude':lon,
            'achievement_score':None,'rank':None,**extra}


def publish(output,key,rows,source,license,year,scope,raw_paths):
    ids=[r['school_id'] for r in rows]
    if len(ids)!=len(set(ids)):
        raise ValueError(f'{key}: duplicate source identifiers')
    metadata={'key':key,'source_url':source,'license':license,'data_year':year,
              'scope':scope,'retrieved_at':datetime.now(timezone.utc).isoformat(),
              'school_count':len(rows),'mapped_count':sum(r['latitude'] is not None and r['longitude'] is not None for r in rows),
              'achievement_status':'not_collected','catchment_status':'not_collected',
              'regions':dict(sorted(Counter(r.get('region','') for r in rows).items())),
              'source_sha256':{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in raw_paths}}
    (output/(key+'.json')).write_text(json.dumps({'metadata':metadata,'schools':rows},ensure_ascii=False,separators=(',',':'),allow_nan=False))
    (output/(key+'.metadata.json')).write_text(json.dumps(metadata,ensure_ascii=False,separators=(',',':'),allow_nan=False))
    print(key,len(rows),metadata['mapped_count'])


def build(raw,output):
    output.mkdir(parents=True,exist_ok=True)
    path=raw/'us-public-2425.zip'
    with zipfile.ZipFile(path) as z:
        # NCES TXT has no header; documented field order is retained here.
        records=list(csv.reader(io.StringIO(z.read('EDGE_GEOCODE_PUBLICSCH_2425.TXT').decode('utf-8-sig')),delimiter='|'))
    rows=[]
    for r in records:
        if len(r)!=23:raise ValueError('Unexpected NCES layout')
        rows.append(row(r[0],r[2],r[12],r[13],country='US',region=r[6],state=r[6],sector='Public',school_type='Public elementary/secondary',address=r[4],suburb=r[5],postcode=r[7],district_id=r[1],data_year=r[22]))
    publish(output,'us',rows,'https://nces.ed.gov/programs/edge/geographic/schoollocations','Public domain · NCES EDGE','2024–25','US public elementary/secondary school locations, including reported territories; excludes private schools. Approximate NCES geocodes.',[path])
    path=raw/'nz-schools.json';d=json.loads(path.read_text());records=d['result']['records']
    if len(records)!=d['result']['total']:raise ValueError('Incomplete NZ directory')
    rows=[row(r['School_Id'],r['Org_Name'],r['Latitude'],r['Longitude'],country='NZ',region=r['Regional_Council'],state=r['Regional_Council'],sector=r['Authority'],school_type=r['Org_Type'],address=r['Add1_Line1'].strip(),suburb=r['Add1_City'],postcode=None,status=r['Status'],data_year='2026') for r in records]
    publish(output,'nz',rows,'https://www.educationcounts.govt.nz/directories/school-directory-api','CC BY 4.0 · New Zealand Ministry of Education','2026','NZ Ministry school directory snapshot. Postal codes are omitted because available codes describe mailing rather than physical addresses.',[path])
    path=raw/'vic-schools-2024.csv'
    with path.open(encoding='cp1252') as f:records=list(csv.DictReader(f))
    rows=[row(r['School_No']+':'+str(i),r['School_Name'],r['Y'],r['X'],country='AU',region='VIC',state='VIC',sector=r['Education_Sector'],school_type=r['School_Type'],address=' '.join(filter(None,[r['Address_Line_1'],r['Address_Line_2']])),suburb=r['Address_Town'],postcode=r['Address_Postcode'],source_school_code=r['School_No'],data_year='2024') for i,r in enumerate(records)]
    publish(output,'au-vic',rows,'https://discover.data.vic.gov.au/dataset/school-locations-2024','CC BY 4.0 · Victorian Department of Education','2024','Victorian school location records. Source school codes may repeat across campuses; record IDs preserve every source row.',[path])
    path=raw/'canada-odef.zip'
    with zipfile.ZipFile(path) as z:
        records=list(csv.DictReader(io.StringIO(z.read('ODEF_v3.0.1/odef_v3_0_1.csv').decode('utf-8-sig'))))
    rows=[]
    for r in records:
        if not any(r[k]=='1' for k in ('ISCED1','ISCED2','ISCED3')) and r['facility_type']!='K12':continue
        match=re.fullmatch(r'POINT\s*\(\s*([\d.\-]+)\s+([\d.\-]+)\s*\)',r['geometry'])
        lon,lat=match.groups() if match else (None,None)
        rows.append(row(r['unique_id'],r['facility_name'],lat,lon,country='CA',region=r['province_code'],state=r['province_code'],sector='Not specified',school_type=r['facility_type'],address=r['full_address'],suburb=r['addressLocality'],postcode=r['postalCode'],data_year=r['date_updated'],source_school_code=r['school_id'],district=r['authority_name']))
    publish(output,'ca',rows,'https://www150.statcan.gc.ca/n1/pub/37-26-0001/372600012022001-eng.htm','Open Government Licence – Canada · Statistics Canada','ODEF 3.0.1 (2025 correction)','Canadian facilities reporting primary/secondary ISCED levels or K12 type. Excludes unclassified/tertiary-only records. Source dates vary; not guaranteed exhaustive or current.',[path])
    path=raw/'nt-directory.json';directory=json.loads(path.read_text());rows=[];paths=[path]
    for item in directory:
        if not item.get('displayExternal'):continue
        code=item['itSchoolCode'];detail=raw/'nt-details'/(code+'.json')
        if not detail.exists():raise ValueError('NT collection incomplete: '+code)
        r=json.loads(detail.read_text());paths.append(detail);address=r.get('physicalAddress') or {}
        rows.append(row(code,r['name'],r.get('lat'),r.get('long'),country='AU',region='NT',state='NT',sector='Government' if item['isGovernment'] else 'Non-government',school_type=item['schoolType'],address=address.get('displayAddress'),suburb='',postcode=None,data_year='2026',preschool=item.get('isPreSchool',False)))
    publish(output,'au-nt',rows,'https://data.nt.gov.au/dataset/school-list','Creative Commons Attribution · NT Department of Education and Training','2026','Publicly displayed NT school and preschool directory entries. Missing coordinates remain unmapped; no geocoding guesses.',paths)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--raw',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();build(a.raw,a.output)
