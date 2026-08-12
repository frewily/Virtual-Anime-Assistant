#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
compose_dir=$(cd -- "$script_dir/.." && pwd)

cd "$compose_dir"
docker compose exec -T vaa-app \
  python -m infrastructure.sqlite_backup \
  --output /data/backups \
  --keep 7
