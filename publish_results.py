"""Push a digest of one cycle to the repo's `results` branch, for the summary routine to read.

    python3 publish_results.py <cycle>

Writes cycles/<cycle>.json and one line of index.jsonl in a separate clone at SIM_RESULTS_DIR
(default ../results), commits, and pushes with the deploy key at SIM_RESULTS_KEY. The digest keeps
each run's violations with their lead-up, and the parts of the run that explain them; the database
dumps and the full feed findings stay on the box. A failure is printed and never raised, so the
loop goes on; an unpushed commit goes up with the next cycle's push.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
RESULTS = Path(os.environ.get("SIM_RESULTS_DIR", HERE.parent / "results"))
KEY = Path(os.environ.get("SIM_RESULTS_KEY", Path.home() / ".ssh/simulation_explorer_results"))
REMOTE = os.environ.get("SIM_RESULTS_REMOTE", "git@github.com:JoelReason-b7h/simulation-explorer.git")
BRANCH = "results"
RUN_KEYS = ("summary", "trialTotals", "violations", "why_counts", "reach", "faults", "journeys",
            "webhooks", "operatorDecisions", "longLived", "interestOracle", "clock")
FEED_SAMPLES = 3


def git(*args, check=True):
    env = dict(os.environ, GIT_SSH_COMMAND="ssh -i {} -o IdentitiesOnly=yes "
               "-o StrictHostKeyChecking=accept-new".format(KEY))
    return subprocess.run(["git", "-C", str(RESULTS), *args], env=env, check=check,
                          capture_output=True, text=True, timeout=120)


def ensure_clone():
    if (RESULTS / ".git").exists():
        return
    RESULTS.mkdir(parents=True, exist_ok=True)
    git("init", "-q")
    git("remote", "add", "origin", REMOTE)
    git("config", "user.name", "simulation-explorer box")
    git("config", "user.email", "simulation-explorer-box@users.noreply.github.com")
    if git("fetch", "-q", "origin", BRANCH, check=False).returncode == 0:
        git("checkout", "-q", "-b", BRANCH, "FETCH_HEAD")
    else:
        git("checkout", "-q", "--orphan", BRANCH)


def progress_line(name):
    for line in reversed((HERE / "fleet.progress").read_text().splitlines()):
        entry = json.loads(line)
        if entry.get("name") == name and "runs" in entry:
            return entry
    return None


def feed_digest(name):
    path = HERE / "findings" / "{}-feed.json".format(name)
    if not path.exists():
        return None
    findings = json.loads(path.read_text()).get("findings", [])
    counts = Counter(f.get("rule") for f in findings)
    samples = {}
    for f in findings:
        rule = f.get("rule")
        if len(samples.setdefault(rule, [])) < FEED_SAMPLES:
            samples[rule].append(f)
    return {"failed": len(findings), "byRule": dict(counts.most_common()), "samples": samples}


def digest(name):
    runs = {}
    for path in sorted(HERE.glob("{}-p*.json".format(name))):
        run = json.loads(path.read_text())
        runs[path.stem] = {key: run.get(key) for key in RUN_KEYS}
    mi = HERE / "findings" / "{}-mi.json".format(name)
    clock = next((r["clock"] for r in runs.values() if r.get("clock")), None)
    return {"name": name, "progress": progress_line(name), "runs": runs, "clock": clock,
            "mi": json.loads(mi.read_text()) if mi.exists() else None,
            "feed": feed_digest(name)}


def publish(name):
    if not KEY.exists():
        print("results not published: no deploy key at {}".format(KEY))
        return
    ensure_clone()
    data = digest(name)
    (RESULTS / "cycles").mkdir(exist_ok=True)
    (RESULTS / "cycles" / "{}.json".format(name)).write_text(json.dumps(data, indent=1) + "\n")
    p = data["progress"] or {}
    runs = (p.get("runs") or {}).values()
    line = {"name": name, "at": p.get("at"), "quiet": p.get("quiet"),
            "trials": sum(r.get("trials", 0) for r in runs),
            "violations": sum(r.get("violations", 0) for r in runs),
            "serviceErrors": p.get("serviceErrors"), "newRules": p.get("newRules", [])}
    with open(RESULTS / "index.jsonl", "a") as index:
        index.write(json.dumps(line) + "\n")
    git("add", "cycles", "index.jsonl")
    git("commit", "-q", "-m", "{}: {} trials, {} violations{}".format(
        name, line["trials"], line["violations"],
        ", new rules: " + "; ".join(line["newRules"]) if line["newRules"] else ""))
    pushed = git("push", "-q", "origin", BRANCH, check=False)
    print("results for {} {}".format(
        name, "pushed" if pushed.returncode == 0 else "committed, push failed: " + pushed.stderr.strip()))


if __name__ == "__main__":
    try:
        publish(sys.argv[1])
    except Exception as error:  # noqa: BLE001 - publishing must never stop the loop
        print("results not published: {}".format(error))
