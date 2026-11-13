# Operation guide

中文：[operation-guide_zh.md](operation-guide_zh.md) · Back to the [README](../README.md)

Goal: run the full matrix `configs/matrices/bailian-18.yaml` (3 datasets × 2 models × 3 harnesses = 18
combinations) on a dedicated ECS host. Part A uses single-host Docker; Part B adds the steps for ACK.
Steps marked **billable** call the Bailian models.

## A. Single-host Docker (ECS)

### A1. Prepare the host

- OS: CentOS Stream 9 x86_64 (CentOS 7 is not supported: no Python 3.12).
- Region: Hong Kong (direct Docker Hub / PyPI / npm / Maven / GitHub access); mainland regions need mirrors.
- Size: 4 vCPU / 8 GiB per concurrent trial. `HB_CONCURRENCY=4` → 16 vCPU / 64 GiB.
- Disk: about 1 TB data disk for SWE-bench / Terminal-Bench images (estimate).

```sh
sudo dnf install -y python3.12 python3.12-pip git rsync curl tar java-21-openjdk-devel skopeo

# Maven 3.9 (the dnf maven package is too old and pulls in an older JDK)
curl -fsSL https://archive.apache.org/dist/maven/maven-3/3.9.9/binaries/apache-maven-3.9.9-bin.tar.gz \
  | sudo tar -xz -C /opt
cat >> ~/.bashrc <<'EOF'
export JAVA_HOME=/usr/lib/jvm/java-21-openjdk
export PATH=/opt/apache-maven-3.9.9/bin:$JAVA_HOME/bin:$PATH
EOF
. ~/.bashrc

# Docker (remove podman-docker first if it is installed)
curl -fsSL https://get.docker.com -o get-docker.sh && sudo sh get-docker.sh   # mainland: add --mirror Aliyun
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER" && newgrp docker
# Optional: put Docker data on the big disk
#   sudo mkdir -p /etc/docker && echo '{"data-root": "/data/docker"}' | sudo tee /etc/docker/daemon.json && sudo systemctl restart docker

docker info && docker compose version && java -version && mvn -v && python3.12 --version
docker login
```

### A2. Get the code

```sh
git clone git@gitlab.alibaba-inc.com:ken.lj/harness-bench.git ~/harness-bench
cd ~/harness-bench
git switch datasets/bundled-prepared      # optional: includes prepared datasets, so A3 can skip them
```

If the code is not pushed yet, rsync it from the repository root on your laptop:

```sh
rsync -az --exclude /.venv --exclude /artifacts --exclude target/ --exclude .env --exclude .DS_Store \
  ./ <user>@<ECS_IP>:~/harness-bench/
rsync -az artifacts/datasets <user>@<ECS_IP>:~/harness-bench/artifacts/   # optional: datasets too
```

### A3. Python environment and datasets

```sh
cd ~/harness-bench
python3.12 -m venv .venv && . .venv/bin/activate
python -m pip install -r requirements-evals.txt
# Skip the next two lines if artifacts/datasets already exists
python -m hbench datasets pull all
python -m hbench datasets prepare all
```

Check: `artifacts/datasets/` contains `terminal-bench` (66 tasks), `swe-bench-verified` (500) and `tau3-bench` (375).

### A4. Fill in `.env`

```sh
cp .env.example .env && chmod 600 .env
vi .env
```

| Variable | Value |
| --- | --- |
| `HB_BAILIAN_API_KEY` | Bailian API key |
| `HB_BAILIAN_BASE_URL` | Default `https://dashscope.aliyuncs.com/compatible-mode/v1` |
| `HB_MODEL_A_ID` / `HB_MODEL_B_ID` | Two different model IDs, both served on Bailian `/responses` (needed by codex) |
| `HB_TAU_API_KEY` / `HB_TAU_BASE_URL` | Default: reuse Bailian |
| `HB_TAU_USER_MODEL` / `HB_TAU_ASSERTIONS_MODEL` | `openai/<model-id>`; may be the same; keep fixed across comparisons |

