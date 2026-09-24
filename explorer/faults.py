"""Faults the run injects into the stack itself, rather than into the domain.

Everything else the harness injects is a domain fault: a compliance stop, a payment that cannot
match, two calls at one barrier. These break the machinery underneath — the wire between two
services, and a service's own process — so the run can ask what a distributed system is actually
asked in production: the call may or may not have landed, and does the system end up consistent
either way.

Three boundaries carry a proxy, created by `scripts/launch_stack` before any service dials through
them:

- `core-to-clearing` on port 20001, which every payment due and settlement crosses.
- `clearing-to-bank` on port 20002, which every statement poll and payment submission crosses.
- `harness-bank` on port 20000, the run's own credit into the bank, created by the run itself.

With no toxics configured each listener passes traffic straight through, so a stack nobody is
injecting into behaves exactly as it did before.
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import sys
import time

import httpx

ADMIN = "http://localhost:8474"

# The run's own credit into the bank. Created here rather than by launch_stack, because only the
# harness routes through it.
OWN_BANK_PROXY = "harness-bank"
OWN_BANK_LISTEN = "0.0.0.0:20000"
OWN_BANK_UPSTREAM = "hot-sauce-bank:10002"
THROUGH_PROXY = "http://localhost:20000"

# The boundaries between services, created by launch_stack.
CORE_TO_CLEARING = "core-to-clearing"
CLEARING_TO_BANK = "clearing-to-bank"

# Every service the run may stop and start again. Named as compose names them.
RESTARTABLE = {
    "clearing": "docker-clearing-1",
    "core": "docker-core-1",
    "bank": "docker-hot-sauce-bank-1",
}

# Where each service says it is healthy again. Waiting on the Direct API instead was not enough:
# public-api answers while core is still down and then fails every call it forwards, which put
# 1866 "Connection refused: core:4000" behind findings the run recorded as though the stack were
# healthy.
HEALTH = {
    "clearing": "http://localhost:8050/health",
    "core": "http://localhost:4000/health",
    "bank": "http://localhost:10002/health",
}


def wait_until_healthy(which, seconds=180):
    """Block until the named service reports itself up again."""
    url = HEALTH.get(which)
    if not url:
        return False
    deadline = time.time() + seconds
    with httpx.Client(timeout=5.0) as http:
        while time.time() < deadline:
            try:
                answer = http.get(url)
                if answer.status_code == 200 and "UP" in answer.text:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(2)
    return False


class Toxiproxy:
    """The toxiproxy admin API, or a stand-in that reports that it is not there."""

    def __init__(self, admin=ADMIN, timeout=5.0):
        self.admin = admin
        self.http = httpx.Client(timeout=timeout)
        self.ready = False
        self.known = set()

    def start(self):
        """Create the run's own bank proxy and find which service proxies already exist."""
        try:
            self.http.delete("{}/proxies/{}".format(self.admin, OWN_BANK_PROXY))
            made = self.http.post("{}/proxies".format(self.admin), json={
                "name": OWN_BANK_PROXY, "listen": OWN_BANK_LISTEN,
                "upstream": OWN_BANK_UPSTREAM, "enabled": True})
            listed = self.http.get("{}/proxies".format(self.admin))
        except httpx.HTTPError:
            return False
        self.ready = made.status_code in (200, 201)
        if listed.status_code == 200:
            try:
                self.known = set(listed.json())
            except ValueError:
                self.known = set()
        return self.ready

    def proxies(self):
        """Every boundary a fault can be injected on right now."""
        return sorted(self.known | ({OWN_BANK_PROXY} if self.ready else set()))

    def add(self, proxy, name, kind, attributes):
        if not self.ready or proxy not in self.proxies():
            return None
        try:
            return self.http.post("{}/proxies/{}/toxics".format(self.admin, proxy), json={
                "name": name, "type": kind, "stream": "downstream", "attributes": attributes})
        except httpx.HTTPError:
            return None

    def clear(self, proxy=None):
        """Remove every toxic, from one boundary or from all of them."""
        if not self.ready:
            return None
        for name in ([proxy] if proxy else self.proxies()):
            try:
                listed = self.http.get("{}/proxies/{}/toxics".format(self.admin, name))
                for toxic in listed.json() if listed.status_code == 200 else []:
                    self.http.delete("{}/proxies/{}/toxics/{}".format(
                        self.admin, name, toxic["name"]))
            except (httpx.HTTPError, ValueError):
                continue
        return True

    def close(self):
        self.clear()
        try:
            self.http.delete("{}/proxies/{}".format(self.admin, OWN_BANK_PROXY))
        except httpx.HTTPError:
            pass
        self.http.close()


