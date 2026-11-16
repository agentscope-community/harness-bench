#!/usr/bin/env bash
# CI step: build the harness runtime image for one commit and push it to the
# registry. Run on a Docker-capable runner; it is not part of the host release.
#
# Usage:
#   deploy/aone/build-runtime.sh <registry>/<namespace>/harness-runtime:<tag>
#
# Environment:
#   PYTHON            python with harbor installed (default .venv/bin/python)
#   HB_PUSH=0         build only, do not push
#   HB_TAU3_DATASET / HB_TAU3_IMAGE_PREFIX
#                     also stage and build the tau3 task images
set -euo pipefail

image="${1:-}"
repo_dir="$(cd "$(dirname "$0")/../.." && pwd)"
python="${PYTHON:-$repo_dir/.venv/bin/python}"

die() { echo "[build] $*" >&2; exit 1; }
say() { echo "[build] $*"; }

[ -n "$image" ] || die "usage: build-runtime.sh <registry>/<namespace>/harness-runtime:<tag>"
[[ "$image" =~ ^[A-Za-z0-9._/:@-]+$ ]] || die "invalid image reference: $image"
cd "$repo_dir"

docker info >/dev/null 2>&1 || die "docker daemon unreachable"
command -v mvn >/dev/null || die "maven not installed (needed for the Java adapter)"
[ -x "$python" ] || die "python not found: $python"

args=(images build --runtime-image "$image" --execute)
[ -z "${HB_TAU3_DATASET:-}" ] || args+=(--tau3-dataset "$HB_TAU3_DATASET")
[ -z "${HB_TAU3_IMAGE_PREFIX:-}" ] || args+=(--tau3-image-prefix "$HB_TAU3_IMAGE_PREFIX")

say "building $image"
"$python" -m hbench "${args[@]}"
docker image inspect "$image" --format '{{.Id}}' >/dev/null \
  || die "image was not produced: $image"

if [ "${HB_PUSH:-1}" != "0" ]; then
  say "pushing $image"
  docker push "$image"
  say "digest: $(docker image inspect "$image" --format '{{index .RepoDigests 0}}' 2>/dev/null || echo unknown)"
else
  say "HB_PUSH=0: built only, not pushed"
fi
