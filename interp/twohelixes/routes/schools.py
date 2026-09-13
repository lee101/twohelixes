from twohelixes import router
from twohelixes.datasets import schools


@router.get("/schools")
def school_map(ctx):
    return router.html(schools.map_html())


@router.get("/v1/schools/data")
def school_data(ctx):
    return router.json_result(schools.dataset())
