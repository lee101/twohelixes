#!/usr/bin/env python3
"""Build and atomically publish a checked NSW crawl snapshot; no model calls."""
import os
import shlex
import subprocess
from pathlib import Path
from build_nsw import build, write_json

REPO = Path(__file__).resolve().parents[2]
RAW = Path(os.environ.get('SCHOOLMAP_RAW_DIR', str(REPO/'schoolmap/data/raw/nsw')))
OUTPUT = REPO/'interp/twohelixes/datasets/fixtures'
HOST = os.environ.get('SCHOOLMAP_PUBLISH_HOST', '')

data, results = build(RAW, 2025, allow_partial=True)
write_json(OUTPUT/'nsw_schools.json', data)
write_json(OUTPUT/'nsw_naplan_results.json', {'results': results, 'metadata': data['metadata']})
if HOST:
    remote = '/nvme0n1-disk/code/twohelixes'
    stage = remote+'/var/nsw-import'
    subprocess.run(['ssh','-o','BatchMode=yes',HOST,'mkdir -p '+shlex.quote(stage)],check=True)
    subprocess.run(['rsync','-a',str(OUTPUT/'nsw_schools.json'),str(OUTPUT/'nsw_naplan_results.json'),HOST+':'+stage+'/'],check=True)
    activate = """
import json, pathlib, shutil
root=pathlib.Path('/nvme0n1-disk/code/twohelixes')
stage=root/'var/nsw-import'
a=json.loads((stage/'nsw_schools.json').read_text())
b=json.loads((stage/'nsw_naplan_results.json').read_text())
assert len(a['schools'])==a['metadata']['directory_count']
assert len({s['school_id'] for s in a['schools']})==len(a['schools'])
assert a['metadata']==b['metadata']
assert all(s['state']=='NSW' for s in a['schools'])
for name in ('nsw_schools.json','nsw_naplan_results.json'):
 target=root/'interp/twohelixes/datasets/fixtures'/name
 pending=target.with_suffix('.pending')
 shutil.copyfile(stage/name,pending)
 pending.replace(target)
from twohelixes.datasets import samples
for key in ('nsw_schools','nsw_naplan_results'):
 target=samples.path_for(key)
 target.parent.mkdir(parents=True,exist_ok=True)
 samples._publish_frame(samples.BY_KEY[key].build(),target)
print('Published NSW snapshot:', a['metadata']['responses_collected'], 'responses;',a['metadata']['scored_schools'],'indices')
"""
    cmd='cd '+shlex.quote(remote)+' && PYTHONPATH=interp .venv-13/bin/python -c '+shlex.quote(activate)
    subprocess.run(['ssh','-o','BatchMode=yes',HOST,cmd],check=True)
print('NSW snapshot:',data['metadata']['responses_collected'],'responses;',data['metadata']['scored_schools'],'indices',flush=True)
