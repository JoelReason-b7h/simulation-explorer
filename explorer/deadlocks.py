"""Lock deadlocks the stack's Postgres cannot detect, found and broken by the harness.

The box runs Postgres under libfaketime, so its timers never fire: deadlock_timeout (1s) never
triggers and statement_timeout never cancels. Compliance sat in two deadlocks for about 600
cycles, `CustomerClosureService.closeCustomer` updating `customer` then `kyc_check` against
`KycCheckRepository.updateCheckStatus` updating `kyc_check` then `customer`. Production
Postgres would abort one of the two transactions after a second. sweep() does that and writes
each deadlock to deadlocks.jsonl, so after_cycle can report it:

    python3 -m explorer.deadlocks          # one sweep

Postgres's now() runs on the same fake clock as the stack, so "waited 60s" is about six real
seconds on a x10 clock.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
LOG = HERE / "deadlocks.jsonl"
SEEN = HERE / "deadlocks.seen.json"

DATABASES = (("core", "postgresql://core:password@localhost:5432/core"),
             ("clearing", "postgresql://clearing:password@localhost:5440/clearing"),
             ("compliance", "postgresql://compliance:password@localhost:5441/compliance"),
             ("hsb", "postgresql://hsb:password@localhost:5444/hsb"),
             ("pay_by_bank", "postgresql://pay_by_bank:password@localhost:5432/pay_by_bank"))

# Seconds on the database's clock, which is the fake one.
DEADLOCK_WAIT = 60
LONG_WAIT = 30 * 60
PSQL_TIMEOUT = 15
INTERVAL = 10
SIGNATURE_CHARS = 120

SQL = """
SELECT coalesce(json_agg(t), '[]'::json) FROM (
  SELECT pid,
         extract(epoch FROM now() - xact_start) AS xact_age,
         extract(epoch FROM xact_start) AS xact_start,
         extract(epoch FROM query_start) AS query_start,
         extract(epoch FROM now() - query_start) AS wait,
         wait_event_type, application_name, query, pg_blocking_pids(pid) AS blockers
  FROM pg_stat_activity
  WHERE datname = current_database() AND pid <> pg_backend_pid()
    AND (wait_event_type = 'Lock' OR cardinality(pg_blocking_pids(pid)) > 0)
) t
"""


def signature_of(query):
    text = re.sub(r"\s+", " ", query or "").strip()
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>", text,
                  flags=re.I)
    text = re.sub(r"'(?:[^']|'')*'", "<v>", text)
    text = re.sub(r"\d+(\.\d+)?", "N", text)
    return text[:SIGNATURE_CHARS]


def signature(queries):
    return " <-> ".join(sorted(signature_of(q) for q in queries))


def cycles(waiters):
    """The groups of waiting pids that wait on each other, from {pid: [blocking pids]}.

    A group is a strongly connected component of more than one pid, or a pid blocked by itself.
    """
    index, low, on_stack, stack, found = {}, {}, set(), [], []
    counter = [0]

    def visit(root):
        # Iterative Tarjan, so a long chain of waiters cannot hit the recursion limit.
        index[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(waiters[root]))]
        while work:
            node, edges = work[-1]
            advanced = False
            for target in edges:
                if target not in waiters:
                    continue
                if target not in index:
                    index[target] = low[target] = counter[0]
                    counter[0] += 1
                    stack.append(target)
                    on_stack.add(target)
                    work.append((target, iter(waiters[target])))
                    advanced = True
                    break
                if target in on_stack:
                    low[node] = min(low[node], index[target])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                group = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    group.append(member)
                    if member == node:
                        break
                if len(group) > 1 or node in waiters[node]:
                    found.append(sorted(group))

    for pid in sorted(waiters):
        if pid not in index:
            visit(pid)
    return found


def _env():
    return dict(os.environ, PGCONNECT_TIMEOUT="5")


def _read(dsn):
    done = subprocess.run(["psql", dsn, "-tA", "-v", "ON_ERROR_STOP=1", "-c", SQL],
                          capture_output=True, text=True, timeout=PSQL_TIMEOUT, env=_env())
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip()[:200] or "psql exit {}".format(done.returncode))
    return json.loads(done.stdout.strip() or "[]")


def _member(row):
    return {"pid": row["pid"], "xactAgeSeconds": round(row["xact_age"] or 0),
            "application": row["application_name"],
            "query": re.sub(r"\s+", " ", row["query"] or "").strip()}


_reported = set()


def judge_rows(db, rows, now):
    """The lines to write and the pids to terminate for one database's activity rows."""
    by_pid = {r["pid"]: r for r in rows}
    waiting = {r["pid"]: list(r["blockers"]) for r in rows
               if r["wait_event_type"] == "Lock" and (r["wait"] or 0) > DEADLOCK_WAIT}
    lines, victims, grouped = [], [], set()
    for group in cycles(waiting):
        grouped.update(group)
        key = (db, tuple((p, by_pid[p]["query_start"]) for p in group))
        if key in _reported:
            continue
        _reported.add(key)
        members = [_member(by_pid[p]) for p in group]
        # Postgres aborts one transaction of the cycle. The newest has done the least work.
        victim = max(group, key=lambda p: by_pid[p]["xact_start"] or 0)
        lines.append({"at": now, "kind": "deadlock", "db": db,
                      "signature": signature(m["query"] for m in members),
                      "members": members, "victim": victim})
        victims.append(victim)
    for pid, blockers in waiting.items():
        row = by_pid[pid]
        if pid in grouped or (row["wait"] or 0) <= LONG_WAIT:
            continue
        key = (db, "long", pid, row["query_start"])
        if key in _reported:
            continue
        _reported.add(key)
        lines.append({"at": now, "kind": "longLockWait", "db": db,
                      "signature": signature_of(row["query"]),
                      "waitSeconds": round(row["wait"]),
                      "members": [_member(row)] + [_member(by_pid[b]) for b in blockers
                                                   if b in by_pid]})
    return lines, victims


