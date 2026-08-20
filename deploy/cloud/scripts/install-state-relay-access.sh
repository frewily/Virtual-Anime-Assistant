#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C

if ! (( EUID == 0 )); then
  echo "run with sudo: $0 PUBLIC_KEY_FILE DEVICE_ID TOKEN_FILE" >&2
  exit 1
fi

if ! [[ $# -eq 3 ]]; then
  echo "usage: $0 PUBLIC_KEY_FILE DEVICE_ID TOKEN_FILE" >&2
  exit 2
fi

readonly public_key_file=$1
readonly device_id=$2
readonly token_file=$3
readonly relay_user=vaa-state-relay
readonly relay_group=vaa-state-relay
readonly relay_home=/var/lib/vaa-state-relay
readonly ssh_dir="$relay_home/.ssh"
readonly authorized_keys_path="$ssh_dir/authorized_keys"
readonly config_dir=/etc/virtual-anime-assistant
readonly installed_token_path="$config_dir/state-relay-token"
readonly wrapper_path=/usr/local/libexec/vaa-state-relay
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
readonly script_dir
readonly wrapper_source="$script_dir/vaa-state-relay.py"
readonly device_id_pattern='^[a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?$'
readonly token_pattern='^[!-~]{32,256}$'
readonly managed_key_pattern='^command="/usr/local/libexec/vaa-state-relay ([a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)",restrict ssh-ed25519 ([A-Za-z0-9+/]+={0,2}) vaa-state-relay:([a-z0-9]([a-z0-9-]{0,62}[a-z0-9])?)$'
readonly authorized_key_options="command=\"/usr/local/libexec/vaa-state-relay $device_id\",restrict"

if ! [[ -f $public_key_file && -r $public_key_file && ! -L $public_key_file ]]; then
  echo "reviewed public key must be a readable regular file" >&2
  exit 2
fi
if ! [[ $device_id =~ $device_id_pattern ]]; then
  echo "device id is invalid" >&2
  exit 2
fi
if ! [[ -f $token_file && -r $token_file && ! -L $token_file ]]; then
  echo "relay token must be a readable regular file" >&2
  exit 2
fi
if ! [[ -f $wrapper_source && -r $wrapper_source && ! -L $wrapper_source ]]; then
  echo "relay wrapper source is unavailable" >&2
  exit 2
fi

public_key_lines=()
mapfile -t public_key_lines < "$public_key_file"
if ! [[ ${#public_key_lines[@]} -eq 1 ]]; then
  echo "reviewed public key must contain exactly one line" >&2
  exit 2
fi
readonly public_key=${public_key_lines[0]}
if ! [[ $public_key =~ ^ssh-ed25519\ [A-Za-z0-9+/]+={0,2}(\ [^[:cntrl:]]+)?$ ]]; then
  echo "reviewed public key must be one bare ssh-ed25519 key" >&2
  exit 2
fi
key_type=
key_data=
key_comment=
read -r key_type key_data key_comment <<<"$public_key"
readonly key_type key_data key_comment

token_lines=()
mapfile -t token_lines < "$token_file"
if ! [[ ${#token_lines[@]} -eq 1 ]]; then
  echo "relay token must contain exactly one line" >&2
  exit 2
fi
readonly relay_token=${token_lines[0]}
if ! [[ $relay_token =~ $token_pattern ]]; then
  echo "relay token is invalid" >&2
  exit 2
fi

command -v ssh-keygen >/dev/null 2>&1
validation_file=$(mktemp)
authorized_keys_source=$(mktemp)
token_source=$(mktemp)
cleanup() {
  rm -f -- "$validation_file" "$authorized_keys_source" "$token_source"
}
trap cleanup EXIT

printf '%s\n' "$public_key" >"$validation_file"
chmod 0600 "$validation_file"
if ! ssh-keygen -l -f "$validation_file" >/dev/null; then
  echo "reviewed public key is not valid ssh-ed25519 material" >&2
  exit 2
fi

if ! getent group "$relay_group" >/dev/null; then
  groupadd --system "$relay_group"
fi
if ! id "$relay_user" >/dev/null 2>&1; then
  useradd --system --gid "$relay_group" --home-dir "$relay_home" \
    --create-home --shell /bin/sh "$relay_user"
fi
if [[ $(id -gn "$relay_user") != "$relay_group" ]]; then
  echo "existing relay account has an unexpected primary group" >&2
  exit 2
fi
relay_passwd=$(getent passwd "$relay_user")
IFS=: read -r _ _ _ _ _ existing_home existing_shell <<<"$relay_passwd"
if [[ $existing_home != "$relay_home" || $existing_shell != /bin/sh ]]; then
  echo "existing relay account has unexpected properties" >&2
  exit 2
fi

install -d -o "$relay_user" -g "$relay_group" -m 0750 "$relay_home"
install -d -o "$relay_user" -g "$relay_group" -m 0700 "$ssh_dir"
install -d -o root -g root -m 0755 /usr/local/libexec
install -o root -g root -m 0755 "$wrapper_source" "$wrapper_path"
install -d -o root -g "$relay_group" -m 0750 "$config_dir"
printf '%s\n' "$relay_token" >"$token_source"
install -o root -g "$relay_group" -m 0640 \
  "$token_source" "$installed_token_path"

if [[ -e $authorized_keys_path ]]; then
  if ! [[ -f $authorized_keys_path && ! -L $authorized_keys_path ]]; then
    echo "existing authorized_keys is not a regular managed file" >&2
    exit 2
  fi
  existing_lines=()
  mapfile -t existing_lines < "$authorized_keys_path"
  for line in "${existing_lines[@]}"; do
    if ! [[ $line =~ $managed_key_pattern ]]; then
      echo "existing authorized_keys contains an unmanaged entry" >&2
      exit 2
    fi
    if [[ ${BASH_REMATCH[1]} != "${BASH_REMATCH[4]}" ]]; then
      echo "existing authorized_keys contains a mismatched device marker" >&2
      exit 2
    fi
    if [[ ${BASH_REMATCH[1]} != "$device_id" ]]; then
      printf '%s\n' "$line" >>"$authorized_keys_source"
    fi
  done
fi

printf '%s %s %s vaa-state-relay:%s\n' \
  "$authorized_key_options" "$key_type" "$key_data" "$device_id" \
  >>"$authorized_keys_source"
install -o "$relay_user" -g "$relay_group" -m 0600 \
  "$authorized_keys_source" "$authorized_keys_path"

echo "state relay SSH access installed for $relay_user and device $device_id"
