import json
from pathlib import Path
from twohelixes.datasets import schools
from twohelixes.pipeline.orchestrator import run
frame=schools.frame('nsw')
question='Map ALL NSW schools, including every school with missing 2025 Year 3 reading results. Keep all rows; do not filter out null scores. Use reading_y3 as the colour measure. Preserve school_name, address, suburb, postcode, latitude and longitude for hover labels. Do not aggregate or impute scores. Show schools with missing scores as neutral grey markers. This collection is partial, so do not call it a statewide ranking.'
r=run(question,{'nsw_schools':frame})
assert r.figure and r.model_calls>0
assert all(t['type']=='scattermap' for t in r.figure['data'])
assert sum(len(t.get('lat',[])) for t in r.figure['data'])==len(frame)
assert any(isinstance(t.get('marker',{}).get('color'),list) for t in r.figure['data'])
Path('/tmp/nsw-agent-naplan-map.json').write_text(json.dumps(r.to_payload(),default=str,allow_nan=False))
print(json.dumps({'rows':r.row_count,'calls':r.model_calls,'config':r.config,'warnings':r.warnings}))
