"""Run the adaptation worker behind a kernel-enforced Docker boundary.

Reads the worker request from stdin and writes its JSON response to stdout. No
host path is mounted; the root filesystem is read-only; networking and Linux
capabilities are absent; cgroup, pids, nofile and wall-time limits are applied.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

IMAGE = os.environ.get(
    "TOOROW_ADAPTATION_SANDBOX_IMAGE", "toorow-adaptation-sandbox:latest"
)
WALL_SECONDS = max(
    1, min(60, int(os.environ.get("TOOROW_ADAPTATION_WALL_SECONDS", "10")))
)


def _error(code: str, message: str) -> int:
    sys.stdout.write(json.dumps({"error": {"code": code, "message": message}}))
    return 1


def main() -> int:
    request = sys.stdin.read()
    command = [
        "docker",
        "run",
        "--rm",
        "--interactive",
        "--network",
        "none",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,nodev,size=16m",
        "--memory",
        "512m",
        "--memory-swap",
        "512m",
        "--cpus",
        "1.0",
        "--pids-limit",
        "32",
        "--ulimit",
        "nofile=32:32",
        "--ulimit",
        "fsize=0:0",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--user",
        "65532:65532",
        IMAGE,
    ]
    try:
        completed = subprocess.run(
            command,
            input=request,
            capture_output=True,
            text=True,
            timeout=WALL_SECONDS + 1,
            check=False,
            env={"PATH": os.environ.get("PATH", "")},
        )
    except subprocess.TimeoutExpired:
        return _error("wall_time_exceeded", "the hard sandbox exceeded its wall-time limit")
    except OSError:
        return _error("hard_sandbox_unavailable", "the container runtime is unavailable")

    if completed.returncode != 0 and not completed.stdout.strip():
        return _error("hard_sandbox_failed", "the isolated container failed")

    try:
        result = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError:
        return _error("bad_worker_output", "the isolated worker returned invalid JSON")
    if not isinstance(result, dict):
        return _error("bad_worker_output", "the isolated worker output is not an object")
    if "error" not in result:
        usage = result.setdefault("resource_usage", {})
        usage["isolation"] = {
            "network_none": True,
            "host_filesystem_none": True,
            "rootfs_read_only": True,
            "memory_limit": True,
            "cpu_limit": True,
            "wall_time_limit": True,
            "process_limit": True,
            "seccomp": True,
            "non_root": True,
        }
    sys.stdout.write(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
