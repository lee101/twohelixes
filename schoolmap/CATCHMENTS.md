# NSW catchment boundaries

Source: NSW Department of Education, [intake zone dataset](https://data.nsw.gov.au/data/dataset/nsw-education-school-intake-zones-catchment-areas-for-nsw-government-schools), Creative Commons Attribution. Modified by Twohelixes.

The downloaded ZIP contains files dated 2026-09-03 and reports current enrolment year 2026. There are 1,657 primary, 443 secondary and 91 future records. All 2,191 geometries passed validity checks. Nineteen records have no verified school-ID link; no name or nearest-point guess is substituted. The verified identifier chain links 2,029 schools:

`catchment USE_ID → NSW School_code → AgeID → ACARA Location AGE ID → ACARA SML ID`

Only unique government-school crosswalk matches are accepted. Source zone names, year-level flags, priority, future/current status, dates and source checksum are retained. NSW source geometries use GDA94 / EPSG:4283; the build explicitly converts to WGS84 / EPSG:4326. Display outlines are simplified at 0.0001 degrees and rounded to six decimal places; school highlighting and spatial queries use unsimplified converted source geometry. These are indicative enrolment zones, not eligibility guarantees or 2025 NAPLAN cohorts. Confirm with the official School Finder.

## Rebuild

`scripts/build_catchments.py` accepts `--archive`, `--raw` (the NSW master CSV and ACARA location XLSX), `--schools`, and `--output`. Its build-only dependencies are pyshp, shapely, pyproj, pandas, and openpyxl. Production needs no new geospatial dependencies.

## APIs and agent helpers

- `/v1/schools/nsw/catchments`: display GeoJSON; `detail=full` returns full geometry, `school_id=` filters by verified ownership, `future=1` includes future zones.
- `/v1/schools/nsw/catchments/lookup?longitude=151.131206&latitude=-33.852728&year_level=3`: all matching current boundary records, not merely the top drawn polygon.
- `school_catchments(school_id=None, state='nsw', include_future=False)` is available inside the agent interpreter.
- `spatial_join(df, geojson, longitude='longitude', latitude='latitude', how='left', year_level=None)` works with arbitrary WGS84 Polygon/MultiPolygon FeatureCollections. It preserves all overlaps and unmatched point rows, handles holes and boundary edges, and prefixes zone properties with `geo_`. Join school metadata via `geo_school_ids`; do not infer ownership from the campus point.

Example: `result = spatial_join(df, school_catchments(), year_level=3)`.

Queensland's listed 2026 KML download returned an empty response during acquisition. No Queensland boundary geometry has been published. Existing school directory coverage is independent of catchment coverage.
