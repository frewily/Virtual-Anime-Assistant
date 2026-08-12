#!/usr/bin/env bash
set -Eeuo pipefail

mode=${1:-startup}
base_url=${VAA_HEALTH_BASE_URL:-http://127.0.0.1:8080}

if [[ "$mode" != "startup" && "$mode" != "full" ]]; then
  echo "usage: verify-deployment.sh <startup|full>" >&2
  exit 2
fi

check_health() {
  local stage=$1
  local path=$2
  local expected=$3
  if ! python3 - "$base_url$path" "$expected" <<'PY'
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

url, expected = sys.argv[1:]
try:
    with urlopen(url, timeout=5) as response:
        payload = json.load(response)
except (HTTPError, URLError, TimeoutError, ValueError):
    raise SystemExit(1)
if response.status != 200 or payload != {"status": expected}:
    raise SystemExit(1)
PY
  then
    echo "deployment check failed: $stage" >&2
    return 1
  fi
}

check_health live /api/health/live ok
check_health ready /api/health/ready ready
if [[ "$mode" == "full" ]]; then
  # Expected wire value: {"status":"connected"}
  check_health onebot /api/health/onebot connected
fi
