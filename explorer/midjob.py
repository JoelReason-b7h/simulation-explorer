"""Restart a service the moment one of its scheduled jobs starts, then check the job recovers.

A plain restart lands wherever the run happens to be, which is almost never inside a job: the
minute schedulers hold their work for well under a second. Watching for a job to start and
restarting its service there kills the job part way through, which is the case a stuck RUNNING
schedule row or a half-written batch comes from.

Two places say a job has started. A schedule row in core's `bank_event_schedule` or
`platform_event_schedule` goes RUNNING with a `run_started_at`. Every ShedLock job, in core,
clearing and compliance, writes a new `locked_at` to `shedlock` when it takes its lock. A held
lock alone says nothing, because `lock_until` stays in the future after the job ends.
"""

from __future__ import annotations

import os
import time
from datetime import datetime

from explorer.integrity import CLEARING_DSN, CORE_DSN, _psql_on

COMPLIANCE_DSN = os.environ.get(
    "SIM_COMPLIANCE_DSN", "postgresql://compliance:password@localhost:5441/compliance")
DSN = {"core": CORE_DSN, "clearing": CLEARING_DSN, "compliance": COMPLIANCE_DSN}

WATCH_SECONDS = 25.0
POLL_SECONDS = 0.3
# Real seconds after the restarted service is healthy again before a job still RUNNING, or a
# minute job that never took its lock again, is a finding. Ten real minutes is a hundred fake.
RECOVERY_SECONDS = 600.0

SCHEDULE_TABLES = ("bank_event_schedule", "platform_event_schedule")


def _at(text):
    return datetime.fromisoformat(text[:19])


def _locks(service):
    rows, _ = _psql_on(DSN[service], "SELECT name, locked_at FROM shedlock", timeout=10)
    return {name: at for name, at in rows}


def _running():
    found = []
    for table in SCHEDULE_TABLES:
        rows, _ = _psql_on(CORE_DSN, "SELECT sid, event_type, run_started_at FROM {} "
                                     "WHERE status = 'RUNNING'".format(table), timeout=10)
        found += [{"table": table, "sid": sid, "job": event_type, "started": started}
                  for sid, event_type, started in rows]
    return found


class Watcher:
    def __init__(self):
        self.seen = {service: {} for service in DSN}
        # Each lock's last two locked_at values, so a minute job can be told from a daily one.
        self.history = {}
        self.pending = []

    def _record(self, service, locks):
        for name, at in locks.items():
            past = self.history.setdefault((service, name), [])
            if not past or past[-1] != at:
                past.append(at)
                del past[:-3]
        self.seen[service] = locks

    def wait_for_start(self):
        """The first job to start within the watch, as a dict, or None."""
        for service in DSN:
            self._record(service, _locks(service))
        deadline = time.time() + WATCH_SECONDS
        while time.time() < deadline:
            running = _running()
            if running:
                job = running[0]
                return dict(job, service="core", kind="schedule")
            for service in DSN:
                before = self.seen[service]
                locks = _locks(service)
                self._record(service, locks)
                for name, at in locks.items():
                    if before and before.get(name) != at:
                        return {"service": service, "kind": "lock", "job": name, "started": at,
                                "minutely": self._minutely(service, name)}
            time.sleep(POLL_SECONDS)
        return None

    def _minutely(self, service, name):
        """True when the lock's last two takes were at most ten fake minutes apart."""
        past = self.history.get((service, name)) or []
        if len(past) < 2:
            return False
        try:
            gap = _at(past[-1]) - _at(past[-2])
        except ValueError:
            return False
        return 0 < gap.total_seconds() <= 600

    def interrupted(self, job):
        self.pending.append(dict(job, healthyAt=time.time()))

    def check(self, note):
        """Report each interrupted job that has not recovered once its time is up."""
        due = [j for j in self.pending if time.time() - j["healthyAt"] >= RECOVERY_SECONDS]
        for job in due:
            self.pending.remove(job)
            if job["kind"] == "schedule":
                rows, error = _psql_on(CORE_DSN, "SELECT status, run_started_at FROM {} WHERE sid "
                                                 "= {}".format(job["table"], int(job["sid"])))
                if error or not rows:
                    continue
                status, started = rows[0]
                if status == "RUNNING" and started == job["started"]:
                    note("a job interrupted by a restart is not left RUNNING",
                         "{} {}".format(job["table"], job["sid"]),
                         "{} {} went RUNNING at {}, {} was restarted, and {:.0f}s after it came "
                         "back the row is still RUNNING from that start".format(
                             job["table"], job["job"], started, job["service"],
                             RECOVERY_SECONDS), "not RUNNING", "RUNNING since " + started)
            elif job.get("minutely"):
                at = _locks(job["service"]).get(job["job"])
                if at == job["started"]:
                    note("a job interrupted by a restart runs again", job["job"],
                         "{} on {} took its lock at {} and was restarted, and {:.0f}s after the "
                         "service came back it has not taken the lock again".format(
                             job["job"], job["service"], job["started"], RECOVERY_SECONDS),
                         "a later locked_at", job["started"])
