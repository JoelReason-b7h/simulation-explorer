"""The run's trials: the recent ones in memory, every one on disk, and the totals the page shows.

A run of days makes hundreds of thousands of trials. Holding each one and publishing the whole
list after every trial makes each trial slower than the one before it, so the log keeps a bounded
window for the page, appends every trial to a JSON Lines file, and counts as it goes.
"""

import json
from collections import deque

RECENT = 500
RACE_FINDINGS = 200
STATES_PER_RULE = 20


class TrialLog:

    def __init__(self, path):
        self.path = path
        self.recent = deque(maxlen=RECENT)
        self.count = 0
        self.by_action = {}
        self.by_pair = {}
        self.race_findings = deque(maxlen=RACE_FINDINGS)
        self.refusals = {}
        open(path, "w").close()

    def __len__(self):
        return self.count

    def __getitem__(self, index):
        return list(self.recent)[index]

    def append(self, trial):
        self.count += 1
        self.recent.append(trial)
        with open(self.path, "a") as handle:
            handle.write(json.dumps(trial) + "\n")
        self._tally(self.by_action.setdefault(trial["action"], {"n": 0, "ok": 0, "rej": 0}), trial)
        pair = self.by_pair.setdefault(
            trial["action"] + " :: " + " :: ".join(map(str, trial["key"])),
            {"action": trial["action"], "key": trial["key"], "n": 0, "ok": 0, "rej": 0})
        self._tally(pair, trial)
        if trial.get("race") and trial.get("message"):
            self.race_findings.append(trial)
        elif not trial.get("race") and not trial["ok"] and trial.get("message"):
            rule = self.refusals.setdefault(
                trial["action"] + " :: " + trial["message"],
                {"action": trial["action"], "message": trial["message"], "count": 0, "states": []})
            rule["count"] += 1
            if trial["key"] not in rule["states"] and len(rule["states"]) < STATES_PER_RULE:
                rule["states"].append(trial["key"])

    @staticmethod
    def _tally(row, trial):
        row["n"] += 1
        row["ok" if trial["ok"] else "rej"] += 1

    def totals(self):
        return {
            "byAction": self.by_action,
            "byPair": list(self.by_pair.values()),
            "raceFindings": list(self.race_findings),
            "refusals": list(self.refusals.values()),
        }
