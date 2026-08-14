#!/usr/bin/env bash
set -Eeuo pipefail

readonly target_sha=${1:-}
readonly bundle_file=${2:-}
readonly repo_root=${3:-}
readonly bundle_ref=refs/heads/vaa-deploy-target

if [[ ! "$target_sha" =~ ^[0-9a-f]{40}$ ]]; then
  echo "invalid deployment commit" >&2
  exit 2
fi

readonly expected_bundle="/tmp/vaa-deploy-$target_sha.bundle"
if [[ "$bundle_file" != "$expected_bundle" || ! -f "$bundle_file" ]]; then
  echo "invalid deployment bundle path" >&2
  exit 2
fi

git -C "$repo_root" rev-parse --git-dir >/dev/null
git -C "$repo_root" bundle verify "$bundle_file" >/dev/null
bundle_target=$(
  git -C "$repo_root" bundle list-heads "$bundle_file" "$bundle_ref" \
    | awk 'NR == 1 {print $1}'
)
readonly bundle_target
if [[ "$bundle_target" != "$target_sha" ]]; then
  echo "deployment bundle target mismatch" >&2
  exit 2
fi

git -C "$repo_root" fetch "$bundle_file" "$bundle_ref"
if [[ "$(git -C "$repo_root" rev-parse FETCH_HEAD)" != "$target_sha" ]]; then
  echo "imported deployment target mismatch" >&2
  exit 2
fi
