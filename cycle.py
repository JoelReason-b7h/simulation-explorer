"""One full cycle: wipe the stack, stand a cohort up, prove a deposit lands, then explore.

The point of wiping first is that a run inherits whatever the last run left behind, and a pool of
preloaded accounts that some earlier run drained looks exactly like a service that refuses to open
accounts. Starting from an empty stack makes each cycle's findings its own.

    python3 cycle.py [seconds]
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import stack_patch  # noqa: E402
from explorer import config, local_auth, world  # noqa: E402
from explorer.client import BearerClient  # noqa: E402
# The checkout whose compose files and launch scripts bring the stack up. The images are built from
# origin/main, and main's compose and env files differ from this branch's old base, so the stack is
# launched from a worktree at main that carries the toxiproxy changes the harness needs.
# sim-main, always: the images are built there and only its docker files carry the local-auth
# settings. Defaulting to this worktree launched a stack whose adapter could not start.
REPO = Path(os.environ.get("SIM_STACK_REPO",
                           "/Users/joelreason/IdeaProjects/exchange.worktrees/sim-main"))


# The ops credentials live in performance-testing/data/auth, which is gitignored, so this worktree
# has no copy and every ops call fails before the first action. The main checkout holds them.
MAIN_AUTH_DIR = Path.home() / "IdeaProjects/exchange/tools/performance-testing/data/auth"

# docker-compose.yml passes the caller's AWS settings straight through to every service, and a
# service with no region refuses to start: the SSM client builder throws "Unable to load region"
# before Micronaut finishes its context. The values are the dummy pair LocalStack accepts, the
# same ones scripts/aws_local exports.
AWS_LOCAL = {
    "AWS_REGION": "eu-west-2",
    "AWS_DEFAULT_REGION": "eu-west-2",
    "AWS_ACCESS_KEY_ID": "secret",
    "AWS_SECRET_ACCESS_KEY": "secret",
    # Every session builds into e2e/*:latest, so a relaunch on that prefix runs whichever session
    # built last. tag_images.sh copies this worktree's build under its own prefix, and
    # local-common.sh keeps a caller's IMAGE_HOST.
    "IMAGE_HOST": "simharness",
}


def shell(command, timeout, cwd=REPO):
    started = time.time()
    env = dict(os.environ)
    env.update(AWS_LOCAL)
    done = subprocess.run(command, shell=True, cwd=str(cwd), capture_output=True,
                          text=True, timeout=timeout, env=env)
    print("    {} ({:.0f}s, exit {})".format(command, time.time() - started, done.returncode))
    return done


DOWN_SECONDS = 240


def force_down():
    """Remove the compose containers outright, for a teardown that has stopped making progress."""
    listed = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "name=docker-"],
        capture_output=True, text=True, timeout=60)
    ids = [i for i in listed.stdout.split() if i]
    if ids:
        subprocess.run(["docker", "rm", "-f"] + ids, capture_output=True, text=True, timeout=180)
    print("    forced {} containers away after the teardown stalled".format(len(ids)))


DATABASES = ("core", "clearing", "hsb", "compliance")


def dump_databases(label):
    """Keep every local database as it stands, in archive/<label>-db/, before anything replaces it.

    A wipe recreates the databases, and fleet 10's two dead-lettered PaymentSettled messages could
    not be traced afterwards because their payment dues went with it. A dump of all four is about
    twelve megabytes. Returns the directory, or None when no dump could be taken.
    """
    target = HERE / "archive" / "{}-db".format(label)
    target.mkdir(parents=True, exist_ok=True)
    taken = []
    for db in DATABASES:
        with open(target / "{}.dump".format(db), "wb") as handle:
            done = subprocess.run(["docker", "exec", "docker-postgres-1", "pg_dump", "-U", db,
                                   "-Fc", db], stdout=handle, stderr=subprocess.PIPE, timeout=600)
        if done.returncode == 0:
            taken.append(db)
    print("    kept the {} databases in {}".format(", ".join(taken) or "no", target))
    return target if taken else None


def top_up_preloaded_accounts():
    settings = config.load("local")
    ops = BearerClient(settings["ops_base_url"], world.ops_token(settings))
    try:
        print("  preloaded account top-up answered {}".format(world.top_up_preloaded_accounts(ops)))
    finally:
        ops.close()


HEALTH_SECONDS = 600
STANDUP_FAILURES = HERE / ".standup-failures"


def stack_containers():
    done = subprocess.run(["docker", "ps", "-a", "--filter", "label=com.docker.compose.project=docker",
                           "--format", "{{.Names}}\t{{.State}}"],
                          capture_output=True, text=True, timeout=60)
    return dict(line.split("\t", 1) for line in done.stdout.splitlines() if "\t" in line)


def health(name):
    done = subprocess.run(["docker", "inspect", name, "--format",
                           "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}"],
                          capture_output=True, text=True, timeout=30)
    return done.stdout.strip()


MEMORY_BOUND = ("docker-hot-sauce-bank-1",)
MEMORY_RESTART_SHARE = 0.85


def memory_share(name):
    """The container's memory use as a share of its limit, or None when docker cannot say."""
    done = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.MemPerc}}", name],
                          capture_output=True, text=True, timeout=60)
    try:
        return float(done.stdout.strip().rstrip("%")) / 100
    except ValueError:
        return None


