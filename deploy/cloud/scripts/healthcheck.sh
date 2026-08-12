#!/usr/bin/env sh
set -eu

python - <<'PY'
import json
from urllib.request import urlopen

with urlopen("http://127.0.0.1:8080/api/health/live", timeout=3) as response:
    payload = json.load(response)
if response.status != 200 or payload != {"status": "ok"}:
    raise SystemExit(1)
PY
