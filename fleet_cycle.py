"""One fleet cycle: several pooled, unverified Direct platforms on one bank, explored at once.

    python3 fleet_cycle.py <seconds> <run name> [platforms]

The first platform's run is the conductor: it alone injects faults, restarts services and runs
the whole-table sweeps. The others are members, which act only on the domain. Every run writes
the work it starts on the shared stack to one events file, so each one knows what the others had
in flight when it reports a change it did not make.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cycle  # noqa: E402
from explorer import config, local_auth, longlived, webhooks  # noqa: E402

HERE = cycle.HERE
FIELDS = ("bankUid", "platformUid", "productUid", "termProductUid", "shortTermProductUid",
          "noticeProductUid", "clientId")


def stand_up(reuse=None, suffix=""):
    env = cycle.child_env()
    env["SIM_PLATFORM_SUFFIX"] = suffix
    if reuse:
        env["SIM_REUSE_COHORT"] = json.dumps(reuse)
    done = subprocess.run([sys.executable, "standup_cohort.py"], cwd=str(HERE), env=env,
                          capture_output=True, text=True, timeout=2400)
    sys.stdout.write("".join("    " + line + "\n" for line in done.stdout.splitlines()
                             if line.startswith(("  REJ", "  ok  open", "  ok  give",
                                                 "      reusing", "  ok  subscribe",
                                                 "  SKIP")) or "Uid" in line))
    if done.returncode != 0:
        print(done.stdout[-2000:])
        print(done.stderr[-2000:])
        raise SystemExit("platform {} did not stand up".format(suffix or 0))
    found = {}
    for field in FIELDS:
        match = re.search(r"^\s+{}\s+(\S+)".format(field), done.stdout, re.M)
        if match:
            found[field] = match.group(1)
    missing = [f for f in FIELDS if f not in found]
    if missing:
        raise SystemExit("the standup printed no {}".format(", ".join(missing)))
    return found


def launch(index, cohort, iban, run_name, seconds, events, capture):
    env = cycle.child_env()
    env.update({
        "PLATFORM_UID": cohort["platformUid"],
        "BANK_UID": cohort["bankUid"],
        "PLATFORM_VIRTUAL_IBAN": iban,
        "SIM_SECONDS": str(seconds),
        "SIM_RUN_NAME": run_name,
        "auth_client_id": cohort["clientId"],
        "auth_client_secret": "local",
        "SIM_FLEET_EVENTS": str(events),
        "SIM_WEBHOOK_CAPTURE": str(capture),
        "SIM_FLEET_ROLE": "conductor" if index == 0 else "member",
    })
    handle = open(HERE / "{}.log".format(run_name), "w")
    return subprocess.Popen([sys.executable, "-u", "explore.py"], cwd=str(HERE), env=env,
                            stdout=handle, stderr=subprocess.STDOUT), handle


def long_lived_cohorts(count):
    """(cohorts, virtual IBANs) from the saved long-lived cohort, or (None, {}) to stand up.

    On by default; SIM_LONG_LIVED_BANK=0 turns it off, and SIM_SAVED_COHORT names the file. The
    saved cohort is used only once every platform in it still answers through the API.
    """
    if not longlived.reuse_enabled():
        return None, {}
    saved = longlived._read(longlived.saved_cohort_path())
    reason = longlived.unusable(saved, count, config.load("local")) if saved else "none saved"
    if reason:
        print("  the saved long-lived cohort is not used: {}".format(reason))
        return None, {}
    cohorts = saved["cohorts"][:count]
    cycle.note_standup(True)
    print("  reusing the long-lived bank {} and its {} platforms, first stood up by {}".format(
        cohorts[0]["bankUid"], count, saved.get("createdBy")))
    return cohorts, dict(saved.get("ibans") or {})


def main():
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    name = sys.argv[2] if len(sys.argv) > 2 else "fleet"
    count = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    cycle.stop_any_running_cycle()
    patched = False
    if config.load("local").get("local_auth"):
        patched = cycle.stack_patch.patch(cycle.REPO)
        local_auth.serve()
    # Started before the standup, which subscribes each platform to it. Every delivery of the
    # cycle, for any platform, lands in this one file.
    capture = webhooks.capture_path(HERE, name)
    webhooks.ensure_serving(capture)
    wiped = False
    # The stack is wiped only when a patch changed what it reads at start, or when asked with
    # SIM_FORCE_WIPE=1. A wipe on a timer destroyed the rows behind fleet 10's findings before
    # they were traced, and a stack that carries on also ages its banks across cycles.
    healed = cycle.heal_stack()
    if healed is None or cycle.standup_keeps_failing():
        print("  the stack is broken, so it is relaunched after a dump")
    if os.environ.get("SKIP_RESTART") != "1" and (
            patched or os.environ.get("SIM_FORCE_WIPE") == "1" or healed is None
            or cycle.standup_keeps_failing()):
        cycle.restart_stack()
        wiped = True
    else:
        print("  keeping the stack that is already up")
    cycle.note_cycle(wiped)

    cycle.top_up_preloaded_accounts()
    cohorts, ibans = long_lived_cohorts(count)
    if cohorts is None:
        print("  standing up {} platforms on one bank".format(count))
        try:
            cohorts = [stand_up()]
        except SystemExit:
            cycle.note_standup(False)
            raise
        cycle.note_standup(True)
        shared = {k: cohorts[0][k] for k in FIELDS if k not in ("platformUid", "clientId")}
        for index in range(1, count):
            cohorts.append(stand_up(reuse=shared, suffix=" {}".format(index)))

    events = HERE / "{}.events.jsonl".format(name)
    events.write_text("")
    if longlived.reuse_enabled():
        # Read by every run of the cycle: the clock, the population and the webhook accounting.
        os.environ.update({"SIM_LONG_LIVED": "1", "SIM_CYCLE_STARTED": str(time.time()),
                           "SIM_SAVED_COHORT": str(longlived.saved_cohort_path())})
    runs = []
    for index, cohort in enumerate(cohorts):
        iban = ibans.get(cohort["platformUid"]) or cycle.virtual_iban(cohort["platformUid"])
        ibans[cohort["platformUid"]] = iban
        run_name = "{}-p{}".format(name, index)
        print("  {} platform {} client {} virtual account {}".format(
            run_name, cohort["platformUid"], cohort["clientId"], iban))
        runs.append(launch(index, cohort, iban, run_name, seconds, events, capture))
    (HERE / "{}.cohorts.json".format(name)).write_text(json.dumps(cohorts, indent=2))
    if longlived.reuse_enabled():
        longlived.save_cohort(cohorts, ibans, name)

    deadline = time.time() + seconds + 900
    codes = []
    for process, handle in runs:
        try:
            codes.append(process.wait(timeout=max(1, deadline - time.time())))
        except subprocess.TimeoutExpired:
            process.kill()
            codes.append("killed")
        handle.close()
    print("  exit codes {}".format(codes))
    return 0 if all(c == 0 for c in codes) else 1


if __name__ == "__main__":
    sys.exit(main())