tau3 tasks read `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `TAU2_USER_MODEL` and `TAU2_NL_ASSERTIONS_MODEL` from the
controller environment when building the compose environment and running the verifier. `hbench run` derives and
exports them from the `HB_TAU_*` values above, so do not set `OPENAI_*` separately.
| `HB_PIP_INDEX_URL` | Optional; mainland: `https://mirrors.aliyun.com/pypi/simple/` |
| `HB_RUNTIME_IMAGE` | `hbench/harness-runtime:0.1.0` |
| `HB_CLUSTER` | `docker` |
| `HB_CONCURRENCY` | Total concurrent trials |
| `HB_N_TASKS` | `1` for smoke; empty for the full run |
| `HB_DRY_RUN_ONLY` | `1` dry-run only; `0` real run |

Check the endpoint (no tokens used; 200 = OK, 401 = wrong key):

```sh
set -a; . ./.env; set +a
curl -sS -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $HB_BAILIAN_API_KEY" "$HB_BAILIAN_BASE_URL/models"
```

### A4a. OSS archive (optional)

Skip it and nothing is archived; evaluation is unaffected. Credentials come only from the ECS instance RAM role;
no AccessKey goes into `.env`.

**1. Bucket** (console, once): same region as the ECS host, ACL "Private". Recommended lifecycle rule: delete
parts (incomplete multipart uploads) after 7 days.

**2. RAM role** (once; if the role is already attached, only check its policy):

1. RAM console → Identities → Roles → Create Role; trusted entity "Alibaba Cloud Service → Elastic Compute Service".
2. Permissions → Policies → Create Policy → JSON, paste the policy below (replace `<bucket>`; also replace
   `harness-bench` if you change `HB_OSS_PREFIX`), then grant it to the role:

   ```json
   {
     "Version": "1",
     "Statement": [
       {
         "Effect": "Allow",
         "Action": "oss:ListObjects",
         "Resource": "acs:oss:*:*:<bucket>",
         "Condition": {"StringLike": {"oss:Prefix": "harness-bench/*"}}
       },
       {
         "Effect": "Allow",
         "Action": ["oss:PutObject", "oss:GetObject", "oss:ListParts", "oss:AbortMultipartUpload"],
         "Resource": "acs:oss:*:*:<bucket>/harness-bench/*"
       }
     ]
   }
   ```

3. ECS console → Instances → More (or "All Actions") → Instance Settings → Attach/Detach RAM Role; pick the role.

Confirm on the host (prints only the role name, never credentials):

```sh
t=$(curl -s -X PUT -H 'X-aliyun-ecs-metadata-token-ttl-seconds: 60' http://100.100.100.200/latest/api/token)
curl -s -H "X-aliyun-ecs-metadata-token: $t" http://100.100.100.200/latest/meta-data/ram/security-credentials/
```

**3. Add to `.env`**:

| Variable | Value |
| --- | --- |
| `HB_OSS_BUCKET` | Bucket name; empty = no archive |
| `HB_OSS_REGION` | Bucket region, e.g. `cn-hangzhou` (same as the ECS host) |
| `HB_OSS_ENDPOINT` | Empty = internal endpoint of the region; cross-region: `https://oss-<region>.aliyuncs.com` (Internet) |
| `HB_OSS_PREFIX` | Default `harness-bench`; must match the prefix in the policy |
| `HB_OSS_ROLE_NAME` | Empty = the role attached to the instance |
| `HB_OSS_SYNC_INTERVAL` | Seconds between background uploads; default `300` |

**4. Block containers from the metadata service** so task code cannot read the role (once per host):

```sh
sudo mkdir -p /etc/systemd/system/docker.service.d
sudo tee /etc/systemd/system/docker.service.d/block-metadata.conf <<'EOT'
[Service]
ExecStartPost=-/bin/sh -c 'iptables -C DOCKER-USER -d 100.100.100.200/32 -j DROP 2>/dev/null || iptables -I DOCKER-USER -d 100.100.100.200/32 -j DROP'
EOT
sudo systemctl daemon-reload && sudo systemctl restart docker
docker run --rm busybox nc -w 3 100.100.100.200 80 </dev/null && echo "NOT blocked" || echo blocked
```

`run-matrix.sh` checks the rule with `sudo -n iptables -C ...`; without passwordless sudo it prints an
`[oss] WARNING`, which is safe to ignore once the command above prints `blocked`.

**5. Check**:

```sh
set -a; . ./.env; set +a
python -m hbench archive --check   # [oss] check OK: oss://<bucket>/harness-bench/ ...
```