# SQS on LocalStack. The queues clearing consumes carry the events that move money between core
# and clearing, so redelivering one is the at-least-once case every consumer is written for.
SQS = "http://localhost:4566"
QUEUE = SQS + "/queue/eu-west-2/000000000000/{}"
DUPLICATING_QUEUES = {
    "account requests": "eventbridge-iar-clearing",
    "accounts created": "eventbridge-icac-clearing",
    "payment events": "eventbridge-pe-clearing",
}
AWS_ENV = {
    "AWS_ACCESS_KEY_ID": "secret",
    "AWS_SECRET_ACCESS_KEY": "secret",
    "AWS_DEFAULT_REGION": "eu-west-2",
}


def _aws(*args, timeout=30):
    env = dict(os.environ)
    env.update(AWS_ENV)
    try:
        return subprocess.run(["aws", "--endpoint-url", SQS] + list(args),
                              capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError):
        return None


# What each queue's visibility timeout was before the run changed it, and which services the run
# stopped. A run that is killed mid-fault leaves both behind, so the cleanup below reads these.
_CHANGED_TIMEOUTS = {}
_STOPPED_SERVICES = set()


def visibility_timeout(queue):
    """What the queue hides a received message for right now, in seconds, or None."""
    done = _aws("sqs", "get-queue-attributes", "--queue-url", QUEUE.format(queue),
                "--attribute-names", "VisibilityTimeout")
    if not done or done.returncode != 0:
        return None
    try:
        return int(json.loads(done.stdout)["Attributes"]["VisibilityTimeout"])
    except (ValueError, KeyError, TypeError):
        return None


def set_visibility_timeout(queue, seconds):
    """Change how long a received message stays hidden from other consumers.

    Setting it to zero makes every message visible again the instant it is received, so the
    consumer sees it more than once. That is genuine duplicate delivery, produced without the
    harness reading or writing a single message: stealing one from the queue to send it again
    would take it away from the consumer it was meant for.

    The value the queue held is kept, because a run that dies while a queue is at zero leaves its
    consumer polling in a tight loop. That happened once: LocalStack reached 229% CPU and the
    machine reached a load average of 92, and nothing put the queue back until a person did.
    """
    if queue not in _CHANGED_TIMEOUTS:
        held = visibility_timeout(queue)
        if held is not None and held != seconds:
            _CHANGED_TIMEOUTS[queue] = held
    done = _aws("sqs", "set-queue-attributes", "--queue-url", QUEUE.format(queue),
                "--attributes", "VisibilityTimeout={}".format(seconds))
    if done and done.returncode == 0 and _CHANGED_TIMEOUTS.get(queue) == seconds:
        del _CHANGED_TIMEOUTS[queue]
    return bool(done and done.returncode == 0)


def duplicate_one_message(queue):
    """Put a second copy of a waiting message on the queue, without taking the first away.

    Setting the visibility timeout to zero does not produce a duplicate: the consumer deletes the
    message within milliseconds of receiving it, so no second poll ever sees it, and every repeat
    the run observed that way turned out to be the ordinary thirty-second retry after a failed
    insert. Reading with a zero visibility timeout leaves the message in place for its real
    consumer, and sending its body back makes a genuine second delivery.

    Returns the message id copied, or None when the queue was empty.
    """
    got = _aws("sqs", "receive-message", "--queue-url", QUEUE.format(queue),
               "--visibility-timeout", "0", "--max-number-of-messages", "1")
    if not got or got.returncode != 0 or not got.stdout.strip():
        return None
    try:
        messages = json.loads(got.stdout).get("Messages") or []
    except ValueError:
        return None
    if not messages:
        return None
    body = messages[0].get("Body")
    sent = _aws("sqs", "send-message", "--queue-url", QUEUE.format(queue),
                "--message-body", body)
    if not sent or sent.returncode != 0:
        return None
    return messages[0].get("MessageId")


