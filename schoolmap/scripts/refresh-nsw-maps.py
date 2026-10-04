#!/usr/bin/env python3
"""Refresh saved agent map configurations from the complete directory, without an LLM call."""
import json
from twohelixes.datasets import schools
from twohelixes.charts import defaults
from twohelixes.pipeline import figures
from twohelixes.pipeline.orchestrator import _preview

frame = schools.frame('nsw')
for name in ('nsw_agent_map.json', 'nsw_agent_naplan_map.json'):
    path = schools.DIRECTORY/name
    payload = json.loads(path.read_text())
    config = payload['config']
    naplan = name == 'nsw_agent_naplan_map.json'
    config['color'] = 'reading_y3' if naplan else 'sector'
    config['title'] = f"All {len(frame):,} NSW school locations" + (' · Year 3 reading' if naplan else '')
    config['subtitle'] = 'Partial NAPLAN collection; grey markers have no collected reading score.' if naplan else 'Coloured by school sector; school names and addresses from the NSW directory.'
    config['reasoning'] = 'Retain every school location, including schools with missing scores. No aggregation or imputation.'
    payload['figure'], payload['warnings'] = figures.build(frame, config)
    payload['figure'] = defaults.apply(payload['figure'], chart_type='map')
    assert sum(len(t.get('lat', [])) for t in payload['figure']['data']) == len(frame)
    payload.update(preview=_preview(frame), columns=list(frame.columns), row_count=len(frame),
        transform_code='result = df.copy()', interpretation=config['title']+'. '+config['subtitle'],
        trace=[{'stage':'chart','thought':'Refreshed the saved agent map configuration with the complete directory. No new model call.'}],
        audit=[], elapsed_ms=0)
    pending = path.with_suffix('.tmp')
    pending.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, default=str))
    pending.replace(path)
    print(name, len(frame), 'locations')