def heal_stack():
    """Start every stack container that has stopped, and wait until each is healthy again.

    LocalStack was killed for memory after eleven hours, and the loop then ran 160 cycles that
    each failed at standup, because nothing looked at the containers. Starting the same container
    keeps its environment, and LocalStack's ready.d scripts create its queues and buckets again.
    Returns the names it started, or None when a container would not come back.
    """
    stopped = [name for name, state in stack_containers().items() if state != "running"]
    # The bank simulator slows to 40 seconds a credit when it runs near its memory limit, and it
    # holds nothing a restart loses, so restart it between cycles once it passes the mark.
    for name in MEMORY_BOUND:
        used = memory_share(name)
        if name not in stopped and used is not None and used >= MEMORY_RESTART_SHARE:
            print("  {} is using {:.0%} of its memory limit, so it is restarted".format(name, used))
            subprocess.run(["docker", "restart", name], capture_output=True, text=True, timeout=180)
            stopped.append(name)
    for name in stopped:
        print("  {} had stopped, so it is being started again".format(name))
        subprocess.run(["docker", "start", name], capture_output=True, text=True, timeout=120)
    deadline = time.time() + HEALTH_SECONDS
    waiting = list(stopped)
    while waiting and time.time() < deadline:
        waiting = [name for name in waiting if health(name) not in ("healthy", "running")]
        if waiting:
            time.sleep(10)
    if waiting:
        print("  {} did not come back healthy".format(", ".join(waiting)))
        return None
    return stopped


def note_standup(ok):
    """Count the cycles in a row whose standup failed. Two in a row means the stack is broken."""
    failures = 0 if ok else int(STANDUP_FAILURES.read_text() or 0) + 1 if STANDUP_FAILURES.exists() else 1
    STANDUP_FAILURES.write_text(str(failures))
    return failures


def standup_keeps_failing():
    return STANDUP_FAILURES.exists() and int(STANDUP_FAILURES.read_text() or 0) >= 2


def restart_stack():
    # Every wipe dumps first. The rows behind findings 12 and the fleet 3 and 4 500s went with
    # wipes that took no dump, and a finding is often traced a cycle or more after it appears.
    dump_databases("pre-wipe-{}".format(time.strftime("%Y%m%dT%H%M%S")))
    print("  wiping and relaunching the stack")
    try:
        down = shell("./local-down.sh", timeout=DOWN_SECONDS)
    except subprocess.TimeoutExpired:
        # A teardown that runs for a quarter of an hour stalls the whole loop and says nothing
        # about the system. One took 938 seconds on a machine carrying load average 16 after
        # forty-odd full wipes, so the run stops waiting and removes the containers itself.
        print("    the teardown passed {}s, so the containers are being removed".format(
            DOWN_SECONDS))
        force_down()
        down = None
    if down is not None and down.returncode != 0:
        print(down.stderr[-2000:])
        raise SystemExit("could not bring the stack down")
    import stack_check
    wrong = stack_check.checkout_problems(REPO)
    if wrong:
        raise SystemExit("refusing to launch from the wrong checkout: {}".format("; ".join(wrong)))
    up = shell("./scripts/launch_stack", timeout=1200)
    if up.returncode != 0:
        print(up.stdout[-3000:])
        print(up.stderr[-3000:])
        raise SystemExit("the stack did not come up")


def child_env():
    env = dict(os.environ)
    if not env.get("SIM_AUTH_DIR") and MAIN_AUTH_DIR.exists():
        env["SIM_AUTH_DIR"] = str(MAIN_AUTH_DIR)
    return env


def stand_up():
    print("  standing up the cohort")
    done = subprocess.run([sys.executable, "standup_cohort.py"], cwd=str(HERE),
                          capture_output=True, text=True, timeout=2400, env=child_env())
    sys.stdout.write("".join("    " + line + "\n" for line in done.stdout.splitlines()))
    if done.returncode != 0:
        print(done.stderr[-2000:])
        raise SystemExit("the cohort did not stand up")
    found = {}
    for field in ("platformUid", "bankUid", "productUid"):
        match = re.search(r"\b{}\s+(\S+)".format(field), done.stdout)
        if match:
            found[field] = match.group(1)
    if "platformUid" not in found:
        raise SystemExit("the standup printed no platformUid")
    return found