DEAD_LETTER_QUEUE = "e2e-dlq"


def dead_letter_messages(limit=20):
    """The messages waiting on the dead letter queue, read without taking them off it.

    A zero visibility timeout leaves each message for the next read, and the dead letter queue has
    no redrive of its own, so the extra receive it counts moves nothing. SQS hands back at most ten
    messages a read and picks them itself, so several reads collect the set.
    Returns the messages and the error the read gave, if it gave one.
    """
    found = {}
    for _ in range(5):
        got = _aws("sqs", "receive-message", "--queue-url", QUEUE.format(DEAD_LETTER_QUEUE),
                   "--visibility-timeout", "0", "--max-number-of-messages", "10",
                   "--attribute-names", "All", "--message-attribute-names", "All")
        if not got:
            return [], "the aws command could not run"
        if got.returncode != 0:
            return [], got.stderr.strip()[:300]
        if not got.stdout.strip():
            break
        try:
            messages = json.loads(got.stdout).get("Messages") or []
        except ValueError:
            return [], "the queue answered with something that is not JSON"
        if not messages:
            break
        for message in messages:
            attributes = message.get("Attributes") or {}
            source = attributes.get("DeadLetterQueueSourceArn") or "an unnamed queue"
            found[message["MessageId"]] = {
                "id": message["MessageId"],
                "source": source.rsplit(":", 1)[-1],
                "receives": attributes.get("ApproximateReceiveCount"),
                "sentAt": attributes.get("SentTimestamp"),
                "body": (message.get("Body") or "")[:300],
            }
        if len(found) >= limit:
            break
    return list(found.values())[:limit], None


def stop_service(which, timeout=60):
    """Stop a service, leaving its queues to fill up behind it."""
    container = RESTARTABLE.get(which)
    if not container:
        return False
    done = _docker("stop", container, timeout=timeout)
    stopped = bool(done and done.returncode == 0)
    if stopped:
        _STOPPED_SERVICES.add(which)
    return stopped


def start_service(which, timeout=60):
    container = RESTARTABLE.get(which)
    if not container:
        return False
    done = _docker("start", container, timeout=timeout)
    started = bool(done and done.returncode == 0)
    if started:
        _STOPPED_SERVICES.discard(which)
    return started


