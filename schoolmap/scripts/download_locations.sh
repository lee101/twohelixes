#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/raw
curl --fail --location --max-time 60 'https://www.data.qld.gov.au/api/3/action/datastore_search?resource_id=5b39065c-df32-415c-994c-5ff12f8de997&limit=10000' -o data/raw/qld_locations_2020.json
python3 scripts/build_dataset.py
