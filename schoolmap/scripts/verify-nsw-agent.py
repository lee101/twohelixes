import json
from pathlib import Path
from twohelixes.datasets import schools
from twohelixes.pipeline.orchestrator import run
question='Show all NSW school locations on a street map. Keep latitude, longitude, school_name, address, suburb and postcode for hover labels. Do not aggregate or colour by IDs, postcode or year. There are no aggregate achievement scores yet.'
r=run(question, {'nsw_schools': schools.frame('nsw')})
Path('/tmp/nsw-agent-map.json').write_text(json.dumps(r.to_payload(),default=str,allow_nan=False))
print(json.dumps({'rows':r.row_count,'config':r.config,'calls':r.model_calls,'warnings':r.warnings,'traces':[(t.get('type'),len(t.get('lat',[]))) for t in (r.figure or {}).get('data',[])]}),flush=True)
assert r.figure and any(t['type']=='scattermap' for t in r.figure['data'])
assert sum(len(t.get('lat',[])) for t in r.figure['data'])==3402
