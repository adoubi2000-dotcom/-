#!/usr/bin/env bash
set -euo pipefail
export AOTO_CRAWL_OUT=${AOTO_CRAWL_OUT:-aoto-official-cn-full}
export AOTO_MAX_PAGES=${AOTO_MAX_PAGES:-50}
export AOTO_DOWNLOAD_IMAGES=${AOTO_DOWNLOAD_IMAGES:-1}
export AOTO_DOWNLOAD_DOCS=${AOTO_DOWNLOAD_DOCS:-1}
export AOTO_DOWNLOAD_VIDEOS=${AOTO_DOWNLOAD_VIDEOS:-0}
python -m scrapy crawl aoto_cn_full -a out="$AOTO_CRAWL_OUT" -a max_pages="$AOTO_MAX_PAGES"
