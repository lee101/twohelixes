"""Exercise the configured production model on the new NT dataset, without saving."""
from twohelixes.datasets import world_schools
from twohelixes.pipeline.orchestrator import run

frame=world_schools.frame('au-nt')
result=run('Count every Northern Territory directory record by sector and show a bar chart. Include all records, even if coordinates are missing.',{'nt_schools':frame})
assert result.figure, result.warnings
assert result.row_count==frame.sector.nunique(), result.data_preview
print('NT agent aggregation:',result.data_preview,'model calls:',result.model_calls,'warnings:',result.warnings)
