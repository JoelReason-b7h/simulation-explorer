"""Everything the harness changes in a stack checkout before the stack launches.

    python3 stack_patch.py <stack checkout>

Safe to run again; it touches only files the compose stack reads, never product code. It applies
local_auth_patch, then:

- turns on the Direct data feed and its RECON file in core and adapter. adapter registers its
  queue consumers only when the flag is on at startup, so both services need both flags;
- turns on the stuck schedule reaper, so a schedule left RUNNING by a restart is reclaimed the way
  it is in a deployed environment;
- sends more of the stack's own traffic through toxiproxy, so a fault can be injected there: core
  to compliance, and clearing's second client to the bank.
- on Linux, maps host.docker.internal to the host in every service. Docker Desktop defines that
  name and Linux Docker does not, and the services reach the harness's webhook sink and its
  Cognito stand-in through it.
- with SIM_CLOCK set (for example "@2026-10-25 00:00:00 x10"), runs every container that reads
  the time on one fake clock at that constant rate, and turns the services' own schedulers on so
  their crons fire on it (faketime/clock.py). Unset, it removes all of that again.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from pathlib import Path

import local_auth_patch

FLAGS = {
    "docker/core.env": ("DIRECT-DATA-FEED", "DIRECT-DATA-RECON", "STUCK-SCHEDULE-REAPER"),
    "docker/adapter.env": ("DIRECT-DATA-FEED", "DIRECT-DATA-RECON"),
}

# (name, listen, upstream) for each proxy launch_stack creates beyond the two it already has.
PROXIES = (
    ("core-to-compliance", "0.0.0.0:20003", "compliance:8075"),
    ("core-to-postgres", "0.0.0.0:20004", "postgres-core:5432"),
    ("clearing-to-postgres", "0.0.0.0:20005", "postgres-clearing:5432"),
)
# The databases stay direct: routed through toxiproxy, core exited during startup (fleet 7).
ROUTES = {
    "docker/core.env": ("B7H_SERVICE_COMPLIANCE_URL=http://toxiproxy:20003",
                        "B7H_DATABASE_CORE_HOSTNAME=postgres-core"),
    "docker/clearing.env": ("B7H_DATABASE_CLEARING_HOSTNAME=postgres-clearing",
                            "B7H_DATABASE_CLEARING_PORT=5432",
                            "B7H_SERVICE_HOT_SAUCE_BANK_URL=http://toxiproxy:20002"),
}
DROPPED = {"docker/core.env": ("B7H_DATABASE_CORE_PORT",)}
LAUNCH = "scripts/launch_stack"
ANCHOR = "create_proxy clearing-to-bank 0.0.0.0:20002 hot-sauce-bank:10002\n"

HOST_OVERLAY = "docker/docker-compose.linux-host.yml"
HOST_SERVICES = ("adapter", "clearing", "compliance", "compliance-api", "core", "core-ro",
                 "hot-sauce-bank", "notification", "ops-api", "public-api", "simulator-api")
COMPOSE_START = "COMPOSE_FILES=(-f ./docker/docker-compose.yml"

HERE = Path(__file__).resolve().parent
CLOCK_OVERLAY = "docker/docker-compose.faketime.yml"
CLOCK_DIR = "docker/faketime"
LOCAL_UP = "local-up.sh"
# local-up.sh starts postgres, redis and LocalStack with the base file alone, so the clock overlay
# has to be named there too, or those three run on the real clock.
LOCAL_UP_LINE = "docker compose -f docker/docker-compose.yml up -d --remove-orphans"
# Every Java service the stack starts, and the ones whose acceptance profile turns their schedulers
# off. application-acceptance.yml sets b7h.async.scheduling.enabled to false outright, so
# B7H_ENV_ASYNC_SCHEDULING_ENABLED alone (read only by application.yml's default) does not win.
CLOCK_JAVA = ("adapter", "clearing", "compliance", "compliance-api", "core", "core-ro",
              "hot-sauce-bank", "notification", "ops-api", "public-api", "simulator-api")
CLOCK_SCHEDULERS = ("adapter", "clearing", "compliance", "core")
CLOCK_ENV = ("FAKETIME_DONT_FAKE_MONOTONIC=0",
             # Read the clock once, when the process starts; see faketime/clock.py.
             "FAKETIME_CACHE_DURATION=1000000000")


def _set_lines(path, wanted):
    """Set each KEY=value in place, and append only the keys the file does not have.

    Moving the keys to the end each time made two calls on one file, the flags and then the routes
    in core.env, reorder each other's lines on every run. The patch then always reported a change,
    and every cycle wiped the stack, which removed the evidence of the cycle before it.
    """
    before = path.read_text()
    values = dict(line.split("=", 1) for line in wanted)
    lines, seen = [], set()
    for line in before.splitlines():
        key = line.split("=", 1)[0]
        if key in values:
            if key in seen:
                continue
            seen.add(key)
            lines.append("{}={}".format(key, values[key]))
        else:
            lines.append(line)
    lines += [line for line in wanted if line.split("=", 1)[0] not in seen]
    after = "\n".join(lines) + "\n"
    if after == before:
        return False
    path.write_text(after)
    return True


def _add_proxies(path):
    text = path.read_text()
    if ANCHOR not in text:
        raise SystemExit("{} has no clearing-to-bank proxy line to add the others after".format(
            path))
    missing = ["create_proxy {} {} {}\n".format(*proxy) for proxy in PROXIES
               if "create_proxy {} ".format(proxy[0]) not in text]
    if not missing:
        return False
    path.write_text(text.replace(ANCHOR, ANCHOR + "".join(missing)))
    return True


def _add_host_overlay(checkout):
    overlay = checkout / HOST_OVERLAY
    wanted = "services:\n" + "".join(
        "  {}:\n    extra_hosts:\n      - \"host.docker.internal:host-gateway\"\n".format(s)
        for s in HOST_SERVICES)
    changed = False
    if not overlay.exists() or overlay.read_text() != wanted:
        overlay.write_text(wanted)
        changed = True
    launch = checkout / LAUNCH
    text = launch.read_text()
    if HOST_OVERLAY not in text:
        # Matched on its start, not the whole line: a checkout copied from the Mac can already
        # carry the fake-clock overlay on it.
        if COMPOSE_START not in text:
            raise SystemExit("{} has no COMPOSE_FILES line to add {} to".format(launch, HOST_OVERLAY))
        launch.write_text(text.replace(COMPOSE_START, "{} -f ./{}".format(COMPOSE_START,
                                                                          HOST_OVERLAY), 1))
        changed = True
    return changed


def _arch():
    machine = platform.machine().lower()
    return "arm64" if machine in ("arm64", "aarch64") else "amd64"


# In LocalStack's own ready.d folder: a file bind-mounted inside that folder's bind mount fails on
# Docker Desktop ("mountpoint is outside of rootfs") and leaves an empty file in its place.
SQS_VISIBILITY = "docker/localstack/998_faketime_sqs_visibility.sh"
# LocalStack's default, and the one queue 002_sqs.sh shortens.
SQS_DEFAULT_VISIBILITY, SQS_SHORT = 30, {"eventbridge-icac-clearing": 5}


def _timeouts(checkout):
    """faketime/timeouts.json, rebuilt first when the checkout has the services' source."""
    import faketime.timeouts
    if (checkout / "apps").exists():
        table = faketime.timeouts.build(checkout)
        _write_if_changed(HERE / "faketime" / "timeouts.json",
                          json.dumps(table, indent=1, sort_keys=True) + "\n")
    return json.loads((HERE / "faketime" / "timeouts.json").read_text())


