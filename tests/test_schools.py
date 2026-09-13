import json
from twohelixes import router
from twohelixes.datasets import samples


def test_school_map_and_data():
    router.build()
    status, _, _, body = router.handle("GET", "/schools", "", "", "{}")
    assert int(status) == 200 and 'id="map"' in body
    status, _, _, body = router.handle("GET", "/v1/schools/data", "", "", "{}")
    assert int(status) == 200
    data = json.loads(body)
    assert len(data["schools"]) == 1774
    assert len({s["school_id"] for s in data["schools"]}) == 1774
    assert all(s["rank"] is None and s["achievement_score"] is None for s in data["schools"])
    assert samples.BY_KEY["queensland_schools"].build().shape[0] == 1774
