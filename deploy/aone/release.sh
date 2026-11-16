#!/usr/bin/env bash
# Deploy one git ref of the console to this host: stop, update code, install
# dependencies, update the runtime image tag, start, verify health, roll back on
# failure. This is the script the Aone release pipeline calls on the ECS host.
#
# Usage (root):
#   sudo /opt/harness-bench/deploy/aone/release.sh <git-ref> [runtime-image]
#   sudo deploy/aone/release.sh v1.2.3 registry.example.com/harness/harness-runtime:abc123
#
# Environment:
#   HB_CODE_DIR         checkout to update (default /opt/harness-bench)
#   HB_CONSOLE_PORT     health-check port (default 8090)
#   HB_SERVICE_USER     owner of the checkout / service user (default hbench)
#   HB_GIT_REMOTE       remote to fetch from (default origin)
#   HB_FORCE=1          restart even when a run is active or queued
#   HB_SKIP_PREFLIGHT=1 skip preflight.sh
#   HB_HEALTH_TIMEOUT   seconds to wait for /api/health (default 60)
set -euo pipefail

ref="${1:-}"
image="${2:-}"
code_dir="${HB_CODE_DIR:-/opt/harness-bench}"
port="${HB_CONSOLE_PORT:-8090}"
service_user="${HB_SERVICE_USER:-hbench}"
service_name="${HB_SERVICE_NAME:-hbench-console}"
remote="${HB_GIT_REMOTE:-origin}"
conf_dir="/etc/harness-bench"
image_env="$conf_dir/runtime-image.env"
python="$code_dir/.venv/bin/python"
health_url="http://127.0.0.1:$port/api/health"
timeout="${HB_HEALTH_TIMEOUT:-60}"

say() { printf '[release] %s\n' "$*"; }
die() { printf '[release] %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root"
[ -n "$ref" ] || die "usage: release.sh <git-ref> [runtime-image]"
[ -d "$code_dir/.git" ] || die "no git checkout at $code_dir"
if [ -n "$image" ] && [[ ! "$image" =~ ^[A-Za-z0-9._/:@-]+$ ]]; then
  die "invalid image reference: $image"
fi

as_service() {
  if [ "$(id -un)" = "$service_user" ]; then "$@"; else runuser -u "$service_user" -- "$@"; fi
}

health() { curl -fsS --max-time 3 "$health_url" 2>/dev/null || true; }

wait_health() {
  local deadline=$((SECONDS + timeout))
  while [ "$SECONDS" -lt "$deadline" ]; do
    if health | grep -Eq '"status": *"ok"'; then return 0; fi
    sleep 2
  done
  return 1
}

write_image() {
  local value="$1"
  if [ -n "$value" ]; then
    printf 'HB_RUNTIME_IMAGE=%s\n' "$value" > "$image_env"
  else
    : > "$image_env"
  fi
  chown root:"$service_user" "$image_env"
  chmod 0640 "$image_env"
}

# ---------------------------------------------------------------- preconditions
[ "${HB_SKIP_PREFLIGHT:-0}" = "1" ] || "$code_dir/deploy/aone/preflight.sh"

previous=$(as_service git -C "$code_dir" rev-parse HEAD)
previous_image=$(sed -n 's/^HB_RUNTIME_IMAGE=//p' "$image_env" 2>/dev/null || true)
changed_image=0

current=$(health)
if [ -n "$current" ]; then
  busy=$(printf '%s' "$current" | "$python" -c \
    'import json,sys; r=(json.load(sys.stdin).get("runner") or {}); print(1 if r.get("active") or r.get("queued") else 0)' \
    2>/dev/null || echo 0)
  if [ "$busy" = "1" ] && [ "${HB_FORCE:-0}" != "1" ]; then
    die "a run is active or queued; wait for it or rerun with HB_FORCE=1 (the run will be marked interrupted)"
  fi
fi

# --------------------------------------------------------------------- rollback
rollback() {
  trap - ERR
  say "rolling back to ${previous:0:12}"
  as_service git -C "$code_dir" checkout --detach "$previous" || true
  [ "$changed_image" = "0" ] || write_image "$previous_image"
  as_service "$python" -m pip install -q -r "$code_dir/requirements-evals.txt" || true
  systemctl start "$service_name" || true
  wait_health || say "health check still failing after rollback; inspect journalctl -u $service_name"
}
trap 'code=$?; say "deploy failed (exit $code)"; rollback; exit $code' ERR

# ----------------------------------------------------------------------- update
say "fetching $remote"
as_service git -C "$code_dir" fetch "$remote" --tags --prune
target=$(as_service git -C "$code_dir" rev-parse --verify "$ref^{commit}")
say "previous ${previous:0:12} -> target ${target:0:12}"

if systemctl is-active --quiet "$service_name"; then
  say "stopping $service_name"
  systemctl stop "$service_name"
fi

as_service git -C "$code_dir" checkout --detach "$target"
say "installing dependencies"
as_service "$python" -m pip install -q -r "$code_dir/requirements-evals.txt"

[ -z "$image" ] || say "runtime image -> $image"
if [ -n "$image" ]; then
  write_image "$image"
  changed_image=1
fi

say "starting $service_name"
systemctl start "$service_name"
if ! wait_health; then
  say "console did not become healthy within ${timeout}s"
  rollback
  exit 1
fi

trap - ERR
say "deployed ${target:0:12}"
health
say "logs: journalctl -u $service_name -n 100"