def _sqs_visibility(rate):
    """A LocalStack ready.d script that stretches every queue's visibility timeout by the rate.

    LocalStack counts it on the fake clock, so at x10 a consumer that held a message for more than
    three real seconds saw it delivered again; 998 runs after 002_sqs.sh makes the queues and
    before 999_finish.sh marks LocalStack healthy."""
    lines = ["#!/bin/sh", "# Written by simulation-explorer's stack_patch.py because SIM_CLOCK is set.",
             "export AWS_DEFAULT_REGION=eu-west-2",
             "for url in $(awslocal sqs list-queues --output text --query 'QueueUrls[]'); do",
             "  awslocal sqs set-queue-attributes --queue-url \"$url\" "
             "--attributes VisibilityTimeout={}".format(int(SQS_DEFAULT_VISIBILITY * rate)),
             "done"]
    for queue, seconds in SQS_SHORT.items():
        lines.append("awslocal sqs set-queue-attributes --queue-url \"$(awslocal sqs get-queue-url "
                     "--queue-name={} --output text --query QueueUrl)\" --attributes "
                     "VisibilityTimeout={}".format(queue, int(seconds * rate)))
    return "\n".join(lines) + "\n"


def _clock_overlay(rate, timeouts):
    """The compose overlay for SIM_CLOCK. Wiremock (Ubuntu 22.04, older than the trixie build)
    and toxiproxy (a static Go binary) stay on the real clock: neither reads the time for anything
    the services depend on.

    Every Java service's timeouts are multiplied by the rate (faketime/timeouts.py), because
    libfaketime runs its timers at the rate too."""
    import faketime.timeouts
    def glibc(extra=()):
        env = ("LD_PRELOAD=/faketime/trixie/libfaketimeMT.so.1",
               "FAKETIME_TIMESTAMP_FILE=/run/faketime/clock", "FAKETIME_FMT=%s",
               # The images drop root after their entrypoint starts, and the shared-memory
               # semaphore the entrypoint created is then unreadable, which hangs postgres.
               "FAKETIME_DISABLE_SHM=1") + CLOCK_ENV + tuple(extra)
        return env

    def service(name, env, more=""):
        return ("  {}:\n{}    depends_on:\n      faketime-clock:\n        condition: service_healthy\n"
                "    volumes:\n      - ./faketime:/faketime:ro\n      - faketime-clock:/run/faketime:ro\n"
                "    environment:\n{}").format(
                    name, more, "".join("      - {}\n".format(line) for line in env))

    parts = ["# Written by simulation-explorer's stack_patch.py because SIM_CLOCK is set.\n",
             "volumes:\n  faketime-clock: {}\n",
             "services:\n",
             "  faketime-clock:\n"
             "    image: localstack/localstack:pinned\n"
             "    entrypoint: [\"python3\", \"/faketime/clock.py\", \"run\", \"/faketime\", \"/run/faketime\"]\n"
             "    network_mode: none\n"
             "    restart: unless-stopped\n"
             "    mem_limit: 64m\n"
             "    memswap_limit: 64m\n"
             "    volumes:\n      - ./faketime:/faketime:ro\n      - faketime-clock:/run/faketime\n"
             "    healthcheck:\n"
             "      test: [\"CMD\", \"test\", \"-s\", \"/run/faketime/clock\"]\n"
             "      interval: 2s\n      retries: 30\n"]
    parts.append(service("postgres", glibc()))
    parts.append(service("localstack", glibc()))
    # musl: no %s in strptime, and no FAKETIME_DISABLE_SHM in Alpine's build. Behind the image's
    # entrypoint script redis-server kept the real clock, so it runs as PID 1 under its own user.
    parts.append(service("redis", ("LD_PRELOAD=/faketime/alpine/libfaketimeMT.so.1",
                                   "FAKETIME_TIMESTAMP_FILE=/run/faketime/clock-utc") + CLOCK_ENV,
                         "    user: redis\n    entrypoint: []\n"))
    for name in CLOCK_JAVA:
        schedulers = ("B7H_ENV_ASYNC_SCHEDULING_ENABLED=true", "B7H_ASYNC_SCHEDULING_ENABLED=true")
        extra = schedulers if name in CLOCK_SCHEDULERS else ()
        extra += tuple("{}={}".format(key, faketime.timeouts.scaled(value, rate))
                       for key, value in sorted((timeouts.get(name) or {}).items()))
        parts.append(service(name, glibc(extra)))
    return "".join(parts)


