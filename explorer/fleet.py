"""What the other platforms' runs did to the shared stack, for a fleet of runs on one stack.

Each platform runs its own explore.py. A settlement sweep, a business day, a restart or a network
fault started by one of them moves the others' money too, so a run that only knew its own
in-flight work would report the effect as a defect with no cause. Every run appends the work it
starts to one file, and reads the other runs' recent entries back as in-flight work of its own.

With SIM_FLEET_EVENTS unset the run is alone, and nothing here does anything.
"""

from __future__ import annotations

import json
import os
import time

EVENTS = os.environ.get("SIM_FLEET_EVENTS")
ROLE = os.environ.get("SIM_FLEET_ROLE", "conductor")
NAME = os.environ.get("SIM_RUN_NAME", "run")

# Seconds, not trials: the runs step at different rates, so one run's trial count says nothing
# about how long ago another run's work started.
WINDOW_SECONDS = {
    "compliance": 40,
    "closure payment": 40,
    "restart": 150,
    "settlement": 25,
    "closure": 20,
    "interest": 20,
    "notice": 25,
    "fault": 60,
}
DEFAULT_WINDOW_SECONDS = 30
KEEP_BYTES = 2_000_000
READ_BYTES = 64_000

# Actions that change the stack for every platform at once. Only the conductor takes them, because
# two runs each healing the other's fault leave toxiproxy in whichever state was set last.
STACK_FAULTS = {
    "SlowTheBank", "BreakTheBank", "SlowClearing", "BreakClearing",
    "SlowBankForClearing", "BreakBankForClearing", "HealTheNetwork",
    "RestartClearing", "RestartCore", "RestartBank",
    "FundAccountInterrupted", "FundAccountDuplicated",
    "DuplicateMessages", "StopDuplicating",
    "RejectClosurePayment", "ReturnClosurePayment", "CloseNoticeAfterDue",
    # Bank-wide, so one run asks for it: the feed of one bank is one sequence of files.
    "RunDataFeed",
}


def is_member():
    return bool(EVENTS) and ROLE == "member"


def note(kind, what):
    if not EVENTS:
        return
    line = json.dumps({"at": time.time(), "run": NAME, "kind": kind, "what": what}) + "\n"
    try:
        if os.path.exists(EVENTS) and os.path.getsize(EVENTS) > KEEP_BYTES:
            with open(EVENTS, "rb") as handle:
                handle.seek(-KEEP_BYTES // 2, os.SEEK_END)
                tail = handle.read()
            with open(EVENTS, "wb") as handle:
                handle.write(tail[tail.find(b"\n") + 1:])
        with open(EVENTS, "a") as handle:
            handle.write(line)
    except OSError:
        pass


def others_in_flight():
    """The other runs' work that could still be landing now, as in-flight entries."""
    if not EVENTS:
        return []
    try:
        with open(EVENTS, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - READ_BYTES))
            lines = handle.read().decode(errors="replace").splitlines()
    except OSError:
        return []
    now = time.time()
    live = []
    for raw in lines:
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if entry.get("run") == NAME:
            continue
        window = WINDOW_SECONDS.get(entry.get("kind"), DEFAULT_WINDOW_SECONDS)
        if 0 <= now - entry.get("at", 0) <= window:
            live.append({"kind": entry["kind"], "what": "{} on {}".format(entry["what"],
                                                                           entry["run"]),
                         "subject": None, "trial": None})
    return live


def _subjects_file():
    return EVENTS[:-len(".events.jsonl")] + ".subjects.jsonl" if EVENTS else None


def share_subject(customer_id, account_id=None, product_id=None):
    """Tell the other runs about a customer this platform holds, so they can try to reach it."""
    path = _subjects_file()
    if not path or not customer_id:
        return
    try:
        with open(path, "a") as handle:
            handle.write(json.dumps({"run": NAME, "customerId": customer_id,
                                     "accountId": account_id, "productId": product_id}) + "\n")
    except OSError:
        pass


def foreign_subject(pick):
    """A customer of another platform, chosen with `pick` from the most recent ones, or None."""
    path = _subjects_file()
    if not path:
        return None
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - READ_BYTES))
            lines = handle.read().decode(errors="replace").splitlines()
    except OSError:
        return None
    theirs = []
    for raw in lines:
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if entry.get("run") != NAME:
            theirs.append(entry)
    return pick(theirs) if theirs else None
