from twohelixes import router
from twohelixes.datasets import schools
from twohelixes.datasets import catchments


@router.get("/v1/schools/nsw/catchments.js")
def nsw_catchment_script(ctx):
    return router.Result(body=(schools.DIRECTORY/'catchments.js').read_text(), content_type='application/javascript; charset=utf-8')


@router.get("/v1/schools/nsw/catchments")
def nsw_catchments(ctx):
    return router.json_result(catchments.school_catchments(
        ctx.q('school_id') or None, display=ctx.q('detail')!='full',
        include_future=ctx.q('future')=='1'))


@router.get("/v1/schools/nsw/catchments/lookup")
def nsw_catchment_lookup(ctx):
    import math
    import pandas as pd
    try:
        longitude, latitude = float(ctx.q('longitude')), float(ctx.q('latitude'))
        year = int(ctx.q('year_level')) if ctx.q('year_level') else None
        if not math.isfinite(longitude) or not math.isfinite(latitude) or not -180<=longitude<=180 or not -90<=latitude<=90 or (year is not None and not 0<=year<=12):
            raise ValueError()
    except (ValueError,TypeError):
        return router.error(400,'invalid_coordinates','Supply valid WGS84 longitude, latitude and optional year_level 0–12.')
    data=catchments.school_catchments()
    matches=catchments.spatial_join(pd.DataFrame([{'longitude':longitude,'latitude':latitude}]),data,how='inner',year_level=year)
    return router.json_result({'matches':matches.to_dict('records'),'notice':data['metadata']['notice']})


@router.get("/schools")
def school_map(ctx):
    return router.html(schools.map_html())


@router.get("/v1/schools/data")
def school_data(ctx):
    return router.json_result(schools.dataset())


@router.get("/schools/nsw")
def nsw_map(ctx):
    return router.html(schools.map_html("nsw"))


@router.get("/v1/schools/nsw/data")
def nsw_data(ctx):
    return router.json_result(schools.dataset("nsw"))


@router.get("/v1/schools/nsw/results")
def nsw_results(ctx):
    return router.json_result(schools.naplan_results())


@router.get("/v1/schools/nsw/agent-map")
def nsw_agent_map(ctx):
    import json
    return router.json_result(json.loads((schools.DIRECTORY / "nsw_agent_map.json").read_text()))


@router.get("/v1/schools/nsw/agent-naplan-map")
def nsw_agent_naplan_map(ctx):
    import json
    return router.json_result(json.loads((schools.DIRECTORY / "nsw_agent_naplan_map.json").read_text()))


@router.post("/v1/schools/nsw/map-config")
def nsw_map_config(ctx):
    """Reconfigure the public example against all rows, without saving a chart."""
    from twohelixes.pipeline import figures
    from twohelixes.charts import defaults
    body = ctx.json
    if not isinstance(body, dict):
        return router.error(400, 'invalid_config')
    frame = schools.frame('nsw')
    config = figures.validate_config({k: body[k] for k in
        ('chart_type', 'x', 'y', 'color', 'z', 'size', 'agg', 'orientation', 'stacked') if k in body}, frame)
    config['title'] = 'NSW school locations' if config['chart_type'] == 'map' else 'NSW schools'
    config['subtitle'] = 'Colour: '+str(config.get('color') or 'none')+'. Missing scores remain unavailable.'
    figure, warnings = figures.build(frame, config)
    figure = defaults.apply(figure, chart_type=config['chart_type'], mode='dark' if body.get('mode') == 'dark' else 'light')
    return router.json_result({'figure': figure, 'config': config, 'warnings': warnings, 'audit': defaults.audit(figure)})
