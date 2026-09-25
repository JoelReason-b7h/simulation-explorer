"""Everything the harness changes in a stack checkout before the stack launches.

    python3 stack_patch.py <stack checkout>

Safe to run again; it touches only files the compose stack reads, never product code. It applies
local_auth_patch, then turns on the features the harness checks and a plain local stack leaves off:

- the Direct data feed and its RECON file. core decides what to send and adapter writes the files,
  and adapter registers its queue consumers only when the flag is on at startup, so both services
  need both flags;
- the stuck schedule reaper, so a schedule left RUNNING by a restart is reclaimed the way it is in
  a deployed environment.
"""

from __future__ import annotations

import sys
from pathlib import Path

import local_auth_patch

FLAGS = {
    "docker/core.env": ("DIRECT-DATA-FEED", "DIRECT-DATA-RECON", "STUCK-SCHEDULE-REAPER"),
    "docker/adapter.env": ("DIRECT-DATA-FEED", "DIRECT-DATA-RECON"),
}


def _set_lines(path, wanted):
    before = path.read_text()
    keys = {line.split("=", 1)[0] for line in wanted}
    lines = [line for line in before.splitlines() if line.split("=", 1)[0] not in keys]
    after = "\n".join(lines + list(wanted)) + "\n"
    if after == before:
        return False
    path.write_text(after)
    return True


def patch(checkout):
    """Returns True when a file changed, because a running stack read them all at start."""
    checkout = Path(checkout)
    changed = local_auth_patch.patch(checkout)
    for name, flags in FLAGS.items():
        wanted = ["B7H_ENV_FEATURE-FLAG_{}_ENABLED=true".format(flag) for flag in flags]
        changed = _set_lines(checkout / name, wanted) or changed
    print("stack {} in {}".format("patched" if changed else "already patched", checkout))
    return changed


if __name__ == "__main__":
    patch(sys.argv[1])
