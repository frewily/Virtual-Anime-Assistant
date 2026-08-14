#!/usr/bin/env bash
set -Eeuo pipefail

readonly target_sha=${1:-}
readonly bundle_file=${2:-}
readonly repo_root=/opt/virtual-anime-assistant/current
readonly repo_script_dir="$repo_root/deploy/cloud/scripts"
readonly compose_file="$repo_root/deploy/cloud/docker-compose.yml"
readonly import_script="/tmp/vaa-import-$target_sha.sh"
readonly lock_file=${VAA_DEPLOY_LOCK:-/opt/virtual-anime-assistant/deploy.lock}
readonly live_url=http://127.0.0.1:8080/api/health/live
readonly ready_url=http://127.0.0.1:8080/api/health/ready

if [[ ! "$target_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "usage: deploy.sh <40-character-commit-sha> <bundle-file>" >&2
  exit 2
fi
if [[ ! -x "$import_script" ]]; then
  echo "deployment bundle importer is missing" >&2
  exit 2
fi

exec 9>"$lock_file"
if ! flock -n 9; then
  echo "another deployment is running" >&2
  exit 3
fi

cd "$repo_root"
previous_sha=$(git rev-parse HEAD)
readonly previous_sha

"$import_script" "$target_sha" "$bundle_file" "$repo_root"

rollback_required=true

rollback() {
  local status=$?
  trap - ERR INT TERM
  if [[ "$rollback_required" == true ]]; then
    echo "deployment failed; starting rollback" >&2
    git checkout --detach "$previous_sha"
    docker compose -f "$compose_file" up -d --build
    "$repo_script_dir/verify-deployment.sh" startup
  fi
  exit "$status"
}
trap rollback ERR INT TERM

if docker compose -f "$compose_file" ps -q vaa-app \
  | grep -q .; then
  "$repo_script_dir/backup-sqlite.sh"
fi

git checkout --detach "$target_sha"
docker compose -f "$compose_file" up -d --build

deadline=$((SECONDS + 90))
until "$repo_script_dir/verify-deployment.sh" startup; do
  if (( SECONDS >= deadline )); then
    echo "deployment health deadline exceeded" >&2
    false
  fi
  sleep 3
done

rollback_required=false
trap - ERR INT TERM
echo "deployment healthy: $target_sha"