| Output | Fix |
| --- | --- |
| `alibabacloud-oss-v2 is not installed` | `python -m pip install -r requirements-evals.txt` |
| `HB_OSS_REGION is required` | Set `HB_OSS_REGION` in `.env` |
| `No RAM role is attached to this ECS instance` | Do step 2.3 (attach the role) |
| `ECS metadata returned no credentials for role ...` | `HB_OSS_ROLE_NAME` differs from the attached role; leave it empty |
| `status=403 code=AccessDenied` | Bucket name or prefix in the policy, or the policy is not granted to the role |
| `status=404 code=NoSuchBucket` | Wrong bucket name or `HB_OSS_REGION` |
| Connection timeout / refused | The internal endpoint works only in the same region: check `HB_OSS_REGION` or set `HB_OSS_ENDPOINT` |

**At run time**: `run-matrix.sh` runs `--check` first; on failure it prints `continuing WITHOUT OSS archive` and
evaluates anyway. It then uploads finished trials in the background every `HB_OSS_SYNC_INTERVAL` seconds, and
everything plus the report after the summary (at most 30 minutes; anything left stays local). OSS errors are
printed as `[oss] ...` and never stop trials or change the exit code; local files are kept. `job.log` (Harbor
debug log, not secret-scrubbed) is never uploaded. Objects: `<prefix>/jobs/<name>/...` and `<prefix>/reports/<name>/...`.

Re-upload by hand (only new or changed files are sent); add `--dry-run` to list the objects without uploading:

```sh
python -m hbench archive artifacts/jobs/<name> --report artifacts/reports/<name>
```

### A5. Build images

```sh
python -m hbench images build --runtime-image hbench/harness-runtime:0.1.0 \
  --tau3-dataset artifacts/datasets/tau3-bench --execute
docker image ls | grep -E 'hbench/(harness-runtime|tau3-main|tau3-runtime)'
```

Check: `hbench/harness-runtime:0.1.0`, `hbench/tau3-main:0.1.0` and `hbench/tau3-runtime:0.1.0` exist.
Repeat this step after changing `adapters/agentscope-java` or `environments/hbench/`.

### A6. Pre-pull task images (optional)

```sh
for d in terminal-bench swe-bench-verified; do
  python -m hbench images list "artifacts/datasets/$d" |
    python -c 'import json,sys; print("\n".join(i for i in json.load(sys.stdin) if not i.startswith("hbench/")))'
done | sed -E 's/:[^/@]+@/@/' | sort -u > artifacts/task-images.txt
xargs -n1 docker pull < artifacts/task-images.txt
docker system df
```

### A7. Smoke run (18 trials)

Set `HB_N_TASKS=1` in `.env`:

```sh
HB_DRY_RUN_ONLY=1 bash deploy/run-matrix.sh   # dry-run only
bash deploy/run-matrix.sh                     # billable: real run + summary
```

Check: open the printed `artifacts/reports/<name>/report.html`; all 18 combinations have records, with no
missing images, 401/404 or `/responses` errors.

3 Terminal-Bench tasks need an H100 and are recorded as errors on CPU-only hosts; to exclude them, set
`selection.exclude_gpu: true` in the matrix file.

### A8. Full run (5,646 trials)

Clear `HB_N_TASKS` in `.env`:

```sh
nohup bash deploy/run-matrix.sh > artifacts/run-matrix.log 2>&1 &   # billable
tail -f artifacts/run-matrix.log
docker ps
```

Interim report:

```sh
python -m hbench summary artifacts/jobs/<name> --output artifacts/reports/<name>-interim
```

Resume after the controller dies (not tested with hbench; otherwise re-run `bash deploy/run-matrix.sh`):

```sh
set -a; . ./.env; set +a
PYTHONPATH="$PWD" .venv/bin/harbor jobs resume -p artifacts/jobs/<name>
```

### A9. Collect results

On your laptop:

```sh
rsync -az <user>@<ECS_IP>:~/harness-bench/artifacts/reports/ ./artifacts/reports/
rsync -az <user>@<ECS_IP>:~/harness-bench/artifacts/jobs/<name> ./artifacts/jobs/   # optional: per-trial evidence behind report links
```

Or from OSS: download `<prefix>/reports/<name>/` and `<prefix>/jobs/<name>/` side by side (console or `ossutil cp -r`).

Report: `artifacts/reports/<name>/summary.json` and `report.html`: one row per harness × model × dataset, plus a
Trials table with one row per trial, linked (relative path) to its `result.json`; `agent/` and `verifier/` next to it
hold the trajectory and test output.

