#!/usr/bin/env bash
# One-time host setup for the evaluation console on a dedicated ECS host.
# Creates the service user, binds the data disk under the checkout, installs the
# systemd unit and the environment template. It does not start the service.
#
# Usage (root):
#   sudo HB_CODE_DIR=/opt/harness-bench HB_DATA_DIR=/data/harness-bench \
#        bash deploy/aone/install.sh
set -euo pipefail

code_dir="${HB_CODE_DIR:-/opt/harness-bench}"
data_dir="${HB_DATA_DIR:-/data/harness-bench}"
service_user="${HB_SERVICE_USER:-hbench}"
service_name="hbench-console"
conf_dir="/etc/harness-bench"
repo_dir="$(cd "$(dirname "$0")/../.." && pwd)"
unit_src="$repo_dir/deploy/systemd/hbench-console.service.in"
unit_dst="/etc/systemd/system/$service_name.service"

fail() { echo "[fail] $*" >&2; exit 1; }
ok() { echo "[ok] $*"; }
note() { echo "[note] $*"; }

[ "$(id -u)" -eq 0 ] || fail "run as root"
[ -f "$unit_src" ] || fail "unit template missing: $unit_src"
[ -f "$repo_dir/deploy/aone/console.env.example" ] || fail "console.env.example missing"
[ -d "$code_dir/.git" ] || fail "no git checkout at $code_dir; deploy the code there first"
case "$code_dir" in
  /root | /root/*) fail "use a path readable by $service_user, e.g. /opt/harness-bench" ;;
esac

# ---------------------------------------------------------------- service user
if ! getent group docker >/dev/null; then
  fail "docker group not found; install Docker first (docs/operation-guide_zh.md A1)"
fi
if ! id "$service_user" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "/var/lib/$service_user" \
    --shell /sbin/nologin "$service_user"
  ok "created user $service_user"
fi
usermod -aG docker "$service_user"
ok "$service_user is in group docker"

# ------------------------------------------------------------------ data disk
install -d -m 0750 "$data_dir" "$data_dir/artifacts"
if [ -n "$(ls -A "$code_dir/artifacts" 2>/dev/null || true)" ] && \
   ! mountpoint -q "$code_dir/artifacts"; then
  note "$code_dir/artifacts is not empty and will be hidden by the bind mount:"
  note "  move its contents to $data_dir/artifacts before starting the service"
fi
install -d "$code_dir/artifacts"
fstab_line="$data_dir/artifacts $code_dir/artifacts none bind 0 0"
grep -qF "$fstab_line" /etc/fstab || echo "$fstab_line" >> /etc/fstab
mountpoint -q "$code_dir/artifacts" || mount --bind "$data_dir/artifacts" "$code_dir/artifacts"
ok "bind mount: $code_dir/artifacts -> $data_dir/artifacts"

chown -R "$service_user:$service_user" "$code_dir" "$data_dir"
ok "ownership: $service_user"

# ------------------------------------------------------------------- env files
install -d -m 0750 -o root -g "$service_user" "$conf_dir"
if [ ! -f "$conf_dir/console.env" ]; then
  # HB_RUNTIME_IMAGE lives in runtime-image.env only; release.sh is its sole writer.
  {
    cat "$repo_dir/deploy/aone/console.env.example"
    echo
    echo "# ---- hbench runtime settings (from .env.example) ----"
    grep -v '^#' "$repo_dir/.env.example" | grep -v '^HB_RUNTIME_IMAGE=' | sed '/^$/d'
  } > "$conf_dir/console.env"
  ok "wrote $conf_dir/console.env"
else
  note "$conf_dir/console.env already exists; left untouched"
fi
if [ ! -s "$conf_dir/runtime-image.env" ]; then
  seed=$(sed -n 's/^HB_RUNTIME_IMAGE=\([^ ]*\).*/\1/p' "$repo_dir/.env.example" | head -1)
  [ -z "$seed" ] || printf 'HB_RUNTIME_IMAGE=%s\n' "$seed" > "$conf_dir/runtime-image.env"
fi
chown root:"$service_user" "$conf_dir/console.env" "$conf_dir/runtime-image.env"
chmod 0640 "$conf_dir/console.env" "$conf_dir/runtime-image.env"
ok "permissions: 0640 root:$service_user"

# ----------------------------------------------------------------- systemd unit
sed -e "s|@CODE_DIR@|$code_dir|g" -e "s|@USER@|$service_user|g" "$unit_src" > "$unit_dst"
systemctl daemon-reload
systemctl enable "$service_name" >/dev/null
ok "installed $unit_dst"

cat <<EOF

Next steps:
  1. Fill in credentials and model ids:
       sudo vi $conf_dir/console.env
       # keep CONSOLE_ALLOW_REAL_RUNS=0 until the paid-run switch is authorized
  2. Run the host checks:
       sudo $code_dir/deploy/aone/preflight.sh
  3. Start and verify:
       sudo systemctl start $service_name
       curl -fsS http://127.0.0.1:\${CONSOLE_PORT:-8090}/api/health
EOF
