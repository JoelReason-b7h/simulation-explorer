"""The harness's one clock: the stack's fake time when it runs on one, the real time otherwise.

The stack runs on a fake clock when `docker/faketime/timeline.json` exists in the stack checkout
(./harness writes it before a launch with a rate and removes it on stop). Everything the harness
sends to the system or compares with the system's own timestamps reads the time here, so a token,
a business date and an "older than" window mean the same instant to the harness and to the
services. Measuring the harness's own work, and waiting on an HTTP answer, stay on the real clock.

The file is read again whenever it changes, because the token server and the webhook sink outlive
a stack relaunch.
"""

from __future__ import annotations

import json
import os
import time as _time
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
UTC = timezone.utc

_cache = {"key": None, "state": None}


def timeline_path():
    repo = os.environ.get("SIM_STACK_REPO",
                          "/Users/joelreason/IdeaProjects/exchange.worktrees/sim-main")
    return Path(repo) / "docker" / "faketime" / "timeline.json"


def timeline():
    """The timeline as faketime/clock.py wrote it, or None when the stack runs on the real clock."""
    path = timeline_path()
    try:
        stat = path.stat()
    except OSError:
        _cache.update(key=None, state=None)
        return None
    key = (stat.st_mtime_ns, stat.st_size)
    if key != _cache["key"]:
        try:
            _cache.update(key=key, state=json.loads(path.read_text()))
        except (OSError, ValueError):
            return _cache["state"]
    return _cache["state"]


def enabled():
    return timeline() is not None


def rate():
    state = timeline()
    return float(state["rate"]) if state else 1.0


def time():
    """Epoch seconds, fake or real, the drop-in for time.time() where the system reads it too."""
    state = timeline()
    real = _time.time()
    if not state:
        return real
    return state["fakeStart"] + (real - state["realStart"]) * state["rate"]


def now(tz=UTC):
    return datetime.fromtimestamp(time(), tz)


def london_now():
    return now(LONDON)


def today():
    """The system's date. Real time keeps date.today(), which is what the harness always used."""
    return london_now().date() if enabled() else date.today()


def utcnow_naive():
    """The drop-in for datetime.utcnow()."""
    return now(UTC).replace(tzinfo=None)


def local_naive():
    """The drop-in for datetime.now() with no zone: the host's local time, fake or real."""
    return datetime.fromtimestamp(time()) if enabled() else datetime.now()


def system_seconds(real_seconds):
    """A real-time allowance as system seconds.

    An "older than five minutes" window over the system's own timestamps was written for the time
    the services take to finish work in the background. That work takes real time, so under a
    fake clock the window grows by the rate and still gives the services the same real minutes.
    """
    return real_seconds * rate()


def system_minutes(real_minutes):
    return int(round(system_seconds(real_minutes * 60) / 60))


def real_seconds(system_secs):
    """How long to wait in real seconds for the system's clock to move by `system_secs`."""
    return system_secs / rate()


def schedulers_run():
    """True when the services' own schedulers do the scheduled work, so the harness's stand-ins
    for those crons stay idle. The fake-clock overlay turns the schedulers on."""
    return enabled()


def describe():
    """What a finding needs to be replayed: the rate, the fake start and the boundary chosen."""
    state = timeline()
    if not state:
        return {"fake": False}
    started = datetime.fromtimestamp(state["fakeStart"], UTC)
    return {"fake": True, "spec": state.get("spec"), "rate": state["rate"],
            "fakeStart": started.isoformat(),
            "realStart": datetime.fromtimestamp(state["realStart"], UTC).isoformat(),
            "boundary": state.get("boundary"), "fakeNow": now().isoformat()}


def age_seconds(stamp):
    """Seconds since an ISO timestamp the system wrote, on the system's clock."""
    moment = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return time() - moment.timestamp()
