#!/usr/bin/env bash
# One-click matrix evaluation: load env file -> render plan -> dry-run -> real run -> summary.
# Usage: bash deploy/run-matrix.sh [env-file]        (default: .env in the repository root)
# Long runs: nohup bash deploy/run-matrix.sh > artifacts/run-matrix.log 2>&1 &
set -euo pipefail
cd "$(dirname "$0")/.."

env_file="${1:-.env}"
[ -f "$env_file" ] || { echo "Env file not found: $env_file (cp .env.example .env and fill it)" >&2; exit 2; }
set -a
# shellcheck disable=SC1090
. "$env_file"
set +a

python=.venv/bin/python
[ -x "$python" ] || python=python3

missing=()
required=(HB_BAILIAN_API_KEY HB_MODEL_A_ID HB_MODEL_B_ID HB_TAU_API_KEY HB_TAU_BASE_URL
          HB_TAU_USER_MODEL HB_TAU_ASSERTIONS_MODEL HB_RUNTIME_IMAGE)
cluster="${HB_CLUSTER:-docker}"
[ "$cluster" = docker ] || required+=(HB_REGISTRY)
for name in "${required[@]}"; do
  [ -n "${!name:-}" ] || missing+=("$name")
done
if [ ${#missing[@]} -gt 0 ]; then
  echo "Missing required settings in $env_file: ${missing[*]}" >&2
  exit 2
fi
if [ "$cluster" = docker ]; then
  docker info >/dev/null 2>&1 || { echo "Docker daemon is not reachable on this host" >&2; exit 2; }
fi

name="${HB_RUN_NAME:-bailian-18}-$(date -u +%Y%m%d-%H%M%S)"
plan="artifacts/plans/$name.yaml"
args=(matrix --config "${HB_MATRIX:-configs/matrices/bailian-18.yaml}" --cluster "$cluster"
      --name "$name" --output "$plan")
[ -z "${HB_CONCURRENCY:-}" ] || args+=(--concurrency "$HB_CONCURRENCY")
[ -z "${HB_N_TASKS:-}" ] || args+=(--n-tasks "$HB_N_TASKS")

echo "==> Render plan: $plan"
"$python" -m hbench "${args[@]}"
echo "==> Dry-run (no model calls)"
"$python" -m hbench run --config "$plan" --dry-run

# Optional OSS archive. It runs beside the evaluation: any OSS problem is only
# printed with an [oss] prefix and never stops trials or changes the exit code.
job="artifacts/jobs/$name"
report="artifacts/reports/$name"
oss=0
if [ -n "${HB_OSS_BUCKET:-}" ]; then
  echo "==> OSS check"
  if "$python" -m hbench archive --check; then
    oss=1
  else
    echo "[oss] check failed; continuing WITHOUT OSS archive (results stay local)" >&2
  fi
  if [ "$cluster" = docker ] && ! sudo -n iptables -C DOCKER-USER -d 100.100.100.200/32 -j DROP 2>/dev/null; then
    echo "[oss] WARNING: cannot confirm containers are blocked from the ECS metadata service" \
         "(100.100.100.200); task code could read the instance role. See docs/operation-guide.md." >&2
  fi
fi

if [ "${HB_DRY_RUN_ONLY:-0}" = 1 ]; then
  echo "HB_DRY_RUN_ONLY=1: stopping before the real run. Plan: $plan"
  exit 0
fi

oss_pid=
if [ "$oss" = 1 ]; then
  nice -n 10 "$python" -m hbench archive "$job" --report "$report" --follow --parent $$ \
    --interval "${HB_OSS_SYNC_INTERVAL:-300}" &
  oss_pid=$!
fi

echo "==> Real run (calls models; may incur cost)"
status=0
"$python" -m hbench run --config "$plan" || status=$?

echo "==> Summary"
"$python" -m hbench summary "$job" --output "$report" || true
if [ -n "$oss_pid" ]; then
  echo "==> Final OSS upload (evaluation already finished)"
  # SIGUSR1 = run and summary are done, so a full final pass is safe (TERM would upload settled trials only).
  kill -USR1 "$oss_pid" 2>/dev/null || true
  wait "$oss_pid" || echo "[oss] some uploads failed (see [oss] lines above); local results are intact." \
    "Retry: $python -m hbench archive $job --report $report" >&2
fi
echo "Plan:   $plan"
echo "Job:    $job"
echo "Report: $report/report.html"
[ "$oss" = 0 ] || echo "OSS:    oss://$HB_OSS_BUCKET/${HB_OSS_PREFIX:-harness-bench}/{jobs,reports}/$name/"
exit "$status"
