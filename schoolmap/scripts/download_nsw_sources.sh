#!/usr/bin/env bash
set -euo pipefail
raw="${SCHOOLMAP_RAW_DIR:-$(dirname "$0")/../data/raw/nsw}"
mkdir -p "$raw"
curl --fail --location --retry 3 'https://data.nsw.gov.au/data/dataset/78c10ea3-8d04-4c9c-b255-bbf8547e37e7/resource/3e6d5f6a-055c-440d-a690-fc0537c31095/download/master_dataset.csv' -o "$raw/nsw-government.csv"
curl --fail --location --retry 3 'https://dataandreporting.blob.core.windows.net/anrdataportal/Data-Access-Program/School%20Location%202025.xlsx' -o "$raw/acara-locations-2025.xlsx"
