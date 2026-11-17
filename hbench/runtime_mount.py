"""Inject the frozen runtime without replacing a task image's ENTRYPOINT.

The runtime image uses environments/hbench/Dockerfile.runtime's layout:
/opt/harness, /opt/java/openjdk, /usr/local/bin/{node,uv,uvx},
/usr/local/lib/node_modules/{npm,@openai/codex,@earendil-works/pi-coding-agent}
and /opt/pi-mcp-adapter. Task images must be Linux/glibc-compatible and have
/bin/sh, bash, tar and the shared libraries required by Node/Java. The runtime
is not a replacement root filesystem and does not install OS dependencies.
"""
from __future__ import annotations

import math
import shlex
from typing import Any

from hbench.compose import merge_pod_overrides

RUNTIME_ROOT = "/opt/hbench-runtime"
SETUP_READY = "/tmp/.hbench-setup-ready"


def runtime_overrides(image: str | None, *, main_image: str, startup_env: dict[str, str],
                      log_subpaths: list[str] | None = None, main_keepalive: bool = True) -> dict[str, Any]:
    """Return a Pod fragment; copy binaries once with a non-privileged init.

    Idle images keep sleep as CMD without replacing ENTRYPOINT. Explicit image
    workloads opt out; ENTRYPOINT compatibility still needs target verification.
    """
    subpaths = sorted(set(["agent", "verifier", "artifacts", "user-agent", *(log_subpaths or [])]))
    mkdir = "mkdir -p " + " ".join(shlex.quote("/hbench-logs/" + path) for path in subpaths)
    mkdir += " && chmod 777 /hbench-logs " + " ".join(shlex.quote("/hbench-logs/" + path) for path in subpaths)
    init = {"name": "hbench-logs-init", "image": image or main_image, "command": ["sh", "-ec", mkdir],
            "securityContext": {"privileged": False, "runAsUser": 0},
            "volumeMounts": [{"name": "hbench-logs", "mountPath": "/hbench-logs"}]}
    result = {"spec": {"volumes": [{"name": "hbench-logs", "emptyDir": {}}], "initContainers": [init],
                       "containers": [{"name": "main", "command": None,
                                       "args": ["sleep", "infinity"] if main_keepalive else None,
                                       "env": [{"name": key, "value": value} for key, value in startup_env.items()],
                                       "volumeMounts": [{"name": "hbench-logs", "mountPath": "/logs"}]}]}}
    if not image:
        return result
    script = runtime_copy_script()
    env = runtime_env(startup_env)
    return merge_pod_overrides(result, {"spec": {
        "volumes": [{"name": "hbench-runtime", "emptyDir": {}}],
        "initContainers": [{"name": "hbench-runtime-init", "image": image, "command": ["sh", "-ec", script],
                            "securityContext": {"privileged": False, "runAsUser": 0},
                            "volumeMounts": [{"name": "hbench-runtime", "mountPath": "/payload"}]}],
        "containers": [{"name": "main", "env": [{"name": key, "value": value} for key, value in env.items()],
                        "volumeMounts": [{"name": "hbench-runtime", "mountPath": RUNTIME_ROOT, "readOnly": True},
                                         {"name": "hbench-runtime", "mountPath": "/opt/harness", "subPath": "harness", "readOnly": True},
                                         {"name": "hbench-runtime", "mountPath": "/opt/pi-mcp-adapter", "subPath": "pi-mcp-adapter", "readOnly": True},
                                         {"name": "hbench-runtime", "mountPath": "/tmp/harbor-pi-mcp-adapter", "subPath": "pi-mcp-adapter", "readOnly": True},
                                         {"name": "hbench-runtime", "mountPath": "/root/.local/bin/uv", "subPath": "bin/uv", "readOnly": True},
                                         {"name": "hbench-runtime", "mountPath": "/root/.local/bin/uvx", "subPath": "bin/uvx", "readOnly": True},
                                         {"name": "hbench-runtime", "mountPath": "/root/.nvm", "subPath": "nvm", "readOnly": True}]}]}})