def _write_if_changed(path, content, binary=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    before = path.read_bytes() if path.exists() else None
    after = content if binary else content.encode()
    if before == after:
        return False
    path.write_bytes(after)
    return True


def _set_clock(checkout, spec):
    """Adds or removes the clock overlay. The timeline itself is written by ./harness just before
    a launch, so that a restart of the same stack keeps the same timeline."""
    launch, local_up = checkout / LAUNCH, checkout / LOCAL_UP
    launch_text, up_text = launch.read_text(), local_up.read_text()
    overlay_arg = " -f ./{}".format(CLOCK_OVERLAY)
    up_arg = " -f {}".format(CLOCK_OVERLAY)
    changed = False
    if spec:
        import faketime.clock
        _, rate = faketime.clock.parse(spec)
        folder = checkout / CLOCK_DIR
        for base in ("trixie", "alpine"):
            lib = HERE / "faketime" / "{}-{}".format(base, _arch()) / "libfaketimeMT.so.1"
            changed = _write_if_changed(folder / base / lib.name, lib.read_bytes(), True) or changed
        changed = _write_if_changed(folder / "clock.py",
                                    (HERE / "faketime" / "clock.py").read_text()) or changed
        changed = _write_if_changed(checkout / SQS_VISIBILITY, _sqs_visibility(rate)) or changed
        (checkout / SQS_VISIBILITY).chmod(0o755)
        changed = _write_if_changed(checkout / CLOCK_OVERLAY,
                                    _clock_overlay(rate, _timeouts(checkout))) or changed
        if overlay_arg not in launch_text:
            if COMPOSE_START not in launch_text:
                raise SystemExit("{} has no COMPOSE_FILES line to add {} to".format(launch, CLOCK_OVERLAY))
            launch.write_text(launch_text.replace(COMPOSE_START, COMPOSE_START + overlay_arg, 1))
            changed = True
        if up_arg not in up_text:
            if LOCAL_UP_LINE not in up_text:
                raise SystemExit("{} has no compose line to add {} to".format(local_up, CLOCK_OVERLAY))
            local_up.write_text(up_text.replace(
                LOCAL_UP_LINE, LOCAL_UP_LINE.replace(" up -d", up_arg + " up -d")))
            changed = True
        return changed
    if overlay_arg in launch_text:
        launch.write_text(launch_text.replace(overlay_arg, ""))
        changed = True
    if up_arg in up_text:
        local_up.write_text(up_text.replace(up_arg, ""))
        changed = True
    if (checkout / CLOCK_OVERLAY).exists():
        (checkout / CLOCK_OVERLAY).unlink()
        changed = True
    if (checkout / CLOCK_DIR).exists():
        shutil.rmtree(checkout / CLOCK_DIR)
    if (checkout / SQS_VISIBILITY).exists():
        (checkout / SQS_VISIBILITY).unlink()
        changed = True
    return changed


def patch(checkout):
    """Returns True when a file changed, because a running stack read them all at start."""
    checkout = Path(checkout)
    changed = local_auth_patch.patch(checkout)
    for name, flags in FLAGS.items():
        wanted = ["B7H_ENV_FEATURE-FLAG_{}_ENABLED=true".format(flag) for flag in flags]
        changed = _set_lines(checkout / name, wanted) or changed
    for name, lines in ROUTES.items():
        changed = _set_lines(checkout / name, lines) or changed
    for name, keys in DROPPED.items():
        path = checkout / name
        before = path.read_text()
        after = "\n".join(line for line in before.splitlines()
                          if line.split("=", 1)[0] not in keys) + "\n"
        if after != before:
            path.write_text(after)
            changed = True
    changed = _add_proxies(checkout / LAUNCH) or changed
    if platform.system() == "Linux":
        changed = _add_host_overlay(checkout) or changed
    changed = _set_clock(checkout, os.environ.get("SIM_CLOCK", "").strip()) or changed
    print("stack {} in {}".format("patched" if changed else "already patched", checkout))
    return changed


if __name__ == "__main__":
    patch(sys.argv[1])
