"""Cache the public NT directory details at a gentle single-request pace."""
import json
import time
from pathlib import Path
import requests

root=Path('/vfast/data/code/schoolmap/data/raw/world')
directory=json.loads((root/'nt-directory.json').read_text())
target=root/'nt-details'
target.mkdir(exist_ok=True)
for school in directory:
    if not school.get('displayExternal'):
        continue
    code=school['itSchoolCode']
    if not code.replace('-','').replace('_','').isalnum():
        raise ValueError('Unexpected public school code')
    path=target/(code+'.json')
    if path.exists():
        continue
    response=requests.get('https://directory.ntschools.net/api/School/GetSchool',params={'itSchoolCode':code},timeout=30)
    if response.status_code==429:
        raise SystemExit('Rate limited; stop and resume after the provider retry window')
    response.raise_for_status()
    record=response.json()
    if not isinstance(record,dict) or not record.get('name'):
        raise ValueError('Unexpected school response')
    path.write_text(json.dumps(record,ensure_ascii=False))
    print(code,flush=True)
    time.sleep(.5)
