#!/usr/bin/env bash
set -Eeuo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

exec python3 "$script_dir/cloud_monitor.py" \
  --compose-file "$script_dir/../docker-compose.yml" \
  --public-state-file "$script_dir/../data/vaa/operations/cloud-monitor-state.json" \
  --private-state-file "$script_dir/../data/monitor/cloud-monitor-private.json" \
  --backup-directory "$script_dir/../data/vaa/backups"
