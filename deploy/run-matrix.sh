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
if [ "${HB_DRY_RUN_ONLY:-0}" = 1 ]; then
  echo "HB_DRY_RUN_ONLY=1: stopping before the real run. Plan: $plan"
  exit 0
fi

echo "==> Real run (calls models; may incur cost)"
status=0
"$python" -m hbench run --config "$plan" || status=$?

echo "==> Summary"
"$python" -m hbench summary "artifacts/jobs/$name" --output "artifacts/reports/$name" || true
echo "Plan:   $plan"
echo "Job:    artifacts/jobs/$name"
echo "Report: artifacts/reports/$name/report.html"
exit "$status"
