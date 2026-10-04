"""Public regional maps, catalogue and downloads. No geocoding or data writes."""
import csv
import io
import math
from twohelixes import router
from twohelixes.datasets import world_schools as world


@router.get('/schools/world')
@router.get('/schools/region/{region}')
def school_world(ctx):
    key=ctx.params.get('region','us')
    try:world.region(key)
    except ValueError:return router.error(404,'unknown_school_region')
    return router.html((world.ROOT.parent/'world_schools.html').read_text())


@router.get('/v1/schools/world/regions')
def school_regions(ctx):
    return router.json_result({'regions':world.regions(),'scope':'Available datasets only; not worldwide completeness. Catchments and achievement data are separate.'})


@router.get('/v1/schools/world/data')
def school_view(ctx):
    try:
        key=ctx.q('region','us');zoom=int(ctx.q('zoom','5'))
        if not 0<=zoom<=18:raise ValueError()
        bbox=None
        if ctx.q('bbox'):
            bbox=[float(v) for v in ctx.q('bbox').split(',')]
            if len(bbox)!=4 or not all(math.isfinite(v) for v in bbox):raise ValueError()
            w,s,e,n=bbox
            if not (-180<=w<=180 and -180<=e<=180 and -90<=s<=n<=90):raise ValueError()
        return router.json_result(world.view(key,bbox,zoom,ctx.q('q','')[:200],ctx.q('sector','')[:100]))
    except ValueError:
        return router.error(400,'invalid_map_query','Use a listed region, zoom 0–18 and WGS84 west,south,east,north bounds.')


@router.get('/v1/schools/world/download/{region}')
def school_download(ctx):
    try:rows=world.rows_for(ctx.params['region'])
    except ValueError:return router.error(404,'unknown_school_region')
    if ctx.q('format')=='csv':
        output=io.StringIO();keys=sorted({k for row in rows for k in row})
        writer=csv.DictWriter(output,fieldnames=keys);writer.writeheader()
        # Protect spreadsheet users from source strings interpreted as formulas.
        for row in rows:
            writer.writerow({k:("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v) for k,v in row.items()})
        return router.Result(body=output.getvalue(),content_type='text/csv; charset=utf-8',headers={'Content-Disposition':'attachment; filename="schools.csv"'})
    return router.json_result({'metadata':world.region(ctx.params['region']),'schools':rows})