CORE_DSN = "postgresql://core:password@localhost:5432/core"


def virtual_iban(platform_uid):
    """The platform's own account at the bank, which every funding payment is paid into.

    Read from core's entity_internal_account rather than over HTTP, because the ops endpoint
    returns the account's status and currency and no identifier at all.

    Creating the platform only asks for the account. Clearing reserves a preloaded subledger off
    an SQS message and publishes InternalAccountCreated, and core writes the identifier when that
    event arrives, so the row is filled a second or two after the standup returns.
    """
    query = (
        "select account_identifier->>'value' from entity_internal_account "
        "where entity_uid = '{}' and currency = 'GBP' "
        "and account_identifier is not null limit 1".format(platform_uid))
    # Five minutes, not three. Under sustained load a psql call alone took minutes, so the poll
    # gave up while core wrote the identifier a moment later: the run reported the environment as
    # broken when it was only slow.
    deadline = time.time() + 300
    last = "the query never ran"
    while time.time() < deadline:
        try:
            done = subprocess.run(["psql", CORE_DSN, "-tAc", query],
                                  capture_output=True, text=True, timeout=60)
        except subprocess.SubprocessError as slow:
            last = "psql did not answer: {}".format(type(slow).__name__)
            continue
        found = done.stdout.strip()
        if found:
            return found
        last = "psql exit {}, stdout {!r}, stderr {!r}".format(
            done.returncode, done.stdout.strip()[:120], done.stderr.strip()[:200])
        time.sleep(3)
    raise SystemExit(
        "core never wrote an identifier onto the platform's internal account after 300s. "
        "The last attempt: {}".format(last))


def explore(seconds, cohort, iban, run_name):
    env = child_env()
    env["PLATFORM_UID"] = cohort["platformUid"]
    env["BANK_UID"] = cohort.get("bankUid", "")
    env["PLATFORM_VIRTUAL_IBAN"] = iban
    env["SIM_SECONDS"] = str(seconds)
    env["SIM_RUN_NAME"] = run_name
    log = HERE / "{}.log".format(run_name)
    print("  exploring for {}s, log at {}".format(seconds, log))
    with open(log, "w") as handle:
        done = subprocess.run([sys.executable, "-u", "explore.py"], cwd=str(HERE), env=env,
                              stdout=handle, stderr=subprocess.STDOUT,
                              timeout=seconds + 900)
    return done.returncode


def stop_any_running_cycle():
    """Stop a run still going from a previous cycle before starting another.

    Two runs against one stack write the same page and clobber each other's temporary file, and
    they also drive the same cohort, so neither one's findings mean anything.
    """
    # Only explore.py: killing cycle.py would kill this process too.
    subprocess.run(["pkill", "-f", "explore.py"], capture_output=True, text=True)
    time.sleep(2)


WIPE_EVERY = 3
COUNTER = HERE / ".cycles-since-wipe"


def due_a_wipe():
    """True when the stack has served enough cycles to be worth wiping again.

    A wipe costs three to fifteen minutes and the docker virtual machine slows as the wipes pile
    up: one teardown ran for 938 seconds after forty-odd of them. Each cycle stands up its own
    bank and platform, so a cohort on a used stack is still independent of every earlier one, and
    wiping every third cycle keeps the state fresh without spending the run on docker.
    """
    try:
        served = int(COUNTER.read_text().strip())
    except (OSError, ValueError):
        served = WIPE_EVERY
    return served >= WIPE_EVERY


def note_cycle(wiped):
    COUNTER.write_text("0" if wiped else str(
        (lambda n: n + 1)(int(COUNTER.read_text().strip()) if COUNTER.exists() else 0)))


def main():
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    run_name = sys.argv[2] if len(sys.argv) > 2 else "run"
    stop_any_running_cycle()
    patched = False
    if config.load("local").get("local_auth"):
        patched = stack_patch.patch(REPO)
        local_auth.serve()
    wiped = False
    if os.environ.get("SKIP_RESTART") != "1" and (due_a_wipe() or patched):
        restart_stack()
        wiped = True
    else:
        print("  keeping the stack that is already up, and standing a new cohort on it")
    note_cycle(wiped)
    cohort = stand_up()
    iban = virtual_iban(cohort["platformUid"])
    print("  platform {} · bank {} · virtual account {}".format(
        cohort["platformUid"], cohort.get("bankUid"), iban))
    Path(HERE / "cohort.env").write_text(
        "PLATFORM_UID={}\nBANK_UID={}\nPLATFORM_VIRTUAL_IBAN={}\n".format(
            cohort["platformUid"], cohort.get("bankUid", ""), iban))
    return explore(seconds, cohort, iban, run_name)


if __name__ == "__main__":
    sys.exit(main())
