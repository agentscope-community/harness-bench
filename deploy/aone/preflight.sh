#!/usr/bin/env bash
# Host checks for the evaluation console. Run before a release and after changing
# host configuration. Exits non-zero on problems that would break runs.
#
# Usage:
#   sudo /opt/harness-bench/deploy/aone/preflight.sh
set -uo pipefail

code_dir="${HB_CODE_DIR:-/opt/harness-bench}"
service_user="${HB_SERVICE_USER:-hbench}"
env_file="${HB_ENV_FILE:-/etc/harness-bench/console.env}"
port="${HB_CONSOLE_PORT:-8090}"
min_free_gb="${HB_MIN_FREE_GB:-50}"
python="$code_dir/.venv/bin/python"

failures=0
warns=0
ok() { printf '[ok]   %s\n' "$*"; }
bad() { printf '[fail] %s\n' "$*" >&2; failures=$((failures + 1)); }
warn() { printf '[warn] %s\n' "$*" >&2; warns=$((warns + 1)); }

env_get() {
  ( set +u; set -a; . "$env_file"; set +a; printf '%s' "${!1:-}" ) 2>/dev/null || true
}
run_with_env() {
  ( set +u; set -a; . "$env_file"; set +a; "$@" )
}

# ------------------------------------------------------------------- checkout
[ -d "$code_dir/.git" ] || bad "no git checkout at $code_dir"
[ -x "$python" ] || bad "missing venv interpreter: $python (python3.12 -m venv .venv)"
if [ -x "$python" ]; then
  if "$python" -c "import fastapi, uvicorn, yaml, harbor" 2>/dev/null; then
    ok "python imports (fastapi, uvicorn, yaml, harbor)"
  else
    bad "venv is missing dependencies; run $python -m pip install -r requirements-evals.txt"
  fi
fi

# ------------------------------------------------------------------ data disk
if mountpoint -q "$code_dir/artifacts"; then
  ok "artifacts is a mountpoint ($code_dir/artifacts)"
else
  bad "artifacts is not a bind mount; results would land on the root disk"
fi
if [ -d "$code_dir/artifacts" ]; then
  free_gb=$(df -Pk "$code_dir/artifacts" | awk 'NR==2 {printf "%d", $4/1048576}')
  [ "${free_gb:-0}" -ge "$min_free_gb" ] \
    && ok "free space ${free_gb}G" \
    || warn "free space only ${free_gb}G (threshold ${min_free_gb}G)"
fi
if [ -d "$code_dir/artifacts/datasets" ] && [ -n "$(ls -A "$code_dir/artifacts/datasets" 2>/dev/null)" ]; then
  ok "prepared datasets present"
else
  warn "artifacts/datasets is empty; prepare datasets before a real run"
fi
[ -w "$code_dir/artifacts" ] || bad "artifacts is not writable by the current user"

# --------------------------------------------------------------------- docker
if docker info >/dev/null 2>&1; then
  ok "docker daemon reachable"
else
  bad "docker daemon unreachable (or $service_user lacks the docker group)"
fi
docker compose version >/dev/null 2>&1 \
  && ok "docker compose plugin" \
  || bad "docker compose plugin missing"
if grep -qs "default-address-pools" /etc/docker/daemon.json; then
  ok "docker default-address-pools configured"
else
  warn "no default-address-pools in /etc/docker/daemon.json; concurrent trials will exhaust the address pool"
fi
if iptables -C DOCKER-USER -d 100.100.100.200/32 -j DROP 2>/dev/null \
   || sudo -n iptables -C DOCKER-USER -d 100.100.100.200/32 -j DROP 2>/dev/null; then
  ok "containers are blocked from the metadata service"
else
  warn "cannot confirm the 100.100.100.200 DROP rule; task code may read the instance role"
fi

# ------------------------------------------------------------------ env file
if [ -f "$env_file" ]; then
  mode=$(stat -c '%a' "$env_file")
  case "$mode" in
    *4 | *5 | *6 | *7) bad "$env_file is group/world readable (mode $mode)" ;;
    *) ok "$env_file mode $mode" ;;
  esac
  if [ "$(env_get CONSOLE_ALLOW_REAL_RUNS | tr '[:upper:]' '[:lower:]')" = "1" ]; then
    for name in HB_BAILIAN_API_KEY HB_MODEL_A_ID HB_MODEL_B_ID; do
      [ -n "$(env_get "$name")" ] || bad "real runs are enabled but $name is empty"
    done
    for name in HB_TAU_API_KEY HB_TAU_BASE_URL HB_TAU_USER_MODEL HB_TAU_ASSERTIONS_MODEL; do
      [ -n "$(env_get "$name")" ] || warn "$name is empty; tau3 trials will fail if the matrix includes tau3"
    done
    ok "paid-run settings present"
  else
    ok "real runs are disabled (dry-run only)"
  fi
  if [ -n "$(env_get HB_OSS_BUCKET)" ]; then
    if run_with_env "$python" -m hbench archive --check >/dev/null 2>&1; then
      ok "OSS archive check passed"
    else
      warn "OSS archive check failed; results stay local (archive is best effort)"
    fi
  fi
  runtime_image=$(sed -n 's/^HB_RUNTIME_IMAGE=//p' /etc/harness-bench/runtime-image.env 2>/dev/null | head -1)
  if [ -z "$runtime_image" ]; then
    bad "no runtime image tag; pass one to release.sh or set /etc/harness-bench/runtime-image.env"
  elif docker image inspect "$runtime_image" >/dev/null 2>&1; then
    ok "runtime image present locally: $runtime_image"
  else
    warn "runtime image not present locally ($runtime_image); hbench will pull it, ensure docker login"
  fi
else
  bad "missing $env_file; run deploy/aone/install.sh and fill it in"
fi

# --------------------------------------------------------------------- service
if systemctl list-unit-files hbench-console.service >/dev/null 2>&1; then
  state=$(systemctl is-active hbench-console 2>/dev/null || true)
  ok "hbench-console.service installed (state: ${state:-unknown})"
else
  warn "hbench-console.service not installed; run deploy/aone/install.sh"
fi
if curl -fsS --max-time 3 "http://127.0.0.1:$port/api/health" >/dev/null 2>&1; then
  ok "console answers on 127.0.0.1:$port"
else
  warn "console is not answering on 127.0.0.1:$port"
fi

echo
if [ "$failures" -gt 0 ]; then
  echo "preflight failed: $failures error(s), $warns warning(s)" >&2
  exit 1
fi
echo "preflight passed with $warns warning(s)"
