"""Which service of the stack is down, or came back too recently to judge a 5xx against.

The run knew only about the outages it caused itself or read from another run's events. Clearing
stopped in fleets 558 and 588 with nothing recording it, and core then answered 500 "Connection
reset" on every call that reached clearing, which the run reported as unexplained 5xx. Docker
knows every stop and start whoever caused it, so the run asks Docker.
"""

from __future__ import annotations

import subprocess
import time
from datetime import datetime, timezone

from explorer import fleet

# A container started this many seconds ago is still coming back. Core takes about five minutes
# to start, and core's pooled connections to a restarted service fail for a while after it is
# back, which is how fleet567's 500s fell outside the 150-second restart window.
SETTLE_SECONDS = {"docker-core-1": 420, "docker-core-ro-1": 420}
DEFAULT_SETTLE_SECONDS = 180

CONTAINERS = (
    "docker-core-1", "docker-core-ro-1", "docker-clearing-1", "docker-compliance-1",
    "docker-compliance-api-1", "docker-public-api-1", "docker-ops-api-1",
    "docker-hot-sauce-bank-1", "docker-adapter-1", "docker-simulator-api-1",
    "docker-postgres-1", "docker-localstack-1", "docker-redis-1", "docker-toxiproxy-1",
)

CACHE_SECONDS = 3.0
_cache = {"at": 0.0, "answer": None}
_noted = set()


def _started(text):
    # Docker prints nanoseconds, which fromisoformat refuses.
    head, _, rest = text.partition(".")
    seconds = datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    return seconds.timestamp()


def _inspect():
    try:
        done = subprocess.run(
            ["docker", "inspect", "--format",
             "{{.Name}} {{.State.Running}} {{.State.StartedAt}}"] + list(CONTAINERS),
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    # A container that does not exist makes docker exit 1 but still print the others.
    rows = []
    for line in done.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3:
            rows.append((parts[0].lstrip("/"), parts[1] == "true", parts[2]))
    return rows or None


def current():
    """What is down or still coming back, as a sentence, or None when the stack is settled."""
    now = time.time()
    if now - _cache["at"] < CACHE_SECONDS:
        return _cache["answer"]
    answer = None
    for name, running, started in _inspect() or ():
        if not running:
            answer, key = "{} is stopped".format(name), (name, "stopped")
        else:
            try:
                age = now - _started(started)
            except ValueError:
                continue
            if age >= SETTLE_SECONDS.get(name, DEFAULT_SETTLE_SECONDS):
                continue
            answer, key = "{} started {:.0f}s ago".format(name, age), (name, started)
        # Write each outage to the events file once, so a cycle's record shows the outages
        # nobody injected as well as the ones a run did.
        if key not in _noted:
            _noted.add(key)
            fleet.note("outage", answer)
        break
    _cache.update(at=now, answer=answer)
    return answer
