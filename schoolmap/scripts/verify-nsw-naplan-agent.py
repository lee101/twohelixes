import json
from pathlib import Path
from twohelixes.datasets import schools
from twohelixes.pipeline.orchestrator import run
frame=schools.frame('nsw')
question='Map NSW schools with available 2025 Year 3 reading results. Filter to rows where reading_y3 is not null. Use reading_y3 as the colour measure. Preserve school_name, address, suburb, postcode, latitude and longitude for hover labels. Do not aggregate, impute or include missing scores. This collection is partial, so do not call it a statewide ranking.'
r=run(question,{'nsw_schools':frame})
assert r.figure and r.model_calls>0
assert all(t['type']=='scattermap' for t in r.figure['data'])
assert sum(len(t.get('lat',[])) for t in r.figure['data'])==int(frame['reading_y3'].notna().sum())
assert any(isinstance(t.get('marker',{}).get('color'),list) for t in r.figure['data'])
Path('/tmp/nsw-agent-naplan-map.json').write_text(json.dumps(r.to_payload(),default=str,allow_nan=False))
print(json.dumps({'rows':r.row_count,'calls':r.model_calls,'config':r.config,'warnings':r.warnings}))
