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
"""

from __future__ import annotations

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
    print("stack {} in {}".format("patched" if changed else "already patched", checkout))
    return changed


if __name__ == "__main__":
    patch(sys.argv[1])
