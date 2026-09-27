"""The stack's fake clock: one timeline at one constant rate, shared by every container.

    python3 faketime/clock.py anchor <dir> "@2026-10-25 00:00:00 x10"   write <dir>/timeline.json
    python3 faketime/clock.py run <dir> <out>                           keep <out>/clock current
    python3 faketime/clock.py now <dir>                                 print the fake time

libfaketime reads "@<time> x<rate>" as "<time> at the moment this process read the string", so one
string written before launch would give every process its own timeline: postgres, started a minute
before core, would run ten fake minutes ahead of it at x10, and a restarted container would go
back to the start. So the harness writes the timeline once, before launch, and the faketime-clock
container rewrites <out>/clock every tenth of a second with the timeline's current value. The
other containers set FAKETIME_CACHE_DURATION high enough that each process reads that file once,
when it starts, and never again. Every process then runs at the one rate from the moment it
started, and all of them agree to within a tenth of a second times the rate.

<out> is a Docker volume, not a folder on the host: through Docker Desktop's file sharing 77 of
2000 reads of a file replaced by rename failed, and a process whose one read fails runs on the
real clock for the rest of its life.

The time goes in as epoch seconds (FAKETIME_FMT=%s) because libfaketime parses the string with
mktime in the process's own time zone, and the Java services run in Europe/London while postgres
runs in UTC. musl's strptime has no %s, so clock-utc carries the same instant as a UTC date for
the Alpine container, which runs in UTC.
"""

from __future__ import annotations

import calendar
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

INTERVAL = 0.1
SPEC = re.compile(r"^@(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s+x(\d+(?:\.\d+)?)$")


def parse(spec):
    """The fake start as epoch seconds (the time is UTC) and the rate."""
    match = SPEC.match(spec.strip())
    if not match:
        raise SystemExit("SIM_CLOCK must look like '@2026-10-25 00:00:00 x10', not {!r}".format(spec))
    start = calendar.timegm(time.strptime(match.group(1), "%Y-%m-%d %H:%M:%S"))
    return start, float(match.group(2))


def timeline_path(folder):
    return Path(folder) / "timeline.json"


def anchor(folder, spec, boundary=None):
    start, rate = parse(spec)
    Path(folder).mkdir(parents=True, exist_ok=True)
    state = {"spec": spec, "fakeStart": start, "realStart": time.time(), "rate": rate,
             "boundary": boundary}
    timeline_path(folder).write_text(json.dumps(state) + "\n")
    return state


def load(folder):
    try:
        return json.loads(timeline_path(folder).read_text())
    except (OSError, ValueError):
        return None


def fake_now(state, real=None):
    real = time.time() if real is None else real
    return state["fakeStart"] + (real - state["realStart"]) * state["rate"]


def _replace(path, text):
    # Written beside and renamed, so a process starting mid-write never reads an empty file:
    # libfaketime exits the process when it cannot parse the string.
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(text)
    os.replace(temp, path)


def write(out, state):
    now = fake_now(state)
    rate = "{:g}".format(state["rate"])
    _replace(Path(out) / "clock", "@{:.6f} x{}\n".format(now, rate))
    utc = datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")
    _replace(Path(out) / "clock-utc", "@{} x{}\n".format(utc, rate))


def run(folder, out):
    state = None
    while state is None:
        state = load(folder)
        time.sleep(INTERVAL)
    print("faketime-clock: {} from real {:.3f}".format(state["spec"], state["realStart"]),
          flush=True)
    while True:
        write(out, state)
        time.sleep(INTERVAL)


if __name__ == "__main__":
    command, folder = sys.argv[1], sys.argv[2]
    if command == "anchor":
        boundary = os.environ.get("SIM_CLOCK_BOUNDARY")
        print(anchor(folder, sys.argv[3], json.loads(boundary) if boundary else None))
    elif command == "run":
        run(folder, sys.argv[3])
    elif command == "now":
        state = load(folder)
        if state is None:
            raise SystemExit("no timeline in {}".format(folder))
        print(datetime.fromtimestamp(fake_now(state), timezone.utc).isoformat())
    else:
        raise SystemExit(__doc__)
