#!/usr/bin/env bash
set -Eeuo pipefail

readonly target_sha=${1:-}
readonly lock_file=${VAA_DEPLOY_LOCK:-/opt/virtual-anime-assistant/deploy.lock}
readonly live_url=http://127.0.0.1:8080/api/health/live
readonly ready_url=http://127.0.0.1:8080/api/health/ready

if [[ ! "$target_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "usage: deploy.sh <40-character-commit-sha>" >&2
  exit 2
fi

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
compose_dir=$(cd -- "$script_dir/.." && pwd)
repo_root=$(git -C "$compose_dir" rev-parse --show-toplevel)
readonly script_dir compose_dir repo_root live_url ready_url

exec 9>"$lock_file"
if ! flock -n 9; then
  echo "another deployment is running" >&2
  exit 3
fi

cd "$repo_root"
previous_sha=$(git rev-parse HEAD)
readonly previous_sha
rollback_required=true

rollback() {
  local status=$?
  trap - ERR INT TERM
  if [[ "$rollback_required" == true ]]; then
    echo "deployment failed; starting rollback" >&2
    git checkout --detach "$previous_sha"
    docker compose -f deploy/cloud/docker-compose.yml up -d --build
    "$script_dir/verify-deployment.sh" startup
  fi
  exit "$status"
}
trap rollback ERR INT TERM

if docker compose -f deploy/cloud/docker-compose.yml ps -q vaa-app \
  | grep -q .; then
  "$script_dir/backup-sqlite.sh"
fi

git fetch origin "$target_sha"
git checkout --detach "$target_sha"
docker compose -f deploy/cloud/docker-compose.yml up -d --build

deadline=$((SECONDS + 90))
until "$script_dir/verify-deployment.sh" startup; do
  if (( SECONDS >= deadline )); then
    echo "deployment health deadline exceeded" >&2
    false
  fi
  sleep 3
done

rollback_required=false
trap - ERR INT TERM
echo "deployment healthy: $target_sha"
