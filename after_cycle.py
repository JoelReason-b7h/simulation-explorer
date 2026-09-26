"""After a fleet cycle: check the harness behaved, check the bank's files, set the next length.

    python3 after_cycle.py <run name>

A cycle is quiet when the harness behaved and nothing new was found. After a quiet cycle the next
one runs twice as long, up to MAX_SECONDS; after anything else it goes back to MIN_SECONDS, so a
new finding or a harness fault is looked at while it is still small.

The page JSON of each run stays, because it carries every finding with its evidence. The logs,
the trial files and the fleet files go into archive/<run name>.tgz.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tarfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FINDINGS = HERE / "findings"
PROGRESS = HERE / "fleet.progress"
LENGTH = HERE / "fleet.seconds"
MIN_SECONDS = 900
MAX_SECONDS = 14400

# Findings already in FINDINGS.md, or measurements rather than defects, as (rule, action). An
# action of None covers the rule on every action. Any other violation makes the cycle interesting,
# so a 5xx on an action not listed here still brings the cycle back to fifteen minutes.
KNOWN = {
    ("a terminal status is not left", None),
    ("a closed account holds no money", None),
    ("a read answers promptly", None),
    ("clearing and the bank agree on how a payment ended", None),
    ("a closure the bank refused still finishes", None),
    ("the service's own integrity check passes", "TransitionChainSweep"),
    ("no unexplained 5xx", "CancelAccountOpening"),
    ("a fault is survived without a server error", "CancelAccountOpening"),
    ("a fault is survived without a server error", "CloseAccount"),
    # Local artefact: each cycle makes a new Direct bank, and core keeps a DIRECT account per bank
    # while clearing holds one Investec DIRECT nostro for all of them, so the second bank's
    # accounts have nothing to match in clearing.
    ("core's Direct reconciliation with clearing passes", None),
}

# Known findings a rule reports under one action for many causes, told apart by their detail.
KNOWN_DETAIL = {
    ("no message goes to the dead letter queue", '"detail-type":"PaymentSettled"'),
}


def is_known(violation):
    rule, action = violation.get("rule"), (violation.get("action") or "").replace("RACE ", "")
    if any(rule == known and text in str(violation.get("detail")) for known, text in KNOWN_DETAIL):
        return True
    return (rule, None) in KNOWN or any((rule, part) in KNOWN for part in action.split(" + "))

# Fewest trials a minute a run should manage. A conductor spends time inside fault windows and
# restarts, so it is allowed fewer.
MIN_RATE = {"conductor": 1.0, "member": 3.0}


def run_check(script, bank_uid, out, *extra):
    done = subprocess.run([sys.executable, str(HERE / "checks" / script), bank_uid, "--json",
                           str(out), *extra], cwd=str(HERE), capture_output=True, text=True,
                          timeout=1800)
    lines = done.stdout.splitlines()
    last = [line for line in lines if "checks failed" in line]
    files = [line for line in lines if re.match(r"^\d+ (MI )?files", line)]
    return {"exit": done.returncode,
            "result": last[-1] if last else (done.stderr.strip().splitlines() or ["?"])[-1],
            "files": files[-1] if files else None}


SERVICES = ("core", "core-ro", "clearing", "adapter", "public-api", "ops-api", "simulator-api",
            "hot-sauce-bank", "compliance")
ERROR_LINES = 150


def started_at(name):
    """When the loop started this cycle, from its line in fleet.progress."""
    if not PROGRESS.exists():
        return None
    for raw in reversed(PROGRESS.read_text().splitlines()):
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if entry.get("name") == name and entry.get("started"):
            return entry["started"]
    return None


def keep_service_errors(name):
    """Copy each service's ERROR lines and their stack traces for this cycle into one file.

    A wipe recreates the containers and their logs go with them, so a finding from the cycle
    before a wipe had its page JSON and no stack trace. The file is packed with the cycle's logs.
    """
    since = started_at(name)
    out = HERE / "{}.service-errors.log".format(name)
    kept = 0
    with open(out, "w") as handle:
        for service in SERVICES:
            try:
                done = subprocess.run(
                    ["docker", "logs", "--since", since.replace(" ", "T") if since else "2h",
                     "docker-{}-1".format(service)],
                    capture_output=True, text=True, timeout=120)
            except (OSError, subprocess.SubprocessError):
                continue
            taking = 0
            for line in (done.stdout + done.stderr).splitlines():
                if " ERROR " in line or "ERROR\x1b" in line or "[1;31mERROR" in line:
                    handle.write("[{}] {}\n".format(service, line))
                    taking = ERROR_LINES
                    kept += 1
                elif line.startswith("Caused by") or taking and (line[:1] in (" ", "\t")
                                 or "Exception" in line.split(" ", 1)[0]):
                    handle.write("[{}] {}\n".format(service, line))
                    taking -= 1
                else:
                    taking = 0
    return kept


CLEARING_DSN = "postgresql://clearing:password@localhost:5440/clearing"


def exception_copies():
    """How far clearing's repeated EXCEPTION lines (finding 7) grew this cycle, before the cleanup.

    Kept on the summary because a wipe removes the rows, and fleet 10's jump to 4081 repeats could
    not be looked at afterwards. Returns the lines, the distinct bank entries, and the five entries
    with the most copies with their first and last insert.
    """
    sql = ("WITH e AS (SELECT entry_ref, count(*) AS n, min(created_at) AS first, "
           "max(created_at) AS last FROM account_statement_line WHERE status = 'EXCEPTION' "
           "AND created_at > now() - interval '3 hours' GROUP BY entry_ref) "
           "SELECT (SELECT sum(n) FROM e), (SELECT count(*) FROM e), "
           "(SELECT string_agg(entry_ref || ' x' || n || ' ' || first || '..' || last, '; ') "
           "FROM (SELECT * FROM e ORDER BY n DESC LIMIT 5) top)")
    done = subprocess.run(["psql", CLEARING_DSN, "-tA", "-F", "|", "-c", sql],
                          capture_output=True, text=True, timeout=120)
    if done.returncode != 0 or not done.stdout.strip():
        return {"error": done.stderr.strip()[:200]}
    lines, entries, top = (done.stdout.strip().split("|") + ["", "", ""])[:3]
    return {"lines": lines, "entries": entries, "mostCopied": top}


def sanity(name, seconds, runs):
    """What says the harness itself misbehaved in this cycle, as plain sentences."""
    problems = []
    minutes = max(seconds / 60.0, 1.0)
    for log in sorted(HERE.glob("{}-p*.log".format(name))):
        text = log.read_text(errors="replace")
        run = log.stem
        role = "conductor" if run.endswith("-p0") else "member"
        tracebacks = text.count("Traceback (most recent call last)")
        if tracebacks:
            problems.append("{} logged {} tracebacks".format(run, tracebacks))
        if "page:" not in text:
            problems.append("{} did not finish: no final page line".format(run))
        unauthorised = len(re.findall(r"^\s+(?:ok|REJ)\s+\S+\s+401\b", text, re.M))
        if unauthorised:
            problems.append("{} got {} answers of 401".format(run, unauthorised))
        trials = (runs.get(run) or {}).get("trials") or 0
        if trials / minutes < MIN_RATE[role]:
            problems.append("{} made {} trials in {:.0f} minutes, under {} a minute".format(
                run, trials, minutes, MIN_RATE[role]))
        refused = len(re.findall(r"^\s+REJ\s", text, re.M))
        done = len(re.findall(r"^\s+(?:ok|REJ)\s", text, re.M))
        if done and refused / done > 0.6:
            problems.append("{} had {} of {} trials refused".format(run, refused, done))
        if "webhook accounting is off" in text:
            problems.append("{} ran without webhook accounting".format(run))
    capture = HERE / "{}.webhooks.jsonl".format(name)
    if not capture.exists():
        problems.append("no webhook reached the receiver")
    elif '"capture truncated"' in capture.read_text(errors="replace")[-200:]:
        problems.append("the webhook capture reached its size limit")
    for run, counts in runs.items():
        trials = counts.get("trials") or 0
        if trials and (counts.get("unattributed") or 0) > trials * 0.05:
            problems.append("{} counted {} unattributed changes in {} trials".format(
                run, counts["unattributed"], trials))
    return problems


def main():
    name = sys.argv[1]
    FINDINGS.mkdir(exist_ok=True)
    try:
        seconds = int(LENGTH.read_text().strip())
    except (OSError, ValueError):
        seconds = MIN_SECONDS
    cohorts_path = HERE / "{}.cohorts.json".format(name)
    cohorts = json.loads(cohorts_path.read_text()) if cohorts_path.exists() else []
    summary = {"name": name, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "seconds": seconds,
               "runs": {}}
    new_rules = set()
    for page in sorted(HERE.glob("{}-p*.json".format(name))):
        try:
            data = json.loads(page.read_text())
        except ValueError:
            continue
        violations = data.get("violations") or []
        counts = data.get("summary") or {}
        rules = sorted({v.get("rule") for v in violations if v.get("rule")})
        new_rules |= {"{} ({})".format(v.get("rule"), v.get("action")) for v in violations
                      if v.get("rule") and not is_known(v)}
        summary["runs"][page.stem] = {
            "trials": counts.get("trials"),
            "races": counts.get("races"),
            "raceFindings": counts.get("race findings"),
            "unattributed": counts.get("unattributed changes"),
            "violations": len(violations),
            "rules": rules,
        }
    summary["sanity"] = sanity(name, seconds, summary["runs"])
    summary["serviceErrors"] = keep_service_errors(name)
    if not cohorts:
        summary["sanity"].append("no cohorts file, so the standup did not finish")
    else:
        bank = cohorts[0]["bankUid"]
        summary["exceptionCopies"] = exception_copies()
        # The runs are over, so the last repeats they left are cleared before MI_RECON is cut.
        cleared = subprocess.run([sys.executable, str(HERE / "checks" / "ignore_repeats.py")],
                                 cwd=str(HERE), capture_output=True, text=True, timeout=3000)
        summary["repeatsIgnored"] = (cleared.stdout.strip().splitlines() or ["?"])[-1]
        summary["feed"] = run_check("direct_feed.py", bank, FINDINGS / "{}-feed.json".format(name),
                                    "--generate", "400")
        summary["mi"] = run_check("direct_mi.py", bank, FINDINGS / "{}-mi.json".format(name))
        if summary["feed"]["files"] and summary["feed"]["files"].startswith("0 files"):
            summary["sanity"].append("the data feed wrote no files for bank {}".format(bank))
    summary["newRules"] = sorted(new_rules)
    file_failures = [k for k in ("feed", "mi") if summary.get(k, {}).get("exit")]
    quiet = not summary["sanity"] and not new_rules and not file_failures
    summary["quiet"] = quiet
    # Only a potential product issue is worth the rows behind it: a violation of a rule not yet
    # known, or a file check that failed. A harness sanity problem alone takes no dump.
    if new_rules or file_failures:
        import cycle
        kept = cycle.dump_databases(name)
        summary["databases"] = str(kept) if kept else None
    summary["next"] = min(seconds * 2, MAX_SECONDS) if quiet else MIN_SECONDS
    LENGTH.write_text(str(summary["next"]))

    packed = [p for pattern in ("{}-p*.log", "{}-p*.trials.jsonl", "{}.events.jsonl",
                                "{}.subjects.jsonl", "{}.webhooks.jsonl",
                                "{}.service-errors.log")
              for p in HERE.glob(pattern.format(name))]
    if packed:
        with tarfile.open(HERE / "archive" / "{}.tgz".format(name), "w:gz") as tar:
            for path in packed:
                tar.add(path, arcname=path.name)
        for path in packed:
            path.unlink()
    with open(PROGRESS, "a") as handle:
        handle.write(json.dumps(summary) + "\n")
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
