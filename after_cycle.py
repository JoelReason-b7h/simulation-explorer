"""After a fleet cycle: check the bank's files, write one progress line, and pack the logs away.

    python3 after_cycle.py <run name>

The page JSON of each run stays, because it carries every finding with its evidence. The logs,
the trial files and the events file go into archive/<run name>.tgz, so a week of cycles does not
fill the disk, and a finding can still be traced back through the trials that led to it.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tarfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FINDINGS = HERE / "findings"
PROGRESS = HERE / "fleet.progress"


def run_check(script, bank_uid, out, *extra):
    done = subprocess.run([sys.executable, str(HERE / "checks" / script), bank_uid, "--json",
                           str(out), *extra], cwd=str(HERE), capture_output=True, text=True,
                          timeout=1800)
    last = [line for line in done.stdout.splitlines() if "checks failed" in line]
    return done.returncode, (last[-1] if last else (done.stderr.strip().splitlines() or ["?"])[-1])


def main():
    name = sys.argv[1]
    FINDINGS.mkdir(exist_ok=True)
    cohorts_path = HERE / "{}.cohorts.json".format(name)
    cohorts = json.loads(cohorts_path.read_text()) if cohorts_path.exists() else []
    summary = {"name": name, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "runs": {}}
    for page in sorted(HERE.glob("{}-p*.json".format(name))):
        try:
            data = json.loads(page.read_text())
        except ValueError:
            continue
        violations = data.get("violations") or []
        counts = data.get("summary") or {}
        summary["runs"][page.stem] = {
            "trials": counts.get("trials"),
            "races": counts.get("races"),
            "raceFindings": counts.get("race findings"),
            "unattributed": counts.get("unattributed changes"),
            "violations": len(violations),
            "rules": sorted({v.get("rule") for v in violations if v.get("rule")}),
        }
    if cohorts:
        bank = cohorts[0]["bankUid"]
        feed = run_check("direct_feed.py", bank, FINDINGS / "{}-feed.json".format(name),
                         "--generate", "6")
        mi = run_check("direct_mi.py", bank, FINDINGS / "{}-mi.json".format(name))
        summary["feed"] = {"exit": feed[0], "result": feed[1]}
        summary["mi"] = {"exit": mi[0], "result": mi[1]}
    packed = [p for pattern in ("{}-p*.log", "{}-p*.trials.jsonl", "{}.events.jsonl")
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
