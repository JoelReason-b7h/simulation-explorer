"""Check the local stack is the one the harness needs, and that every service is up.

    python3 stack_check.py checkout   # exit 1 unless SIM_STACK_REPO is the right checkout
    python3 stack_check.py up         # wait for every service; exit 1 if one stays down

The stack must come from sim-main: the images are built there, only its docker files carry the
harness's local-auth settings (local_auth_patch.py), and an older checkout's LocalStack scripts
miss queues the images need. A launch from the harness's own worktree on 2026-09-27 left the
adapter exiting on a missing `core-dmi-adapter` queue.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import cycle

SERVICES = ("adapter", "clearing", "compliance", "compliance-api", "core", "core-ro",
            "hot-sauce-bank", "localstack", "ops-api", "postgres", "public-api", "redis",
            "simulator-api", "toxiproxy", "wiremock")
OPS_HEALTH = "http://localhost:5200/health"


def checkout_problems(repo=None):
    repo = Path(repo or cycle.REPO)
    problems = []
    if repo.name != "sim-main":
        problems.append("the stack checkout is {}, not sim-main".format(repo))
    for env in ("docker/ops.env", "docker/simulator-api.env", "docker/compliance-api.env"):
        path = repo / env
        if not path.exists() or "AWS_ENDPOINT_URL_COGNITO_IDENTITY_PROVIDER" not in path.read_text():
            problems.append("{} lacks the local-auth endpoint (run local_auth_patch.py)".format(env))
    jwks = repo / "docker/wiremock/__files/cognito-jwks.json"
    if not jwks.exists() or "simulation-explorer-local" not in jwks.read_text():
        problems.append("the WireMock JWKS lacks the harness key (run local_auth_patch.py)")
    problems += mount_problems(repo)
    return problems


def mount_problems(repo):
    """A bind mount whose source is missing makes Docker create a root-owned folder in its place,
    and a missing permissions.sql then leaves Postgres with no service users."""
    compose = repo / "docker/docker-compose.yml"
    problems = []
    for source in re.findall(r"^\s*-\s*(\.{1,2}/[^:\s]+):", compose.read_text(), re.M):
        path = (compose.parent / source).resolve()
        if not path.exists():
            problems.append("{} mounts {}, which is missing".format(compose.name, path))
        elif path.suffix and path.is_dir():
            problems.append("{} is a folder, not a file (remove it and copy the file)".format(path))
    return problems


def states():
    done = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}\t{{.State}}"],
                          capture_output=True, text=True, timeout=30)
    found = {}
    for line in done.stdout.splitlines():
        name, _, state = line.partition("\t")
        if name.startswith("docker-") and name.endswith("-1"):
            found[name[len("docker-"):-len("-1")]] = state
    return found


def ops_healthy():
    try:
        return urllib.request.urlopen(OPS_HEALTH, timeout=10).status == 200
    except Exception:  # noqa: BLE001 - any failure means not healthy yet
        return False


def wait_up(seconds=600):
    """Every service running and ops healthy. A service that exited is started again, because
    the adapter can start before LocalStack's init scripts finish creating its queues."""
    deadline = time.time() + seconds
    restarted = {}
    while time.time() < deadline:
        now = states()
        down = [s for s in SERVICES if now.get(s) != "running"]
        for service in down:
            if now.get(service) == "exited" and restarted.get(service, 0) < 3:
                restarted[service] = restarted.get(service, 0) + 1
                subprocess.run(["docker", "start", "docker-{}-1".format(service)],
                               capture_output=True, timeout=60)
        if not down and ops_healthy():
            time.sleep(20)
            if all(states().get(s) == "running" for s in SERVICES):
                return []
        time.sleep(10)
    now = states()
    return ["{} is {}".format(s, now.get(s, "missing")) for s in SERVICES
            if now.get(s) != "running"] or ["ops-api health did not answer 200"]


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else "up"
    problems = checkout_problems() if what == "checkout" else wait_up()
    for problem in problems:
        print("  STACK PROBLEM: {}".format(problem))
    print("  stack {}: {}".format(what, "ok" if not problems else "NOT OK"))
    sys.exit(1 if problems else 0)