def _docker(*args, timeout=60):
    try:
        return subprocess.run(["docker"] + list(args),
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def restart_service(which, timeout=90):
    """Stop a service and start it again, which is the crudest fault there is.

    A service that dies part way through a payment is the case every retry and every idempotency
    key exists for, and no amount of domain-level injection reaches it.
    """
    container = RESTARTABLE.get(which)
    if not container:
        return False, "no container is registered for {}".format(which)
    try:
        done = subprocess.run(["docker", "restart", container],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as fault:
        return False, "{}: {}".format(type(fault).__name__, fault)
    if done.returncode != 0:
        return False, done.stderr.strip()[:200]
    return True, "{} was stopped and started again".format(container)


def clear_every_toxic():
    """Take every toxic off every boundary, without holding a Toxiproxy client.

    The cleanup path cannot reach the run's own client: a signal arrives wherever the run is, and
    a killed run never gets back to its own shutdown code.
    """
    try:
        with httpx.Client(timeout=5.0) as http:
            listed = http.get("{}/proxies".format(ADMIN))
            if listed.status_code != 200:
                return False
            for name in listed.json():
                toxics = http.get("{}/proxies/{}/toxics".format(ADMIN, name))
                for toxic in toxics.json() if toxics.status_code == 200 else []:
                    http.delete("{}/proxies/{}/toxics/{}".format(ADMIN, name, toxic["name"]))
        return True
    except (httpx.HTTPError, ValueError, KeyError):
        return False


def restore_stack(why="the run ended"):
    """Put back everything the run changed in the stack itself.

    Safe to call more than once: each restored item is dropped from the record, so a second call
    finds nothing to do.
    """
    restored = []
    for queue in sorted(_CHANGED_TIMEOUTS):
        seconds = _CHANGED_TIMEOUTS[queue]
        if set_visibility_timeout(queue, seconds):
            restored.append("{} back to a {}s visibility timeout".format(queue, seconds))
    for which in sorted(_STOPPED_SERVICES):
        if start_service(which):
            restored.append("{} started again".format(which))
    if _BANK_STATUS_BEFORE:
        # Read the value out before the call, because a successful call clears the record.
        normal = _BANK_STATUS_BEFORE[0]
        if set_bank_payment_status(normal):
            restored.append("the bank answers {} again".format(normal))
            _BANK_STATUS_BEFORE.clear()
    if clear_every_toxic():
        restored.append("every boundary passes traffic through")
    if restored:
        print("  -- restoring the stack because {}: {}".format(why, "; ".join(restored)))
    return restored


_INSTALLED = False


def install_cleanup():
    """Restore the stack when the run ends, however it ends.

    `atexit` covers a normal end and an uncaught exception. The signal handlers cover the case
    that did the damage: cycle.py sends SIGTERM to a run still going from an earlier cycle, and a
    process that dies on SIGTERM runs no `atexit` handler at all.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True
    atexit.register(restore_stack, "the process is exiting")

    def on_signal(number, frame):
        restore_stack("signal {} arrived".format(number))
        if number == signal.SIGINT:
            raise KeyboardInterrupt
        sys.exit(128 + number)

    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(number, on_signal)
        except (ValueError, OSError):
            continue


# The bank simulator's own configuration, which decides what it does with a payment it accepts.
# It lives in the hsb database rather than behind an endpoint: nothing in hot-sauce-bank exposes
# `setPaymentAutoProcessingDefaultStatus` over HTTP. `ApplicationConfigurationService` reads the
# row on every call and caches nothing, so a change takes effect on the next payment.
HSB_DSN = os.environ.get("SIM_HSB_DSN", "postgresql://hsb:password@localhost:5444/hsb")

# The bank simulator itself, dialled directly rather than through the harness proxy, because a
# network fault injected on that proxy must not stop the run putting the bank back.
BANK_BASE_URL = os.environ.get("SIM_HSB_URL", "http://localhost:10002")

# The connector profile every Direct cohort pays through, from clearing's application.yml.
INVESTEC_PROFILE_ID = os.environ.get(
    "SIM_INVESTEC_PROFILE_ID", "8345101873944135BCA1F7CDD2F88DCF")

# The Direct cohorts run on the INVESTEC connector, which is the profile world.poll_bank_transactions
# polls, so this is the key that decides the status the bank gives the cohort's payments.
BANK_STATUS_KEY = "b7h.config.payment.auto-processing.investec.default-status"


def _hsb_sql(sql, timeout=30):
    try:
        done = subprocess.run(["psql", HSB_DSN, "-tA", "-v", "ON_ERROR_STOP=1", "-c", sql],
                              capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return done.stdout.strip()


def bank_payment_status():
    """The status the bank gives a payment it accepts, such as ACSC or RJCT, or None."""
    found = _hsb_sql("SELECT value FROM application_configuration WHERE key = '{}'".format(
        BANK_STATUS_KEY))
    return found or None


# What the bank answered with before the run changed it. A run that dies while the bank is
# rejecting leaves every later run's payments rejected, which reads as a defect in the service.
_BANK_STATUS_BEFORE = []


def set_bank_payment_status(status):
    """Make the bank give every later payment this status, such as ACSC or RJCT.

    The simulator owns this setting, so the change goes through its own endpoint rather than
    through the row underneath it. Reading still goes to the database, because the simulator
    offers no endpoint that answers with the status it currently gives.
    """
    if not _BANK_STATUS_BEFORE:
        held = bank_payment_status()
        if held and held != status:
            _BANK_STATUS_BEFORE.append(held)
    try:
        with httpx.Client(timeout=15.0) as http:
            answer = http.post("{}/hsb/payment/management/{}/default-status/{}".format(
                BANK_BASE_URL, INVESTEC_PROFILE_ID, status))
        written = answer.status_code < 300
    except httpx.HTTPError:
        written = False
    if written and _BANK_STATUS_BEFORE[:1] == [status]:
        _BANK_STATUS_BEFORE.clear()
    return written