def _terminate(dsn, pid):
    subprocess.run(["psql", dsn, "-tA", "-c", "SELECT pg_terminate_backend({})".format(int(pid))],
                   capture_output=True, text=True, timeout=PSQL_TIMEOUT, env=_env())


def sweep():
    """One pass over every database. Never raises."""
    now = time.time()
    for db, dsn in DATABASES:
        try:
            lines, victims = judge_rows(db, _read(dsn), now)
            for pid in victims:
                _terminate(dsn, pid)
            if lines:
                with open(LOG, "a") as handle:
                    for line in lines:
                        handle.write(json.dumps(line) + "\n")
                        print("  deadlock watchdog: {} {} {}".format(
                            line["kind"], db, line["signature"][:200]), flush=True)
        except Exception as fault:  # noqa: BLE001 - a watchdog fault must not reach the cycle
            print("  deadlock watchdog: {} sweep failed: {}".format(db, repr(fault)[:200]),
                  flush=True)


def _loop():
    while True:
        try:
            sweep()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(INTERVAL)


def start():
    """Sweep every INTERVAL real seconds on a daemon thread, which ends with the process."""
    threading.Thread(target=_loop, name="deadlock-watchdog", daemon=True).start()


def window(since):
    """The lines written at or after the epoch `since`."""
    try:
        text = LOG.read_text(errors="replace")
    except OSError:
        return []
    found = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("at", 0) >= since:
            found.append(entry)
    return found


def judge(name, since):
    """(all, new): the window's lines grouped by signature with counts, and the deadlock
    signatures never seen before. Records every deadlock signature as seen."""
    try:
        seen = json.loads(SEEN.read_text())
    except (OSError, ValueError):
        seen = {}
    grouped = {}
    for entry in window(since):
        key = (entry["kind"], entry["db"], entry["signature"])
        group = grouped.setdefault(key, {"kind": entry["kind"], "db": entry["db"],
                                         "signature": entry["signature"], "count": 0})
        group["count"] += 1
    everything = sorted(grouped.values(), key=lambda g: -g["count"])
    new = []
    for group in everything:
        if group["kind"] != "deadlock":
            continue
        key = group["signature"]
        if key not in seen:
            new.append(group)
            seen[key] = {"first": name, "last": name}
        else:
            seen[key]["last"] = name
    if everything:
        SEEN.write_text(json.dumps(seen, indent=1, sort_keys=True))
    return everything, new


if __name__ == "__main__":
    sweep()
