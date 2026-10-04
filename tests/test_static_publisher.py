"""Publishing must not inherit a sibling application's storage bucket."""
import os
import runpy
from pathlib import Path
from unittest.mock import patch
from twohelixes.storage import r2


def test_static_publisher_has_explicit_bucket_even_with_shared_dotenv(tmp_path):
    for name in ('app.js','marketing.js','styles.css'):
        (tmp_path/name).write_text('test')
    (tmp_path/'.upload-in-progress').write_text('incomplete')
    buckets=[]
    def presign(*args,**kwargs):
        buckets.append(r2.bucket())
        return 'https://example.invalid/upload'
    script=Path(__file__).resolve().parents[1]/'scripts/publish-static.py'
    with patch.dict(os.environ,{'R2_BUCKET':'unrelated-app'}), \
         patch('sys.argv',[str(script),'--directory',str(tmp_path)]), \
         patch.object(r2,'presign',side_effect=presign), \
         patch('requests.put') as put:
        put.return_value.ok=True
        runpy.run_path(str(script),run_name='__main__')
    assert buckets==['twohelixesstatic']*3