def runtime_copy_script() -> str:
    """Copy the already-built frozen payload; no downloads or installation."""
    return """set -eu
mkdir -p /payload/bin /payload/nvm
cp -a /opt/harness /payload/harness
cp -a /opt/java/openjdk /payload/java
cp -a /usr/local/bin/node /payload/bin/node
cp -a /usr/local/bin/uv /usr/local/bin/uvx /payload/bin/
cp -a /usr/local/lib/node_modules /payload/node_modules
cp -a /opt/pi-mcp-adapter /payload/pi-mcp-adapter
if [ -d /opt/hbench-runtime/extensions ]; then
    cp -a /opt/hbench-runtime/extensions /payload/extensions
    ln -s /opt/hbench-runtime/extensions/bin/hermes /payload/bin/hermes
    ln -s /opt/hbench-runtime/node_modules/@anthropic-ai/claude-code/bin/claude.exe /payload/bin/claude
    ln -s /opt/hbench-runtime/node_modules/openclaw/openclaw.mjs /payload/bin/openclaw
fi
ln -s /opt/hbench-runtime/node_modules/npm/bin/npm-cli.js /payload/bin/npm
ln -s /opt/hbench-runtime/node_modules/npm/bin/npx-cli.js /payload/bin/npx
ln -s /opt/hbench-runtime/node_modules/@openai/codex/bin/codex.js /payload/bin/codex
ln -s /opt/hbench-runtime/node_modules/@earendil-works/pi-coding-agent/dist/cli.js /payload/bin/pi
if [ -f /root/.nvm/nvm.sh ]; then cp /root/.nvm/nvm.sh /payload/nvm/nvm.sh; else touch /payload/nvm/nvm.sh; fi
test -f /payload/node_modules/@openai/codex/bin/codex.js
test -f /payload/node_modules/@earendil-works/pi-coding-agent/dist/cli.js
test -f /payload/pi-mcp-adapter/node_modules/pi-mcp-adapter/index.ts
test -x /payload/java/bin/java
"""


def runtime_env(startup_env: dict[str, str]) -> dict[str, str]:
    """Only the frozen runtime's PATH/JAVA_HOME overlay, idempotently."""
    runtime_path = RUNTIME_ROOT + "/bin:" + RUNTIME_ROOT + "/java/bin:"
    original_path = startup_env.get("PATH", "/root/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    return {"JAVA_HOME": RUNTIME_ROOT + "/java",
            "PATH": original_path if original_path.startswith(runtime_path) else runtime_path + original_path}


def add_setup_gate(container: dict, timeout_sec: float, *, agent_user: str | int | None = None,
                   agent_home: str | None = None) -> dict:
    """Preserve ENTRYPOINT/CMD and healthcheck; gate readiness on postStart.

    Kubernetes runs postStart concurrently with the image entrypoint. Runtime
    binaries/env are already present at creation; task setup hooks may race the
    entrypoint but no agent is released until both setup and readiness succeed.
    The main container must be root, even when agent execs use a non-root user.
    """
    result = merge_pod_overrides({}, container)
    lifecycle = result.setdefault("lifecycle", {})
    previous = lifecycle.get("postStart")
    script = ""
    if previous:
        if set(previous) != {"exec"} or not previous["exec"].get("command"):
            raise ValueError("main postStart must be exec to compose the setup readiness gate")
        script = shlex.join(previous["exec"]["command"]) + "\n"
    user = "root" if agent_user in (None, "root", 0) else agent_user
    user_arg = (f"$(getent passwd {user} | cut -d: -f1)" if isinstance(user, int) else shlex.quote(user))
    home_arg = shlex.quote(agent_home) if agent_home is not None else (
        '${HOME:-/root}' if user == "root" else '$(su "$agent_user" -s /bin/sh -c \'printf "%s" "$HOME"\')')
    user_setup = """set -eu
if [ ! -d "$HOME/.nvm" ]; then mkdir -p "$HOME/.nvm"; fi
if [ ! -e "$HOME/.nvm/nvm.sh" ]; then touch "$HOME/.nvm/nvm.sh"; fi
test -r "$HOME/.nvm/nvm.sh"
test -w "$HOME"
for path in /logs/agent /logs/verifier /logs/artifacts /logs/user-agent; do test -w "$path"; done
"""
    if agent_home is not None:
        user_setup = "export HOME=" + shlex.quote(agent_home) + "\n" + user_setup
    elif user == "root":
        user_setup = "export HOME=${HOME:-/root}\n" + user_setup
    setup_command = ("sh -ec " if user == "root" else 'su "$agent_user" -s /bin/sh -c ') + shlex.quote(user_setup)
    script += f"""agent_user={user_arg}
test -n "$agent_user"
agent_home={home_arg}
case "$agent_home" in /*) ;; *) exit 1 ;; esac
mkdir -p "$agent_home"
chown "$agent_user" "$agent_home"
{setup_command}
touch {shlex.quote(SETUP_READY)}
"""
    lifecycle["postStart"] = {"exec": {"command": ["sh", "-ec", script]}}
    if "startupProbe" in result:
        # A separate user startup probe cannot safely be weakened or replaced.
        raise ValueError("main startupProbe is reserved for hbench setup gating; use readinessProbe")
    result["startupProbe"] = {"exec": {"command": ["sh", "-c", "test -f " + shlex.quote(SETUP_READY)]},
                              "periodSeconds": 1, "timeoutSeconds": 1,
                              "failureThreshold": max(1, math.ceil(timeout_sec))}
    return result
