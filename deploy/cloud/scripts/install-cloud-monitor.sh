#!/usr/bin/env bash
set -Eeuo pipefail

if ! (( EUID == 0 )); then
  echo "run with sudo: $0" >&2
  exit 1
fi

readonly repo_root=/opt/virtual-anime-assistant/current
readonly cloud_dir="$repo_root/deploy/cloud"
readonly vaa_data_dir="$cloud_dir/data/vaa"
readonly backup_dir="$vaa_data_dir/backups"
readonly operations_dir="$vaa_data_dir/operations"

id vaa-deploy >/dev/null 2>&1
command -v setfacl >/dev/null 2>&1
test -d "$vaa_data_dir"
test -d "$backup_dir"

# The host monitor only traverses the VAA data root, reads backup metadata,
# and writes redacted state. The container UID only reads that state.
setfacl -m u:vaa-deploy:--x "$vaa_data_dir"
setfacl -m u:vaa-deploy:r-x "$backup_dir"
install -d -o vaa-deploy -g vaa-deploy -m 0750 "$operations_dir"
setfacl -m u:10001:r-x,d:u:10001:r-x "$operations_dir"

install -m 0644 "$cloud_dir/systemd/vaa-cloud-monitor.service" \
  /etc/systemd/system/vaa-cloud-monitor.service
install -m 0644 "$cloud_dir/systemd/vaa-cloud-monitor.timer" \
  /etc/systemd/system/vaa-cloud-monitor.timer
systemctl daemon-reload
systemctl enable --now vaa-cloud-monitor.timer

echo "cloud monitor installed"
