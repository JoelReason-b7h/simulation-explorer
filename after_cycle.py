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

# Rules whose findings are already in FINDINGS.md or are measurements, not defects. A violation
# of any other rule makes the cycle interesting.
KNOWN_RULES = {
    "a terminal status is not left",
    "a closed account holds no money",
    "a read answers promptly",
}

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
        new_rules |= set(rules) - KNOWN_RULES
        summary["runs"][page.stem] = {
            "trials": counts.get("trials"),
            "races": counts.get("races"),
            "raceFindings": counts.get("race findings"),
            "unattributed": counts.get("unattributed changes"),
            "violations": len(violations),
            "rules": rules,
        }
    summary["sanity"] = sanity(name, seconds, summary["runs"])
    if not cohorts:
        summary["sanity"].append("no cohorts file, so the standup did not finish")
    else:
        bank = cohorts[0]["bankUid"]
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
    summary["next"] = min(seconds * 2, MAX_SECONDS) if quiet else MIN_SECONDS
    LENGTH.write_text(str(summary["next"]))

    packed = [p for pattern in ("{}-p*.log", "{}-p*.trials.jsonl", "{}.events.jsonl",
                                "{}.subjects.jsonl")
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