## B. ACK (later)

Prerequisites: an ACR namespace (e.g. `registry.cn-hongkong.aliyuncs.com/<ns>`), an ACK cluster in the same
region, its kubeconfig on the ECS host, and Pod access to Bailian. Run on the ECS host.

### B1. Build and push images

```sh
export REG=registry.cn-hongkong.aliyuncs.com/<ns>
python -m hbench images build --runtime-image "$REG/harness-runtime:0.1.0" \
  --tau3-dataset artifacts/datasets/tau3-bench --tau3-image-prefix "$REG/tau3" --execute
BUILD_PLAN_JSON=<plan_path printed above>
docker login "${REG%%/*}"
docker push "$REG/harness-runtime:0.1.0"
docker push "$REG/tau3-main:0.1.0"
docker push "$REG/tau3-runtime:0.1.0"
```

### B2. Mirror task images to ACR

```sh
skopeo login docker.io && skopeo login "${REG%%/*}"
for d in terminal-bench swe-bench-verified tau3-bench; do
  python -m hbench images mirror "artifacts/datasets/$d" --registry "$REG" \
    --built-image-map "$BUILD_PLAN_JSON" --output "artifacts/image-map-$d.json" --execute
done
python - <<'EOF'
import json
merged = {}
for d in ("terminal-bench", "swe-bench-verified", "tau3-bench"):
    merged.update(json.load(open(f"artifacts/image-map-{d}.json"))["image_map"])
with open("artifacts/image-map.json", "x") as f:
    json.dump({"image_map": merged}, f, indent=2)
print(len(merged), "mappings")
EOF
```

To review first, run the mirror loop without `--execute` and with a different `--output` file name.

### B3. ACK matrix file

```sh
cp configs/matrices/bailian-18.yaml configs/matrices/bailian-18-ack.yaml
# edit: cluster: ack    image_map: artifacts/image-map.json
```

### B4. Bootstrap the namespace (once)

```sh
export KUBECONFIG=<path> CTX=<context>
kubectl --context "$CTX" auth whoami
kubectl --context "$CTX" apply -f deploy/k8s/namespace.yaml -f deploy/k8s/rbac.yaml
kubectl --context "$CTX" -n harness-bench create rolebinding harness-bench-runner \
  --role=harness-bench-runner --user=<user from whoami>
kubectl --context "$CTX" -n harness-bench auth can-i create pods/exec
read -rs ACR_PASSWORD
kubectl --context "$CTX" -n harness-bench create secret docker-registry hb-acr \
  --docker-server="${REG%%/*}" --docker-username=<acr-user> --docker-password="$ACR_PASSWORD"
unset ACR_PASSWORD
```

### B5. Update `.env` and run

```sh
HB_MATRIX=configs/matrices/bailian-18-ack.yaml
HB_CLUSTER=ack
HB_REGISTRY=registry.cn-hongkong.aliyuncs.com/<ns>
HB_RUNTIME_IMAGE=registry.cn-hongkong.aliyuncs.com/<ns>/harness-runtime:0.1.0
HB_NAMESPACE=harness-bench
HB_KUBECONFIG=<path>
HB_KUBE_CONTEXT=<context>
HB_IMAGE_PULL_SECRET=hb-acr
```

Then run A7 and A8.

Note: ACK defaults to `network_enforcement: none`, so restricted-network tasks are rejected; GPU tasks need H100 nodes.

## C. Common commands

```sh
python -m hbench list                                                     # dataset/harness/model/cluster/matrix profiles
python -m hbench matrix --config configs/matrices/bailian-18.yaml \
  --output artifacts/plans/<name>.yaml [--cluster ack] [--concurrency 8] [--n-tasks 1]
python -m hbench run --config artifacts/plans/<name>.yaml --dry-run      # no model calls
python -m hbench run --config artifacts/plans/<name>.yaml                # billable
python -m hbench summary artifacts/jobs/<name> --output artifacts/reports/<name>
python -m hbench images list artifacts/datasets/<dataset>
python -m unittest discover -s tests/hbench -p 'test_*.py'               # offline unit tests
```

- Each plan runs once; render a new one to rerun (`run-matrix.sh` adds a timestamp).
- `concurrency` is the total across all combinations.
- Do not commit `.env` or `artifacts/`.
