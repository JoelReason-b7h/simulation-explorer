"""Runs the explorer over the core-side Direct actions.

One cohort, one platform, one entity in flight at a time, so every change is attributable by entity
identity — which is what §5 of the design requires when an action mints no token to match on.

Set PLATFORM_UID and PLATFORM_VIRTUAL_IBAN for the cohort. The IBAN comes from core's
entity_internal_account, because no ops endpoint exposes it.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
import os
import shutil
import time
import uuid
from decimal import Decimal
import sys

from explorer import (client, actions, config, driver, faults, fleet, integrity, ledger, oracles,
                      preflight, projector, race, triallog, webhooks, weird, world)
from explorer import interest_oracle, journeys, longlived
from explorer.client import BearerClient, Call, DirectClient

# No natural end: the frontier keeps growing as new states appear, so the run continues
# until it is stopped. Set SIM_STEPS to bound it by trials, or SIM_SECONDS to bound it by time.
# A wall-clock bound ends the run through the same path as the step bound, so the final publish
# still happens; killing the process instead leaves the page holding the second-to-last state.
STEPS = int(os.environ.get("SIM_STEPS", "0"))
SECONDS = float(os.environ.get("SIM_SECONDS", "0"))


class Run:
    def __init__(self, client, ops=None, hsb=None, platform_uid=None, virtual_iban=None,
                 sim=None, compliance=None):
        self.client = client
        self.ops = ops
        self.hsb = hsb
        self.sim = sim
        self.compliance = compliance
        self.platform_uid = platform_uid
        self.virtual_iban = virtual_iban
        self.run_id = driver.run_id()
        name = os.environ.get("SIM_RUN_NAME", "run")
        self.page = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".html")
        self.log = triallog.TrialLog(self.page[:-len(".html")] + ".trials.jsonl")
        self.published_at = 0.0
        self.counter = 0
        self.product_id = None
        # A pool of subjects, not one. With a single customer the driver can never spend an
        # entity — closing its only account would strand every action that needs one — so it
        # protects it forever and CloseAccount stays untried. With a pool it forks instead.
        self.subjects = [{}]
        self.current = 0
        self.explorer = driver.Explorer(actions.BY_NAME, creates=actions.CREATES,
                                       budgeted=actions.EXPENSIVE_FAULTS)
        self.races = {}
        self.route = []
        self.route_target = None
        # `route` is consumed step by step, so the plan as first drawn is kept beside it for the
        # page: a reader needs the whole sequence and the position in it, not the remainder.
        self.route_plan = []
        self.route_from = None
        self.route_note = ""
        self.route_state = "none"
        # Which rule chose the action this step. The page shows it, because "what did it do" is
        # only half the question and the other half is which branch of the driver fired.
        self.why = ""
        self.why_counts = {}
        self.stuck = {}
        self.stuck_at = {}
        self.steps = 0
        # How often the driver has pointed at each account. State visits are counted per state
        # key, and every cancelled account of a customer shares one key, so five of them looked
        # cheaper than the single open account and kept winning the tie.
        self.account_picks = {}
        # Every oracle violation the run has found, newest last.
        self.violations = []
        # Batches live on the run, not on a customer: one spans several customers, several can be
        # outstanding at once, and a batch nobody is standing on must still be payable.
        self.batches = []
        self.batch_at = None
        # Logical time. The bank's business date is not readable over HTTP, so the run counts its
        # own advances from a bank that was approved today.
        self.bank_uid = os.environ.get("BANK_UID")
        self.days_advanced = 0
        # How many settlement sweeps have run. A batch is only owed a settlement once a sweep has
        # had the chance to see its payment.
        self.sweeps = 0
        # Bank credits made since the last poll. Polling asks the bank for the whole of today's
        # transactions, so polling when nothing new has been credited re-ingests every earlier one:
        # twelve sweeps against twelve credits produced 2301 statement lines, almost all of which
        # could match nothing and landed at EXCEPTION.
        self.credits_since_poll = 0
        # Every product the platform offers, by type. A cohort with only an INSTANT product can
        # never satisfy "Destination account can only be set for term accounts", so the maturity
        # actions were refused for the whole of every run.
        self.products = {}
        # Network faults are injected on the bank path only, and only while the proxy exists.
        self.faults = faults.Toxiproxy()
        # Every customer ever given a product, counted for the whole run. Counting the live pool
        # instead let retire_finished take the count back down, so the rotation kept restarting
        # and never reached the slot that opens on a product other than INSTANT.
        self.customers_made = 0
        # The last change the run made, kept so ReplayLastCall can send it again unchanged.
        self.last_change = None
        # Which boundary currently carries a network fault, named in the page so a finding can be
        # read against the fault that was live when it happened.
        self.faulted_boundary = None
        # The trial the current fault was injected on, so the run can take it off again.
        self.faulted_at = 0
        # The trial a service was last restarted on, so restarts stay far enough apart.
        self.restarted_at = 0
        # How many interrupted fundings have run, so the cut moves through the sequence.
        self.interruptions = 0
        # Which step the last interrupted funding cut, named in the page beside the finding.
        self.last_interruption = None
        # Which queue is currently delivering every message more than once, and how many times
        # the run has turned duplication on, so it moves between the queues.
        self.duplicating = None
        self.duplications = 0
        # How many messages the run has put a second copy of onto a queue.
        self.messages_copied = 0
        # Work the run started that finishes after the call returned: a compliance result, a
        # settlement sweep, an interest run. A finding names these, because the action the run was
        # holding when a rule broke is not the only thing that could have caused it.
        self.in_flight = []
        # The transition chain sweep: when it last ran, how many it has run, and what psql refused.
        self.chain_swept_at = 0.0
        self.chain_sweeps = 0
        self.chain_baseline = set()
        self.chain_errors = []
        # What each injected closure payment left behind, which is the question the run was built
        # to answer and no oracle can answer from one read.
        self.closure_injections = []
        # What each operator decision on a refused closure's payment group left behind.
        self.operator_decisions = []
        # Decided closures whose account still held money when last read.
        self.decision_watch = []
        # The accounts an injected closure left behind, watched over later closure sweeps.
        self.closing_watch = []
        # How many times the run has tried to injure a closure payment, whether or not the
        # attempt found an account it could close.
        self.closure_attempts = 0
        # The trial the run last tried an injection on, so the attempts spread out rather than
        # firing on consecutive trials against a pool that has not settled yet.
        self.closure_forced_at = 0
        # What each notice closure injection did, and the counters that space the attempts out.
        self.notice_closures = []
        self.notice_attempts = 0
        self.notice_forced_at = 0
        # Every reference the run minted, with its kind, so a SAVINGS_TRANSACTION delivery that
        # carries one can be tied back to the run's own action.
        self.minted = {}
        # The cycle's webhook capture, which fleet_cycle's receiver writes. Unset outside a fleet.
        self.webhook_capture = os.environ.get("SIM_WEBHOOK_CAPTURE")
        self.webhook_checked_at = 0.0
        self.webhook_seen_before = set()
        self.webhook_subscribed = None
        self.webhook_stats = None

    @property
    def held(self):
        view = dict(self.subjects[self.current])
        # Each subject carries its own product, so a TERM customer and an INSTANT customer can be
        # in the pool at once. Every action that names a product — opening, funding, a batch line,
        # a maturity destination — has to name the one the account was opened on.
        product = self.subjects[self.current].get("productId") or self.product_id
        if product:
            view["productId"] = product
            view["productType"] = self.subjects[self.current].get("productType") or "INSTANT"
        batch = self.batch
        if batch:
            view["batchId"] = batch["batchId"]
            view["batchPaymentReference"] = batch["paymentReference"]
            line = self.cancellable_line(batch)
            if line:
                view["cancellableInstructionId"] = line["reference"]
        return view

    @property
    def batch(self):
        """The batch the run is working on, or None while the pool is empty.

        Named rather than indexed: `batch_at` used to count into the full list and then index the
        live subset, so the moment any earlier batch finished the two index spaces diverged and
        every payment and cancellation silently hit the wrong batch.
        """
        live = [b for b in self.batches if not b.get("done")]
        if not live:
            return None
        for candidate in live:
            if candidate.get("paymentReference") == self.batch_at:
                return candidate
        return live[-1]

    def cancellable_line(self, batch):
        """A line of this batch that can still be cancelled, preferring one never tried."""
        for line in batch.get("lines", []):
            if line.get("status") == "PENDING":
                return line
        return None

    def mint(self, kind, limit):
        """Run-id prefixed so replay never collides, and alphanumeric so the 16-char batch reference accepts it."""
        self.counter += 1
        reference = "{}{}{}".format(self.run_id, kind, self.counter)[:limit]
        self.minted[reference] = kind
        return reference

    def read(self, entity):
        held = self.held
        if entity == "customer" and held.get("customerId"):
            call = self.client.call("GET", "/direct/v1/customers/{customerId}".format(**held))
        elif entity == "account" and held.get("accountId"):
            call = self.client.call(
                "GET", "/direct/v1/customers/{customerId}/accounts/{accountId}".format(**held))
        elif entity == "batch" and held.get("batchId"):
            call = self.client.call("GET", "/direct/v1/batches/{batchId}".format(**held))
        else:
            return None
        if not call.ok:
            return None
        body = call.body
        if isinstance(body, dict):
            # Stamp which object this reading is of. The batch read carries no batchId of its own,
            # so without this nothing can tell one batch's reading from another's.
            body = dict(body)
            body.setdefault(self.ID_FIELD.get(entity, "customerId"),
                            held.get(self.ID_FIELD.get(entity, "customerId")))
        return body

    def key(self, entity, snapshot):
        if snapshot is None:
            return (entity, "absent")
        if entity == "customer":
            return projector.customer_key(snapshot)
        if entity == "batch":
            # How much has been paid is the run's own tally, not something the read carries, so
            # it is added here rather than invented inside the projector.
            view = dict(snapshot)
            batch = self.batch
            if batch:
                view["paidState"] = self.paid_state(batch)
            return projector.batch_key(view)
        return projector.account_key(snapshot, self.held.get("customerStatus"),
                                     self.pending_instructions())

    STATUS_FIELD = {"customer": "customerStatus", "account": "status", "batch": "status"}
    ID_FIELD = {"customer": "customerId", "account": "accountId", "batch": "batchId"}

    def check(self, action_name, call, entity, before, after):
        """Run every oracle that the reads this step already took can answer.

        Reading the transactions costs one more call, so it only runs for an account that exists
        and only when the step could have moved money.
        """
        # Carry the whole identifier and both readings, because a violation reporting a status
        # change the database never recorded means the two reads are of different objects, and
        # the short form gave no way to tell which.
        subject = self.held.get("customerId") or "?"
        # Compare the identifier of the entity being checked, not always the customer. A batch
        # body carries no customerId, so the customer comparison never fired for a batch and
        # PlaceBatchPayment reported the old batch turning into the new one 18 times.
        id_field = self.ID_FIELD.get(entity, "customerId")
        before_id = (before or {}).get(id_field) if isinstance(before, dict) else None
        after_id = (after or {}).get(id_field) if isinstance(after, dict) else None
        under_fault = self.live_fault() or fleet.peer_fault()
        own_wire = getattr(call, "own_wire", False) and call.status == oracles.TRANSPORT_FAULT
        found = [] if own_wire else [oracles.no_server_error(action_name, call, subject, under_fault)]
        if not under_fault:
            # A slow answer while the wire carries a second of injected latency is the latency,
            # not the service.
            found.append(oracles.answers_promptly(action_name, call, subject))

        field = self.STATUS_FIELD.get(entity)
        if action_name in actions.CREATES:
            # The after-read is of a different object than the before-read, so comparing their
            # statuses compares two unrelated things.
            field = None
        if before_id and after_id and before_id != after_id:
            # Whatever the action, the two reads are of different customers, so no status
            # comparison between them means anything.
            field = None
        if field and isinstance(before, dict) and isinstance(after, dict):
            found.append(oracles.terminal_not_left(
                entity, before.get(field), after.get(field), subject))

        if entity == "account" and isinstance(after, dict) and after.get("accountId"):
            def balance_oracle(snapshot):
                transactions = self.read_transactions()
                if transactions is None:
                    return None
                return oracles.account_balance(snapshot, transactions)
            found.append(self.confirmed(balance_oracle, after, "account"))

        if entity == "batch" and isinstance(after, dict):
            found.append(self.confirmed(
                lambda snapshot: oracles.batch_total(snapshot, _rows(snapshot)), after, "batch"))

        for violation in found:
            if violation is None:
                continue
            row = violation.as_row()
            row["n"] = len(self.log) + 1
            row["action"] = action_name
            row["beforeId"] = before_id
            row["afterId"] = after_id
            # Carry the evidence with the finding. Chasing a 500 after the fact meant reading a
            # database that had moved on, so the sequence that produced it and the body the
            # service sent are kept on the row itself.
            row["body"] = call.body if isinstance(call.body, (dict, list)) else str(call.body)
            row["leadUp"] = [
                {"n": t["n"], "action": t["action"], "status": t["status"],
                 "message": t.get("message")}
                for t in self.log[-8:]]
            # What else could have caused this. The run drives compliance through the simulator
            # and settlement through the ops endpoints, so a result from an earlier trial can
            # arrive inside any later one: the action the run was holding is not proof of cause.
            concurrent = self.in_flight_now()
            row["inFlight"] = concurrent
            row["attribution"] = "{} alone".format(action_name) if not concurrent else (
                "the run cannot attribute this to {}, because {} was also in flight".format(
                    action_name, ", ".join(sorted({e["what"] for e in concurrent}))))
            self.record_violation(row, violation)

    def record_violation(self, row, violation):
        """Keep one row per distinct finding, with a count, rather than one per occurrence.

        A defect the run can reach over and over fills the report with the same sentence: one run
        recorded the same OpenAccount 500 a hundred and forty-eight times. The first occurrence
        carries the evidence, and the count says how reachable the defect is.
        """
        # Keyed without `actual`, because a measurement differs every time it is taken: the slow
        # read oracle reported 2.01s and 1.47s as two separate findings when they are one. The
        # worst reading seen is kept instead, since that is the number worth acting on.
        same = (row.get("rule"), row.get("action"), row.get("expected"))
        for seen in self.violations:
            if (seen.get("rule"), seen.get("action"), seen.get("expected")) == same:
                seen["count"] = seen.get("count", 1) + 1
                seen["lastSeenAt"] = row["n"]
                if _worse(row.get("actual"), seen.get("actual")):
                    seen["actual"] = row.get("actual")
                    seen["detail"] = row.get("detail")
                return
        row["count"] = 1
        row["lastSeenAt"] = row["n"]
        self.violations.append(row)
        print("  !! ORACLE {}".format(violation))

    # How many trials each kind of started work can still land in. A compliance result comes back
    # through a queue and arrives whenever it arrives, while an ops endpoint whose path ends in
    # `sync` has finished by the time it answers, so listing the two for the same length names a
    # cause that cannot still be acting and weakens every finding that carries it.
    ASYNC_WINDOW_TRIALS = {
        "compliance": 12,
        "closure payment": 8,
        "restart": 6,
        "settlement": 4,
        "closure": 4,
        "interest": 2,
    }
    DEFAULT_ASYNC_WINDOW = 8

    def note_in_flight(self, kind, what, subject=None):
        """Record work that finishes after the call that started it returned."""
        self.in_flight.append(
            {"kind": kind, "what": what, "subject": subject, "trial": self.steps})
        del self.in_flight[:-40]
        if subject is None:
            fleet.note(kind, what)

    def in_flight_now(self):
        """The started work that could still land on this trial."""
        live = []
        for entry in self.in_flight:
            window = self.ASYNC_WINDOW_TRIALS.get(entry["kind"], self.DEFAULT_ASYNC_WINDOW)
            if 0 <= self.steps - entry["trial"] <= window:
                live.append(entry)
        return live + fleet.others_in_flight()

    # How often the transition chains are read. The queries read whole tables, so running them
    # after each trial would cost more than the trials do; three minutes keeps the trial rate and
    # still catches a break while the run that made it is still going.
    CHAIN_SWEEP_SECONDS = float(os.environ.get("SIM_CHAIN_SWEEP_SECONDS", "180"))

    IGNORE_REPEATS_SECONDS = 60

    def ignore_repeats_now(self):
        """Clear the repeated EXCEPTION lines clearing inserts on each poll (finding 7).

        Left alone they grew to 30006 in one cycle and every poll of the queue slowed with them.
        One run in a fleet does it, once a minute, because the queue is shared.
        """
        if fleet.is_member() or not self.ops:
            return
        now = time.time()
        if now - getattr(self, "_repeats_cleared_at", 0.0) < self.IGNORE_REPEATS_SECONDS:
            return
        self._repeats_cleared_at = now
        result = world.ignore_repeated_exceptions(self.ops)
        if result and result[1]:
            print("  -- ignored {} repeated exception lines of {}".format(result[1], result[0]))

    TOP_UP_SECONDS = 60

    def top_up_accounts_now(self):
        """Keep the shared preloaded account pool from running dry. One run in a fleet does it."""
        if fleet.is_member() or not self.ops:
            return
        now = time.time()
        if now - getattr(self, "_topped_up_at", 0.0) < self.TOP_UP_SECONDS:
            return
        self._topped_up_at = now
        statuses = world.top_up_preloaded_accounts(self.ops)
        if any(s >= 400 for s in statuses):
            print("  -- preloaded account top-up answered {}".format(statuses))

    def take_chain_baseline(self):
        """Remember the breaks that exist before the first action, so the run reports only its own.

        The sweep reads whole tables, and cycles share a stack between wipes, so every later cycle
        reported an earlier cohort's closed account as its own finding.
        """
        found, errors = integrity.sweep()
        self.chain_baseline = {(f["rule"], f["subject"]) for f in found}
        self.operator_queues = integrity.operator_queues()
        for entry in errors:
            print("  -- the baseline sweep could not read: {}".format(entry))
        print("  {} breaks were already in the tables before the run".format(
            len(self.chain_baseline)))

    def sweep_transition_chains(self, force=False):
        """Read the chains in core and record each break as a finding.

        A break is a write that did not go through the transition rule, and the API cannot show
        it: a read answers with the status the entity holds now, which a lawful move and an
        unlawful one both leave behind.
        """
        # The sweep reads every platform's rows, so in a fleet only the conductor runs it and
        # each break is reported once.
        if fleet.is_member():
            return 0
        now = time.time()
        if not force and now - self.chain_swept_at < self.CHAIN_SWEEP_SECONDS:
            return 0
        self.chain_swept_at = now
        self.chain_sweeps += 1
        found, errors = integrity.sweep()
        found = [f for f in found if (f["rule"], f["subject"]) not in self.chain_baseline]
        # Ask the service to run its own integrity checks as well, and read what they wrote. The
        # harness had only ever read the chains it wrote itself, so a check the service already
        # owns could fail for a whole run and the page would say nothing about it.
        # The service runs its checks by calling across the boundaries the run breaks, so a check
        # asked for while a fault is live fails on the fault and records it as a failed check.
        if self.live_fault():
            errors.append("the service's own checks were not asked for, because {} is "
                          "injected".format(self.faulted_boundary))
        else:
            errors.extend(integrity.run_system_checks(self.ops))
        self.operator_queues = integrity.operator_queues()
        system_found, system_errors = integrity.system_check_failures()
        found.extend(system_found)
        errors.extend(system_errors)
        self.chain_errors = errors
        for entry in errors:
            print("  -- the chain sweep could not read: {}".format(entry))
        for finding in found:
            violation = oracles.Violation(
                finding["rule"], finding["subject"], finding["detail"],
                expected=finding.get(
                    "expected", "{} for {}".format(finding["table"], finding["subject"])),
                actual=finding.get("actual", " | ".join(finding["row"])))
            row = violation.as_row()
            row["n"] = len(self.log) + 1
            row["action"] = "TransitionChainSweep"
            row["table"] = finding["table"]
            row["row"] = finding["row"]
            if finding["rule"] == "a closed account holds no money":
                row["evidence"] = integrity.closed_account_evidence(finding["subject"])
                print("  -- evidence for closed account {}: {}".format(
                    finding["subject"], json.dumps(row["evidence"])[:1500]))
            row["inFlight"] = self.in_flight_now()
            row["leadUp"] = [
                {"n": t["n"], "action": t["action"], "status": t["status"],
                 "message": t.get("message")}
                for t in self.log[-8:]]
            self.record_violation(row, violation)
        return len(found)

    WEBHOOK_SWEEP_SECONDS = float(os.environ.get("SIM_WEBHOOK_SWEEP_SECONDS", "180"))
    # A transaction this young may still have its delivery on the way, so a sweep leaves it out.
    WEBHOOK_GRACE_SECONDS = float(os.environ.get("SIM_WEBHOOK_GRACE_SECONDS", "120"))
    # How long the final accounting waits for the platform's AWAITING_RESPONSE events to clear.
    WEBHOOK_DRAIN_SECONDS = float(os.environ.get("SIM_WEBHOOK_DRAIN_SECONDS", "180"))

    def account_webhooks(self, final=False):
        """Hold the platform's webhook deliveries up against the Direct API's reads.

        Every run does this for its own platform, members included, because each platform has
        its own url. During the run a finding is recorded only when the sweep before found it
        too, since a delivery can trail its transaction by a retry. The final accounting waits
        for the platform's AWAITING_RESPONSE events to clear, then records only what a second
        reading agrees with.
        """
        if not self.webhook_capture or not self.ops or not self.platform_uid:
            return 0
        now = time.time()
        if not final and now - self.webhook_checked_at < self.WEBHOOK_SWEEP_SECONDS:
            return 0
        self.webhook_checked_at = now
        if self.webhook_subscribed is None:
            types = webhooks.subscribed(self.ops, self.platform_uid)
            self.webhook_subscribed = bool(types) and len(types) == len(webhooks.EVENT_TYPES)
            if not self.webhook_subscribed:
                print("  -- webhook accounting is off: the platform is subscribed to {}".format(
                    types))
        if not self.webhook_subscribed:
            return 0
        waited, left = 0.0, None
        if final:
            while waited < self.WEBHOOK_DRAIN_SECONDS:
                left = webhooks.outstanding(self.ops, self.platform_uid)
                if not left:
                    break
                time.sleep(10)
                waited += 10
        findings, stats = self.webhook_findings(0.0 if final else self.WEBHOOK_GRACE_SECONDS)
        if findings is None:
            return 0
        keys = {(f["rule"], f["expected"]) for f in findings}
        if final and findings:
            time.sleep(20)
            again, stats = self.webhook_findings(0.0)
            confirmed = {(f["rule"], f["expected"]) for f in again or []}
        else:
            confirmed = self.webhook_seen_before
        self.webhook_seen_before = keys
        stats = dict(stats or {})
        stats["final"] = final
        stats["awaitingResponse"] = left if final else webhooks.outstanding(
            self.ops, self.platform_uid)
        stats["drainWaitSeconds"] = waited
        self.webhook_stats = stats
        print("  -- webhook accounting{}: {}".format(" (final)" if final else "",
                                                     json.dumps(stats, sort_keys=True)))
        recorded = 0
        for finding in findings:
            if (finding["rule"], finding["expected"]) not in confirmed:
                continue
            self.note_violation("WebhookAccounting", finding["rule"], finding["subject"],
                                finding["detail"], finding["expected"], finding["actual"])
            recorded += 1
        return recorded

    def webhook_not_before(self):
        """On a long-lived bank, the cycle's start: customers made in earlier cycles had their
        deliveries captured in earlier cycles' files, so only this cycle's are accounted."""
        if not longlived.ENABLED:
            return None
        try:
            return float(os.environ.get("SIM_CYCLE_STARTED") or 0) or None
        except ValueError:
            return None

    def webhook_findings(self, grace):
        world_now = webhooks.read_world(self.client, not_before=self.webhook_not_before())
        if world_now is None:
            print("  -- webhook accounting could not list the platform's customers")
            return None, None
        records, truncated = webhooks.load(self.webhook_capture)
        if truncated:
            print("  -- webhook accounting is off: the capture reached its size limit")
            return None, None
        outstanding = webhooks.outstanding_transactions(self.ops, self.platform_uid)
        findings, stats = webhooks.check(records, world_now, self.platform_uid, self.minted, grace,
                                         outstanding=outstanding)
        if stats is not None:
            stats = dict(stats, outstandingTransactions=len(outstanding))
        return findings, stats

    CONFIRM_PAUSE_SECONDS = float(os.environ.get("SIM_CONFIRM_PAUSE", "2.0"))

    def confirmed(self, oracle, snapshot, entity):
        """A disagreement is only a finding once it survives a second reading.

        A deposit lands on the balance and on the transaction list at different moments, so the
        first reading after funding catches the gap between the two and reports a conservation
        failure that is gone a second later. Re-reading turns that timing artefact into silence
        and leaves a real disagreement standing.
        """
        first = oracle(snapshot)
        if first is None:
            return None
        time.sleep(self.CONFIRM_PAUSE_SECONDS)
        again = self.read(entity)
        if again is None:
            return first
        return oracle(again)

    def read_transactions(self):
        """The account's own transaction list, or None when the service would not give it.

        None rather than an empty list, because the two mean opposite things and the balance
        oracle cannot tell them apart. The transactions read answers 400 "Customer not found" for
        a closed customer while the account read still succeeds, so an empty list was recorded as
        "this account has no transactions" and every funded closed account reported a conservation
        failure — 24 of them in one run.
        """
        held = self.held
        if not held.get("customerId") or not held.get("accountId"):
            return None
        # The listing answers 50 rows a page, newest first. Summing the first page alone dropped the
        # oldest deposit once an account passed 50 transactions, and the balance oracle reported a
        # 3.00 gap eleven times on one account in cycle 29.
        path = "/direct/v1/customers/{customerId}/accounts/{accountId}/transactions".format(**held)
        rows = []
        for page in range(self.TRANSACTION_PAGES):
            call = self.client.call(
                "GET", "{}?skip={}&take={}".format(path, page * self.TRANSACTION_PAGE,
                                                   self.TRANSACTION_PAGE))
            if not call.ok:
                return None
            chunk = _rows(call.body)
            rows.extend(chunk)
            if len(chunk) < self.TRANSACTION_PAGE:
                return rows
        # More rows than the run will read: an incomplete sum would report a false gap.
        return None

    # `take` allows 1000 (ExternalDirectCustomerTransactionPaginatedQueryBean), so one page covers
    # nearly every account the run builds.
    TRANSACTION_PAGE = 1000
    TRANSACTION_PAGES = 5

    def pending_instructions(self, account_id=None, remember=True):
        """How many instructions are still in flight, for this customer or for one of its accounts.

        Read once per step and cached, because the account key is built several times a step and
        each build would otherwise cost another call.

        `account_id` narrows the count to one account. The service refuses a close while a deposit
        is in flight on that account, so a customer-wide count refuses closes the service would
        allow: one run made 28 attempts at the closure injection and landed none, every refusal
        naming an instruction that belonged to another account of the same customer.
        """
        held = self.held
        if not held.get("customerId"):
            return 0
        stamp = (held["customerId"], account_id, self.steps, remember)
        cache = getattr(self, "_pending_cache", None)
        if cache is None:
            cache = self._pending_cache = {}
        if stamp in cache:
            return cache[stamp]
        call = self.client.call(
            "GET", "/direct/v1/customers/{}/instructions".format(held["customerId"]))
        # The instructions read answers with a bare list, while the batch read wraps its rows in
        # `content`, so accept either rather than assuming the two shapes match.
        lines = _rows(call.body) if call.ok else []
        # Keep one pending withdrawal's id, because cancelling an instruction needs it and the
        # driver held no way to name one. The field is `type` on this payload; reading
        # `instructionType`, which the batch lines use, found no withdrawal ever and left
        # CancelWithdrawal unbuildable for the whole run.
        # `remember` is false while the closure injection scans the pool. That scan moves the
        # driver between subjects to read them, and writing the identifier as it went took the
        # identifier off the subject the driver had already chosen CancelWithdrawal for, which
        # killed a run with KeyError: 'instructionId' when the action came to build its path.
        if remember:
            subject = self.subjects[self.current]
            waiting = [l for l in lines
                       if l.get("status") == "PENDING"
                       and (l.get("type") or l.get("instructionType")) == "WITHDRAWAL"
                       and l.get("instructionId")]
            if waiting:
                subject["instructionId"] = waiting[0]["instructionId"]
            else:
                subject.pop("instructionId", None)
        # Keyed per customer and account, and only this step's entries are kept, so the pool can
        # be scanned without one customer evicting the one before it.
        pending = [l for l in lines if l.get("status") == "PENDING"]
        if account_id:
            pending = [l for l in pending if l.get("accountId") == account_id]
        count = len(pending)
        for old_stamp in [k for k in cache if k[2] != self.steps]:
            cache.pop(old_stamp, None)
        cache[stamp] = count
        return count

    def absorb(self, body, action_name=None):
        """Keep every identifier the response hands back, so later actions become constructible.

        A new customer starts a new subject rather than overwriting the current one, so earlier
        customers stay reachable and the driver can return to them.
        """
        if not isinstance(body, dict):
            return
        target = self.subjects[self.current]
        if action_name in actions.CREATES and body.get("customerId") and target.get("customerId"):
            target = {}
            self.subjects.append(target)
            self.current = len(self.subjects) - 1
        if action_name in actions.CREATES and not target.get("productId"):
            product_type, product_id = self.product_for_new_subject()
            target["productId"] = product_id
            target["productType"] = product_type
            self.customers_made += 1
        for field in ("customerId", "accountId", "batchId", "accountReference",
                      "paymentReference", "batchReference", "customerStatus",
                      "batchPaymentReference"):
            if body.get(field):
                target[field] = body[field]
        if ((action_name in actions.CREATES or action_name == "OpenAccount")
                and target.get("customerId")):
            fleet.share_subject(target["customerId"], body.get("accountId"),
                                target.get("productId"))
        if body.get("accountId"):
            seen = target.setdefault("accounts", [])
            if body["accountId"] not in [a["id"] for a in seen]:
                seen.append({"id": body["accountId"],
                             "reference": body.get("accountReference")})

    # A fee rise lowers the customer's rate, so it must start at least fifteen days out plus the
    # product's notice; a fall may start today. The run alternates the two, so the accruals of a
    # long cycle cross both kinds of change.
    PLATFORM_FEE_HIGH = "0.0050"
    PLATFORM_FEE_LOW = "0.0001"
    PLATFORM_FEE_LEAD_DAYS = 45

    def change_platform_fee(self):
        """Propose a change to this platform's fee on the product the subject holds, and approve it."""
        held = self.held
        product = held.get("productId") or self.product_id
        if not self.platform_uid or not product:
            return Call("POST", "ChangePlatformFee", 412, {"message": "no platform product"}, 0)
        raising = not getattr(self, "platform_fee_raised", False)
        start = date.today() + timedelta(days=self.PLATFORM_FEE_LEAD_DAYS if raising else 0)
        proposal = self.ops.call(
            "POST", "/operations/proposals/platforms/{}/platform-products/{}/platform-fee-rate"
            .format(self.platform_uid, product),
            json_body={"rate": self.PLATFORM_FEE_HIGH if raising else self.PLATFORM_FEE_LOW,
                       "startDate": start.isoformat()})
        approval = (proposal.body or {}).get("approvalUid") if isinstance(proposal.body, dict) \
            else None
        if not proposal.ok or not approval:
            return proposal
        accepted = self.ops.call("POST", "/operations/approvals/{}/accept".format(approval), json_body={})
        if accepted.ok:
            self.platform_fee_raised = raising
            self.note_in_flight("fee", "a platform fee change from {}".format(start))
        return Call("POST", "ChangePlatformFee", accepted.status, {
            "message": "{} the platform fee to {} from {}: proposal {}, approval {}".format(
                "raised" if raising else "lowered",
                self.PLATFORM_FEE_HIGH if raising else self.PLATFORM_FEE_LOW, start,
                proposal.status, accepted.status),
            "approval": accepted.body}, proposal.elapsed_ms + accepted.elapsed_ms)

    # A rise may start today; a cut must start at least fifteen days out plus the product's notice
    # (ProductRateDateValidator), and forty-five clears the two-day notice product with room. Each
    # rise climbs from the last so it always changes the rate, and each cut starts a day later
    # than the one before so the future rates stack rather than land on one date.
    BANK_RATE_RISE_FROM = Decimal("0.0410")
    BANK_RATE_RISE_STEP = Decimal("0.0010")
    BANK_RATE_RISES_BEFORE_WRAPPING = 40
    BANK_RATE_CUT = Decimal("0.0300")
    BANK_RATE_CUT_LEAD_DAYS = 45
    RATE_APPROVALS = ("/operations/approvals?approvalType=PRODUCT_RATE_UPDATE"
                      "&paginatedProperty=CREATED_AT&orderAscDesc=DESC&take=50")

    def change_bank_rate(self):
        """Propose a new gross rate on the bank product under this subject, and approve it."""
        held = self.held
        product = held.get("productId") if held.get("productType") != "TERM" else None
        found = world.bank_product_for(product or self.product_id)
        if not found:
            return Call("POST", "ChangeBankRate", 412,
                        {"message": "could not read the bank product from core"}, 0)
        product_uid, name, product_type, bank_uid = found
        turns = getattr(self, "bank_rate_turns", {})
        self.bank_rate_turns = turns
        turn = turns.get(product_uid, 0)
        raising = turn % 2 == 0
        if raising:
            rises = (turn // 2) % self.BANK_RATE_RISES_BEFORE_WRAPPING
            rate = self.BANK_RATE_RISE_FROM + self.BANK_RATE_RISE_STEP * rises
            start = date.today()
        else:
            rate = self.BANK_RATE_CUT
            start = date.today() + timedelta(days=self.BANK_RATE_CUT_LEAD_DAYS + turn // 2)
        proposal = self.ops.call(
            "PUT", "/operations/proposals/banks/{}/products/{}/rates".format(bank_uid, product_uid),
            json_body={"grossRate": str(rate), "startDate": start.isoformat(),
                       "announcedAt": date.today().isoformat()})
        if not proposal.ok:
            return proposal
        # The proposal answers with no body, so the approval is found by the product's name, as
        # the acceptance suite does. Core refuses a second proposal on a bank product while one
        # is waiting, so at most one row can match.
        listed = self.ops.call("GET", self.RATE_APPROVALS)
        rows = (listed.body.get("content") or []) if listed.ok and isinstance(listed.body, dict) \
            else []
        approval = next((row.get("approvalUid") for row in rows
                         if row.get("approvalType") == "PRODUCT_RATE_UPDATE"
                         and row.get("entityName") == name
                         and row.get("approvalStatus") in (None, "PENDING")), None)
        if not approval:
            return Call("POST", "ChangeBankRate", 412, {
                "message": "proposed {} on {} but found no approval to accept".format(rate, name)},
                proposal.elapsed_ms + listed.elapsed_ms)
        accepted = self.ops.call("POST", "/operations/approvals/{}/accept".format(approval),
                                 json_body={})
        if accepted.ok:
            turns[product_uid] = turn + 1
            self.note_in_flight("rate", "a {} bank rate change to {} from {}".format(
                product_type, rate, start))
        else:
            # A proposal left waiting blocks every later one on this bank product.
            self.ops.call("POST", "/operations/approvals/{}/reject".format(approval), json_body={})
        return Call("POST", "ChangeBankRate", accepted.status, {
            "message": "{} {} to {} from {}: proposal {}, approval {}".format(
                "raised" if raising else "cut", name, rate, start, proposal.status,
                accepted.status),
            "approval": accepted.body},
            proposal.elapsed_ms + listed.elapsed_ms + accepted.elapsed_ms)

    def weird_call(self):
        """Send a real request with one thing wrong or strange in it, and judge the answer.

        The generic check already reads the customer before and after, so a refused request that
        still moved something shows up as a change; this method adds the two rules the mutation
        itself decides: no 5xx for any of them, and a refusal for an invalid one.
        """
        held = self.held
        bases = [n for n in weird.BASES if actions.BY_NAME[n].can_build(held)]
        if not bases:
            return Call("POST", "WeirdCall", 412, {"message": "nothing to build a request from"}, 0)
        base = actions.BY_NAME[bases[self.steps % len(bases)]]
        method, path, body = base.build(held, self.mint)
        changed, mutation = weird.mutate(body, self.steps // len(bases))
        if changed is None:
            return Call("POST", "WeirdCall", 412, {"message": "no mutation fits " + base.name}, 0)
        name, invalid = mutation
        call = self.client.call(method, path, json_body=changed)
        rule = None
        # 598 is the harness's own code for a transport fault, not an answer from the service.
        if 500 <= call.status < 598:
            rule = "a strange request is refused, not answered with a server error"
        elif invalid and call.ok:
            rule = "an invalid request is refused"
        if rule:
            violation = oracles.Violation(
                rule, held.get("customerId"), "{} on {} answered {}".format(
                    name, base.name, call.status),
                expected="a 4xx" if invalid else "no 5xx",
                actual="{} {}".format(call.status, str(call.body)[:300]))
            row = violation.as_row()
            row["n"] = len(self.log) + 1
            row["action"] = "WeirdCall {}".format(base.name)
            row["body"] = call.body if isinstance(call.body, (dict, list)) else str(call.body)
            row["request"] = {"method": method, "path": path, "mutation": name,
                              "body": str(changed)[:1500]}
            row["inFlight"] = self.in_flight_now()
            row["attribution"] = "WeirdCall alone" if not row["inFlight"] else "in flight"
            self.record_violation(row, violation)
        return Call(method, "WeirdCall", call.status, {
            "message": "{} on {}: {}".format(name, base.name, str(call.body)[:200])},
            call.elapsed_ms)

    def run_data_feed(self):
        """Ask core for the bank's data feed and then its RECON, as their schedules would.

        RECON moves one business date per run and the feed holds until the RECON before it is
        sealed, so the pair is asked for together. checks/direct_feed.py reads the files after the
        cycle; here the run only records that the files were cut while its trials were in flight.
        """
        if not self.bank_uid:
            return Call("POST", "RunDataFeed", 412, {"message": "BANK_UID is not set"}, 0)
        self.note_in_flight("feed", "a data feed and RECON run")
        world.mark_feed_files_sent(self.bank_uid)
        feed = self.ops.call("POST", "/operations/batch/processor/bank/{}/DIRECT_DATA_FEED/sync"
                             .format(self.bank_uid))
        recon = self.ops.call("POST", "/operations/batch/processor/bank/{}/DIRECT_DATA_RECON/sync"
                              .format(self.bank_uid))
        worst = feed if not feed.ok else recon
        return Call("POST", "RunDataFeed", worst.status, {
            "message": "feed {} recon {}".format(feed.status, recon.status),
            "feed": feed.body, "recon": recon.body}, feed.elapsed_ms + recon.elapsed_ms)

    def probe_other_platform(self):
        """Reach for another platform's customer with this platform's token.

        Each call must be refused, because the platform in the token is the tenant boundary. A 2xx
        on any of them is a breach, whatever the body says, so the finding carries the answer.
        """
        theirs = fleet.foreign_subject(lambda found: found[self.steps % len(found)])
        if not theirs:
            return Call("GET", "ProbeOtherPlatform", 412,
                        {"message": "no other platform has shared a customer yet"}, 0)
        customer = theirs["customerId"]
        calls = [
            ("read the customer", self.client.call("GET", "/direct/v1/customers/{}".format(
                customer))),
            ("read the balances", self.client.call(
                "GET", "/direct/v1/customers/{}/balances".format(customer))),
            ("read the instructions", self.client.call(
                "GET", "/direct/v1/customers/{}/instructions".format(customer))),
        ]
        if theirs.get("accountId") and theirs.get("productId"):
            reference = self.mint("x", 36)
            calls.append(("withdraw from the account", self.client.call(
                "POST", "/direct/v1/customers/{}/accounts/{}/instruction".format(
                    customer, theirs["accountId"]),
                json_body={"instructionRequestType": "WITHDRAW",
                           "productId": theirs["productId"], "amount": "0.01",
                           "instructionReference": reference})))
        for what, call in calls:
            if call.ok:
                violation = oracles.Violation(
                    "a platform cannot reach another platform's customer", customer,
                    "{} of {}'s customer answered {}".format(what, theirs["run"], call.status),
                    expected="a refusal", actual="{} {}".format(call.status, str(call.body)[:300]))
                row = violation.as_row()
                row["n"] = len(self.log) + 1
                row["action"] = "ProbeOtherPlatform"
                row["body"] = call.body if isinstance(call.body, (dict, list)) else str(call.body)
                row["inFlight"] = []
                row["attribution"] = "ProbeOtherPlatform alone"
                self.record_violation(row, violation)
        worst = max(calls, key=lambda c: (c[1].ok, c[1].status))[1]
        return Call("GET", "ProbeOtherPlatform", 403 if not worst.ok else worst.status,
                    {"message": "; ".join("{} {}".format(w, c.status) for w, c in calls)}, 0)

    def fund_account(self):
        """The grouped action §3 declares as plumbing: batch, credit, poll, process, dues, settle.

        It is one action to the driver, not six, because the six have no meaning apart and the
        driver would otherwise count each as a separate trial against the same account.
        """
        batch = self.client.call("POST", "/direct/v1/batches", json_body=self.one_line_batch())
        if not batch.ok:
            return batch
        # Raise the dues BEFORE crediting the bank. PartnerPaymentMatchingService creates a
        # funding record the moment the statement line drains, and matches it against the payment
        # dues that exist at that instant; with none raised yet it logs "No payment dues found to
        # match funding record" and the record is never revisited. Crediting first left 4838 of
        # 4937 statement lines at EXCEPTION and almost no deposit ever completed.
        steps = [
            world.raise_platform_dues(self.ops, self.platform_uid),
            self.credit_and_count("3.00", batch.body["paymentReference"]),
            self.poll_if_new_money(),
            world.drain_transactions(self.ops),
            world.settle_payments(self.ops),
            world.drain_transactions(self.ops),
        ]
        failed = _first_failure(steps)
        if failed is not None:
            return failed
        # FundAccount ends in the same settle sequence as SettleWorld, so it counts as a sweep.
        self.sweeps += 1
        return batch

    def credit_and_count(self, amount, reference):
        call = world.credit_platform_at_bank(
            self.hsb, self.virtual_iban, amount, reference,
            counterpart="GB29NWBK60161331926819")
        if getattr(call, "ok", False):
            self.credits_since_poll += 1
        # This call crosses only the harness's own proxy into the bank simulator, so no answer
        # means the run cut its own wire. FundAccount and PayBatchPart return it as their result,
        # so without this mark the run reported its own cut as the service failing.
        call.own_wire = True
        return call

    # Where the funding sequence is cut, and which boundary carries that step. Cutting a boundary
    # the step never crosses changes nothing: breaking clearing-to-bank before the settlement
    # sweep left every interrupted funding answering 200 with clearing logging no timeout at all,
    # because settlement drives core and clearing over their own wire and never reads the bank.
    INTERRUPTIONS = (
        # The credit never reaches the bank, so the money did not leave and nothing should land.
        ("the credit into the bank", faults.OWN_BANK_PROXY),
        # The credit lands, and clearing cannot read the statement that would attribute it.
        ("clearing reading the bank statement", faults.CLEARING_TO_BANK),
        # The statement is read and attributed, and core cannot raise the due that moves it on.
        ("core raising the payment due", faults.CORE_TO_CLEARING),
        # Clearing dies outright with the credit already made and the settlement not yet run.
        # A cut wire leaves the service alive and able to finish its work once the wire returns;
        # a restart takes its in-memory state with it, which is the harder case.
        ("clearing dying with the money in flight", "restart:clearing"),
    )

    def fund_account_interrupted(self):
        """Fund an account with one step of the sequence cut off at the wire.

        A fault injected between two whole actions never makes this state, because FundAccount
        runs its six steps to the end before the driver looks again. Cutting inside leaves the
        money part way along, which is what the conservation oracles exist to judge.

        The wire is healed before returning, so the trials that follow are judged on a clean stack
        and the interruption is attributable to this action alone.
        """
        if not self.faults.ready:
            return Call("POST", "fund-interrupted", 412,
                        {"message": "toxiproxy is not running, so nothing can be interrupted"}, 0)
        where, boundary = self.INTERRUPTIONS[self.interruptions % len(self.INTERRUPTIONS)]
        self.interruptions += 1
        restarting = boundary.startswith("restart:")
        if restarting:
            since = self.steps - self.restarted_at
            if self.restarted_at and since < self.RESTART_COOLDOWN_TRIALS:
                return Call("POST", "fund-interrupted", 412, {
                    "message": "a service was restarted {} trials ago, and the run waits {} "
                               "between restarts".format(since,
                                                         self.RESTART_COOLDOWN_TRIALS)}, 0)
        elif boundary not in self.faults.proxies():
            return Call("POST", "fund-interrupted", 412,
                        {"message": "no proxy called {} exists".format(boundary)}, 0)

        batch = self.client.call("POST", "/direct/v1/batches", json_body=self.one_line_batch())
        if not batch.ok:
            return batch
        reference = batch.body["paymentReference"]

        def cut():
            self.faults.add(boundary, "cut", "timeout", self.CUT)
            self.faulted_boundary = boundary
            self.faulted_at = self.steps

        if restarting:
            # Credit the bank, then take clearing down before anything settles.
            self.credit_and_count("3.00", reference)
            self.poll_if_new_money()
            self.restarted_at = self.steps
            self.faulted_boundary = boundary
            self.faulted_at = self.steps
            faults.restart_service(boundary.split(":", 1)[1])
            faults.wait_until_healthy(boundary.split(":", 1)[1])
            world.drain_transactions(self.ops)
            world.raise_platform_dues(self.ops, self.platform_uid)
        elif boundary == faults.OWN_BANK_PROXY:
            cut()
            self.credit_and_count("3.00", reference)
        else:
            self.credit_and_count("3.00", reference)
            if boundary == faults.CLEARING_TO_BANK:
                cut()
                self.poll_if_new_money()
                world.drain_transactions(self.ops)
            else:
                self.poll_if_new_money()
                world.drain_transactions(self.ops)
                cut()
                world.raise_platform_dues(self.ops, self.platform_uid)
        world.settle_payments(self.ops)

        self.faults.clear()
        self.faulted_boundary = None
        self.sweeps += 1
        self.refresh_batches()
        self.check_batches_settled()
        # Read the batch on a healed wire, because that reading is what says where the money is.
        read = self.client.call("GET", "/direct/v1/batches/{}".format(batch.body["batchId"]))
        self.last_interruption = where
        return read

    def fund_account_duplicated(self):
        """Fund an account so that clearing receives one of its messages twice.

        Stopping clearing first is what makes this work. The queues drain in milliseconds while
        clearing is up, so there is never a message sitting there to copy, and setting the
        visibility timeout to zero changes nothing either because the consumer deletes a message
        the moment it takes it. With clearing stopped the messages pile up, the copy goes on
        beside the original, and clearing sees both when it comes back.
        """
        since = self.steps - self.restarted_at
        if self.restarted_at and since < self.RESTART_COOLDOWN_TRIALS:
            return Call("POST", "fund-duplicated", 412, {
                "message": "clearing was stopped {} trials ago, and the run waits {} between "
                           "stops".format(since, self.RESTART_COOLDOWN_TRIALS)}, 0)
        if not faults.stop_service("clearing"):
            return Call("POST", "fund-duplicated", 412,
                        {"message": "could not stop clearing"}, 0)
        self.restarted_at = self.steps
        self.faulted_boundary = "clearing stopped, with a message delivered twice"
        self.faulted_at = self.steps
        # Other runs' 500s while clearing is down belong to this stop; without the note fleet
        # 183 blamed them on a cut that had already expired.
        fleet.note("restart", "clearing stopped to deliver a message twice")
        copied = []
        try:
            # These are the steps that put messages on the queues clearing consumes.
            batch = self.client.call("POST", "/direct/v1/batches",
                                     json_body=self.one_line_batch())
            if batch.ok:
                self.credit_and_count("3.00", batch.body["paymentReference"])
                world.raise_platform_dues(self.ops, self.platform_uid)
            for queue in sorted(faults.DUPLICATING_QUEUES.values()):
                found = faults.duplicate_one_message(queue)
                if found:
                    copied.append(found)
        finally:
            faults.start_service("clearing")
            faults.wait_until_healthy("clearing")
            self.faulted_at = self.steps
        # Let clearing work through both copies, then read where the money ended up.
        self.settle_world()
        self.faulted_boundary = None
        self.messages_copied += len(copied)
        if not batch.ok:
            return batch
        return self.client.call("GET", "/direct/v1/batches/{}".format(batch.body["batchId"]))

    # What the run makes the bank answer while it injects a rejection. The normal value is read
    # first and put back afterwards, rather than assumed, because a run that ends mid-injection
    # would otherwise leave the bank rejecting every payment of every later run.
    BANK_REJECTS = "RJCT"

    # Each injection runs once per run, in this order, and the driver may choose either again
    # afterwards out of the fault budget.
    CLOSURE_INJECTIONS = ("RejectClosurePayment", "ReturnClosurePayment")

    # Which action made each observation, so a finding names the action a reader can look up.
    INJECTION_BY_CASE = {"rejected": "RejectClosurePayment", "returned": "ReturnClosurePayment"}

    # How many trials the run explores before it forces the first closure payment to fail. Late
    # enough that the pool holds a funded account, early enough that both injections still run.
    TRIALS_BEFORE_A_CLOSURE_PAYMENT_FAILS = 60

    # How many times the run may force an injection that refuses itself. Without a limit, a run
    # whose pool holds no account with money would force one on every trial and explore nothing.
    CLOSURE_ATTEMPTS_ALLOWED = 20

    # How many trials pass between two forced attempts. Eight attempts fired inside twelve trials
    # and every one of them found the same account still settling its deposit.
    TRIALS_BETWEEN_CLOSURE_ATTEMPTS = 12

    # How many closure sweeps an account gets before the run calls it stuck. One sweep can be in
    # flight when the closure is requested, and an INSTANT account also waits for its interest
    # schedule to drain, so a single sweep proves nothing.
    CLOSURE_SWEEPS_BEFORE_CLOSED_IS_OWED = 3

    def closable_subject(self):
        """Stand on an account this run can close, and answer with it, or say why none will do.

        The injection chooses the account rather than taking whichever one the driver holds, and it
        funds that account itself when it holds nothing. Hunting for an account that already held
        money did not work: closing is refused while any instruction of that customer is in flight,
        and the driver leaves batches part paid, so the customers holding money were exactly the
        customers with an instruction that no settlement sweep would ever complete.

        An account qualifies when it is OPEN, on an INSTANT or NOTICE product, and its customer has
        no instruction in flight. The balance the service reports decides whether it needs funding.
        """
        saved = self.current
        refusals = []
        empty = None
        # One settlement sweep for the whole scan, because a sweep costs several seconds and one
        # is enough to complete every deposit the run has already paid for.
        settled_once = False
        for i, subject in enumerate(self.subjects):
            if subject.get("closed") or not subject.get("customerId"):
                continue
            if not subject.get("accountId") or not subject.get("accountReference"):
                continue
            if (subject.get("productType") or "INSTANT") == "TERM":
                # The service refuses it: "Only Instant or Notice accounts can be closed".
                continue
            self.current = i
            # A customer holds several accounts and the driver carries one identifier, so point
            # the subject at an OPEN one before reading. Without this the scan saw only whichever
            # account the driver absorbed last, and a run whose newest accounts were CANCELLED or
            # CLOSED reported "no account this run may close" while the customer still held one.
            self.point_at_an_open_account()
            account = self.read("account") or {}
            # REQUESTED counts, because an account stays REQUESTED until a deposit lands, so the
            # accounts with no money are nearly all REQUESTED rather than OPEN. Funding one moves
            # it to OPEN and gives the closure a payment to make fail.
            if account.get("status") not in ("OPEN", "REQUESTED"):
                refusals.append("one account reads {}".format(account.get("status")))
                continue
            if self.pending_instructions(account_id=subject["accountId"], remember=False):
                # A deposit the run made itself is in flight on this account, so settle once and
                # read again. Giving up here refused every account the run had just funded, which
                # is most of the accounts that hold money.
                if settled_once:
                    refusals.append("one open account has an instruction in flight")
                    continue
                # The driver leaves batches part paid, so after a few minutes every customer holds
                # a deposit that no sweep completes. Cycle 25 refused the closure 18 times for that
                # reason and never reached an operator decision. Paying what each batch still owes
                # lets the sweep complete those deposits.
                self.pay_off_batches()
                self.settle_world()
                settled_once = True
                self._pending_cache = {}
                if self.pending_instructions(account_id=subject["accountId"], remember=False):
                    refusals.append("one open account has an instruction in flight")
                    continue
                account = self.read("account") or {}
            try:
                balance = Decimal(str(account.get("balance") or "0"))
            except (ArithmeticError, ValueError):
                balance = Decimal("0")
            if balance > 0:
                return self.held, None
            if empty is None:
                empty = i
        if empty is None:
            self.current = saved
            return None, "; ".join(sorted(set(refusals))[:3]) or "no account this run may close"
        # Fund it here, because closing an empty account raises no payment and leaves the bank
        # nothing to refuse.
        self.current = empty
        funded = self.fund_account()
        self._pending_cache = {}
        account = self.read("account") or {}
        try:
            balance = Decimal(str(account.get("balance") or "0"))
        except (ArithmeticError, ValueError):
            balance = Decimal("0")
        if balance <= 0:
            self.current = saved
            return None, "funding the account for the closure answered {} and moved nothing".format(
                getattr(funded, "status", "nothing"))
        return self.held, None

    def pay_off_batches(self):
        """Credit the bank with what every live batch still owes, and answer how many were paid."""
        paid = 0
        for batch in self.batches:
            if batch.get("done") or not batch.get("paymentReference"):
                continue
            owed = self.owed(batch)
            if owed <= 0:
                continue
            call = self.credit_and_count("{:.2f}".format(owed), batch["paymentReference"])
            if getattr(call, "ok", False):
                batch["paid"] += owed
                batch.setdefault("payments", []).append({"shape": "payoff", "amount": str(owed)})
                paid += 1
        return paid

    def point_at_an_open_account(self):
        """Move the held subject onto an account that is OPEN, or failing that one that is
        REQUESTED, because either can carry the closure once it holds money."""
        subject = self.subjects[self.current]
        known = subject.get("accounts") or []
        if len(known) < 2:
            return
        saved_id = subject.get("accountId")
        saved_ref = subject.get("accountReference")
        second_best = None
        for held in known:
            subject["accountId"] = held["id"]
            if held.get("reference"):
                subject["accountReference"] = held["reference"]
            status = (self.read("account") or {}).get("status")
            if status == "OPEN":
                return
            if status == "REQUESTED" and second_best is None:
                second_best = (held["id"], held.get("reference"))
        if second_best is not None:
            subject["accountId"] = second_best[0]
            if second_best[1]:
                subject["accountReference"] = second_best[1]
            return
        subject["accountId"] = saved_id
        if saved_ref is None:
            subject.pop("accountReference", None)
        else:
            subject["accountReference"] = saved_ref

    def reject_closure_payment(self):
        """Close a funded account while the bank rejects every payment.

        Closing drains the account, so a rejected payment is the case where the money leaves the
        product account and never arrives. Clearing debits the customer cash account when it
        raises the payment due, and the `RJCT` reaches only a `log.error`, so nothing credits the
        money back. The run records where the money stopped.
        """
        # One fault at a time across the whole stack. Without this the injection took over the
        # record of which fault was live, and a 500 caused by a cut wire was then reported as an
        # unexplained server error on the closure. An attempt refused here costs no allowance,
        # because the wire the other fault sits on says nothing about this injection.
        live = self.live_fault()
        if live:
            return Call("POST", "reject-closure-payment", 412,
                        {"message": "{} is already injected".format(live)}, 0)
        self.closure_attempts += 1
        self.closure_forced_at = self.steps
        held, refused = self.closable_subject()
        if not held:
            return Call("POST", "reject-closure-payment", 412, {"message": refused}, 0)
        normal = faults.bank_payment_status()
        if not normal:
            return Call("POST", "reject-closure-payment", 412,
                        {"message": "could not read the bank simulator's payment status"}, 0)
        if not faults.set_bank_payment_status(self.BANK_REJECTS):
            return Call("POST", "reject-closure-payment", 412,
                        {"message": "could not make the bank reject payments"}, 0)
        self.faulted_boundary = "the bank rejects every payment"
        self.faulted_at = self.steps
        before = ledger.newest_payment_sid()
        close = None
        try:
            close = self.client.call(
                "POST", "/direct/v1/customers/{customerId}/accounts/{accountId}/close"
                        "?reason=NO_LONGER_NEEDED".format(**held))
            if close.ok:
                self.note_in_flight("closure payment", "a closure payment the bank rejects",
                                    held["accountId"])
                self.finalise_the_closure(held["accountId"])
                # The bank must still be rejecting when clearing sends the payment, and clearing
                # sent one 12 seconds after the closure sweep raised the withdrawal.
                self.wait_for_payment_after(before, held.get("accountId"))
                # Sending a payment is not the same as learning its outcome, so the rejection
                # reaches clearing only once the status enquiry has run.
                world.enquire_payment_status(self.ops)
                self.settle_world()
        finally:
            faults.set_bank_payment_status(normal)
            self.faulted_boundary = None
        if close is None or not close.ok:
            return close
        clearing_account = ledger.clearing_account_of(held.get("accountId"))
        payment = (ledger.payment_for_account_after(before, clearing_account) if clearing_account
                   else None)
        if payment:
            payment = ledger.payment_by_sid(payment["sid"]) or payment
        observed = self.record_closure_injection("rejected", held, payment=payment)
        if payment and payment.get("status") == "RJCT":
            self.decide_refused_group(held, payment)
        return observed

    # What an operator sends for each refused closure's waiting group, one plan per refusal, in
    # this order. The first plan is the refusal the nominated account causes: APPROVE resends to the
    # same creditor, so the bank refuses again and the group returns to PENDING_APPROVAL, and then
    # REJECT_DISAGGREGATE raises the dues again against the owner's newest verified account.
    APPROVE_WHILE_REFUSING = "APPROVE_WHILE_REFUSING"
    OPERATOR_PLANS = (
        (APPROVE_WHILE_REFUSING, "REJECT_DISAGGREGATE"),
        ("REJECT_FAIL",),
        ("APPROVE",),
        ("CANCEL",),
    )
    DECIDE_GROUP = "/operations/own/payment/management/payment/groups/{}/{}"

    def decide_refused_group(self, held, payment):
        """Act as the operator on the group a refused closure payment waits in.

        The refusal moves the group to PENDING_APPROVAL in SQL and raises no ops task, so the run
        acts as an operator would once they found it, and records where the money ends up.
        """
        plan = self.OPERATOR_PLANS[self.next_operator_plan() % len(self.OPERATOR_PLANS)]
        steps = []
        for decision in plan:
            group = ledger.group_of_payment(payment["sid"])
            if not group or group["status"] != "PENDING_APPROVAL":
                break
            steps.append(self.send_operator_decision(held, payment, group, decision))
        if not steps:
            return None
        account = self.settled_account_reading()
        observation = {
            "plan": list(plan),
            "trial": self.steps,
            "accountId": held.get("accountId"),
            "payment": payment.get("end_to_end_id"),
            "steps": steps,
            "statusAfter": account.get("status"),
            "balanceAfter": account.get("balance"),
        }
        self.operator_decisions.append(observation)
        for step in steps:
            self.record_decision_violation(step.pop("violation"), observation)
        # Core can settle a re-sent payout minutes after the decision: in cycles 26 and 32 it came
        # after a 240-second wait. So the account is watched over the following sweeps, and it is
        # reported only if it still holds money once DECISION_SETTLE_SECONDS have passed.
        self.decision_watch.append({"observation": observation, "since": time.monotonic(),
                                    "decisions": " then ".join(s["decision"] for s in steps)})
        self.check_decided_closures()
        print("  -- operator {} on account {}: account {} holding {}".format(
            " then ".join(s["decision"] for s in steps), (held.get("accountId") or "?")[:8],
            account.get("status"), account.get("balance")))
        return observation

    # Each cycle is a new process and a cycle gets two or three refused closures, so a count held in
    # memory would start every cycle on the first plan and never reach the last two.
    PLAN_COUNTER = ".operator-plan-index"

    def next_operator_plan(self):
        try:
            with open(self.PLAN_COUNTER) as f:
                index = int(f.read().strip() or 0)
        except (OSError, ValueError):
            index = 0
        try:
            with open(self.PLAN_COUNTER, "w") as f:
                f.write(str(index + 1))
        except OSError:
            pass
        return index

    DECISION_SETTLE_SECONDS = 600

    def check_decided_closures(self):
        """Record when each decided closure's account reaches zero, or report it after the wait."""
        still = []
        for watched in self.decision_watch:
            observation = watched["observation"]
            elapsed = time.monotonic() - watched["since"]
            balance = ledger.core_balance(observation["accountId"])
            observation["balanceLater"] = balance
            observation["secondsSinceDecision"] = round(elapsed)
            try:
                empty = balance is not None and Decimal(balance) == 0
            except ArithmeticError:
                empty = False
            if empty:
                observation["settledAfterSeconds"] = round(elapsed)
            elif elapsed >= self.DECISION_SETTLE_SECONDS:
                self.record_decision_violation(oracles.decided_closure_is_empty(
                    observation["accountId"], observation.get("statusAfter"), balance,
                    "{} {} seconds before".format(watched["decisions"], round(elapsed))),
                    observation)
            else:
                still.append(watched)
        self.decision_watch = still

    def record_decision_violation(self, violation, observation):
        if violation is None:
            return
        row = violation.as_row()
        row["n"] = len(self.log) + 1
        row["action"] = "DecideRefusedClosure"
        row["body"] = observation
        row["inFlight"] = self.in_flight_now()
        row["attribution"] = "an operator's {} on a refused closure payment".format(
            " then ".join(observation.get("plan") or []))
        row["leadUp"] = [
            {"n": t["n"], "action": t["action"], "status": t["status"],
             "message": t.get("message")}
            for t in self.log[-8:]]
        self.record_violation(row, violation)

    def send_operator_decision(self, held, payment, group, decision):
        """Send one decision for the group, settle what it starts, and answer what it left."""
        refusing = decision == self.APPROVE_WHILE_REFUSING
        sent = "APPROVE" if refusing else decision
        normal = faults.bank_payment_status() if refusing else None
        if refusing and not (normal and faults.set_bank_payment_status(self.BANK_REJECTS)):
            return {"decision": decision, "note": "could not make the bank reject payments",
                    "violation": None}
        before = ledger.newest_payment_sid()
        try:
            call = self.ops.call("PUT", self.DECIDE_GROUP.format(group["uid"], sent))
            self.note_in_flight("operator decision", "an operator's {}".format(decision),
                                held.get("accountId"))
            resent = None
            if sent in ("APPROVE", "REJECT_DISAGGREGATE"):
                # Both send a new payment: APPROVE in the same group, REJECT_DISAGGREGATE in a new
                # group once the next send run aggregates the dues again.
                resent = self.wait_for_payment_after(before, held.get("accountId"))
                deadline = time.monotonic() + self.PAYMENT_WAIT_SECONDS
                while resent and resent.get("status") not in ("ACSC", "RJCT") \
                        and time.monotonic() < deadline:
                    world.enquire_payment_status(self.ops)
                    self.settle_world()
                    resent = ledger.payment_by_sid(resent["sid"]) or resent
                    time.sleep(3)
            self.settle_world()
        finally:
            if refusing:
                faults.set_bank_payment_status(normal)
        after = ledger.group_of_payment(payment["sid"]) or {}
        violation = None
        if refusing:
            # The refused resend puts the group back at PENDING_APPROVAL, so the check is that a
            # resend happened and the bank refused it, not that the status moved.
            if call.ok and not (resent and resent.get("status") == "RJCT"):
                violation = oracles.Violation(
                    "an approved payment is sent again", group["uid"],
                    "APPROVE while the bank refuses left no refused resend",
                    expected="a resend at RJCT", actual=(resent or {}).get("status"))
        elif call.ok:
            violation = oracles.operator_decision_takes_effect(
                group["uid"], decision, group["status"], after.get("status"))
        print("  -- operator {} on group {}: {} -> {}, resend {}".format(
            decision, group["uid"][:8], group["status"], after.get("status"),
            (resent or {}).get("status")))
        return {
            "decision": decision,
            "decisionStatus": call.status,
            "group": group["uid"],
            "groupBefore": group["status"],
            "groupAfter": after.get("status"),
            "resent": resent,
            "violation": violation,
        }

    def return_payment(self, payment):
        """Credit the money back to the account it left, the way the bank books a reversal.

        The simulator books "REVERSAL OF" and sets `reversal_indicator` when the end-to-end id
        holds "reverse", and `Payments.reverseDebitOnHsbcAccount` in the acceptance corpus credits
        `reverse-<end-to-end id>` to the real account the payment came from.
        """
        source = json.loads(payment.get("from_account_identifier") or "{}").get("value")
        if not source:
            return None
        call = world.credit_platform_at_bank(
            self.hsb, source, payment["amount"], "reverse-" + payment["end_to_end_id"])
        call.own_wire = True
        if call.ok:
            self.credits_since_poll += 1
        return call

    # How long the run waits for core to settle a closure payout. In cycle 23 a returned closure
    # read CLOSED holding 3.00 straight after the sweep, and 0.00 a few minutes later.
    BALANCE_WAIT_SECONDS = 60

    def settled_account_reading(self, seconds=None):
        """Read the account until it holds nothing or the wait runs out, and answer the last read.

        A refused payout never brings the balance to zero, so that case costs the whole wait.
        """
        deadline = time.monotonic() + (seconds or self.BALANCE_WAIT_SECONDS)
        while True:
            account = self.read("account") or {}
            balance = account.get("balance")
            try:
                empty = balance is not None and Decimal(str(balance)) == 0
            except ArithmeticError:
                empty = False
            if empty or time.monotonic() >= deadline:
                return account
            time.sleep(3)
            self.settle_world()

    # How long the run waits for clearing to send a closure payment. Clearing sent one 12 seconds
    # after the closure sweep raised the withdrawal, so this leaves room for a slow stack.
    PAYMENT_WAIT_SECONDS = 90

    def wait_for_payment_after(self, sid, account_id=None):
        """Settle until clearing has sent the payment that follows `sid`, and answer it or None.

        Reading straight after the closure sweep found no payment and a CLOSED account still
        holding its balance, and both were reported as money left behind. Given an account, only
        a payment of that account's dues counts, because the sweep closes other accounts too.
        """
        clearing_account = ledger.clearing_account_of(account_id) if account_id else None
        deadline = time.monotonic() + self.PAYMENT_WAIT_SECONDS
        while True:
            payment = (ledger.payment_for_account_after(sid, clearing_account) if clearing_account
                       else ledger.payment_after(sid))
            if payment or time.monotonic() >= deadline:
                return payment
            time.sleep(3)
            self.settle_world()

    def return_closure_payment(self):
        """Close a funded account, let the payment leave, then send the money back from the bank.

        A returned payment arrives as a credit into the platform's pooled account carrying the
        closed account's reference. Every Direct platform is POOLED, so the customer holds no
        account at the bank for the money to come back to, and what clearing does with a credit
        for an account it has already closed is the question.
        """
        # One fault at a time across the whole stack. Without this the injection took over the
        # record of which fault was live, and a 500 caused by a cut wire was then reported as an
        # unexplained server error on the closure. An attempt refused here costs no allowance,
        # because the wire the other fault sits on says nothing about this injection.
        live = self.live_fault()
        if live:
            return Call("POST", "return-closure-payment", 412,
                        {"message": "{} is already injected".format(live)}, 0)
        self.closure_attempts += 1
        self.closure_forced_at = self.steps
        held, refused = self.closable_subject()
        if not held:
            return Call("POST", "return-closure-payment", 412, {"message": refused}, 0)
        # Read the highest payment clearing holds first. The payment raised after it is the
        # closure payment, and its end-to-end identifier is the reference a returning bank quotes.
        before = ledger.newest_payment_sid()
        close = self.client.call(
            "POST", "/direct/v1/customers/{customerId}/accounts/{accountId}/close"
                    "?reason=NO_LONGER_NEEDED".format(**held))
        if not close.ok:
            return close
        self.note_in_flight("closure payment", "a closure payment the bank returns",
                            held["accountId"])
        self.finalise_the_closure(held["accountId"])
        payment = self.wait_for_payment_after(before, held["accountId"])
        if not payment:
            return self.record_closure_injection(
                "returned", held, note="closing raised no payment, so nothing could be returned")
        # Let the payout settle first, so the return arrives for money that has left. A payment
        # clearing created but never sent was returned in fleet 190, so the bank credited a
        # reversal of a payment it never received; return only what the bank has paid.
        self.settle_world()
        if not ledger.bank_paid(payment.get("end_to_end_id")):
            return self.record_closure_injection(
                "returned", held, payment=payment,
                note="the bank has no record of the closure payment, so nothing could be returned")
        self.return_payment(payment)
        self.settle_world()
        return self.record_closure_injection("returned", held, payment=payment)

    # How many times the run advances the business day to drain an account's interest schedule.
    # `fetchClosingAccountsReadyForFinalisation` takes an INSTANT account only once both its
    # accrual and its realisation dates are null, so a close alone raises no payment and the
    # account sits at CLOSING holding its money.
    DAYS_TO_DRAIN_THE_SCHEDULE = 2

    def finalise_the_closure(self, account_uid=None):
        """Drive a closing account as far towards CLOSED as the harness can.

        Closing alone leaves nothing for the bank to refuse, because the withdrawal is raised by
        the closure sweep and the sweep passes over an account whose interest schedule still has
        dates on it. A NOTICE closure pays out through its full notice withdrawal instead, and
        without bringing that due the account sat at CLOSING with no payment, which the closure
        oracle reported as a stuck closure (finding 12).
        """
        self.settle_world()
        for _ in range(self.DAYS_TO_DRAIN_THE_SCHEDULE):
            self.advance_business_day()
        if account_uid and world.bring_notice_due(account_uid):
            world.process_due_notice(self.ops)
            self.settle_world()
        self.process_closures()
        self.settle_world()

    def record_closure_injection(self, case, held, payment=None, note=None):
        """Read back what the injection left, and keep watching whether the account ever closes."""
        account = self.settled_account_reading()
        observation = {
            "case": case,
            "trial": self.steps,
            "customerId": held.get("customerId"),
            "accountId": held.get("accountId"),
            "accountReference": held.get("accountReference"),
            "statusAfter": account.get("status"),
            "balanceAfter": account.get("balance"),
            "sweepsAtInjection": self.sweeps,
            "payment": payment,
            "note": note,
        }
        self.closure_injections.append(observation)
        stranded = oracles.closed_account_is_empty(
            held.get("accountId"), account.get("status"), account.get("balance"), case)
        if stranded is not None:
            row = stranded.as_row()
            row["n"] = len(self.log) + 1
            row["action"] = self.INJECTION_BY_CASE[case]
            row["body"] = observation
            row["inFlight"] = self.in_flight_now()
            row["attribution"] = "a closure payment the bank {}".format(case)
            row["leadUp"] = [
                {"n": t["n"], "action": t["action"], "status": t["status"],
                 "message": t.get("message")}
                for t in self.log[-8:]]
            self.record_violation(row, stranded)
        # Keep the account under watch rather than judging it now. An INSTANT account waits for its
        # interest schedule to drain and a NOTICE account waits for a zero balance, so an account
        # still at CLOSING one sweep after the injection is the ordinary wait.
        self.closing_watch.append(dict(observation, sweepsSeen=0))
        del self.closing_watch[:-5]
        print("  -- closure payment {}: account {} reads {} holding {}".format(
            case, (held.get("accountId") or "?")[:8], observation["statusAfter"],
            observation["balanceAfter"]))
        return Call("POST", "closure-payment-" + case, 200, observation, 0)

    def check_watched_closures(self):
        """Ask of each injected closure whether the account ever reached CLOSED.

        An account that stays at CLOSING for ever makes the SAV-11580 refusal permanent rather
        than a wait, because the uniqueness trigger counts a CLOSING account and nothing else
        moves that status.
        """
        if not self.closing_watch:
            return
        still_waiting = []
        for watched in self.closing_watch:
            watched["sweepsSeen"] += 1
            call = self.client.call(
                "GET", "/direct/v1/customers/{}/accounts/{}".format(
                    watched["customerId"], watched["accountId"]))
            status = call.body.get("status") if call.ok and isinstance(call.body, dict) else None
            watched["statusNow"] = status
            if status in ("CLOSED", "CANCELLED") or status is None:
                continue
            if watched["sweepsSeen"] < self.CLOSURE_SWEEPS_BEFORE_CLOSED_IS_OWED:
                still_waiting.append(watched)
                continue
            violation = oracles.closure_finishes(
                watched["accountId"], status, watched["sweepsSeen"], watched["case"])
            if violation is None:
                continue
            row = violation.as_row()
            row["n"] = len(self.log) + 1
            row["action"] = "ProcessClosures"
            row["body"] = watched
            row["inFlight"] = self.in_flight_now()
            row["attribution"] = "a closure payment the bank {}".format(watched["case"])
            row["leadUp"] = [
                {"n": t["n"], "action": t["action"], "status": t["status"],
                 "message": t.get("message")}
                for t in self.log[-8:]]
            self.record_violation(row, violation)
        self.closing_watch = still_waiting

    def one_line_batch(self):
        """A single-customer deposit batch for the subject the run is standing on."""
        return {
            "batchReference": self.mint("batch", 36),
            "paymentReference": self.mint("p", 16),
            "totalPaymentRequired": "3.00",
            "allocations": [{
                "customerId": self.held["customerId"],
                "accountReference": self.held["accountReference"],
                "instructionReference": self.mint("i", 36),
                "instructionType": "DEPOSIT",
                "productId": self.held["productId"],
                "amount": "3.00",
            }],
        }

    def batch_members(self, limit=5):
        """Customers with an account ready to take a deposit, newest last."""
        ready = []
        for i, subject in enumerate(self.subjects):
            if subject.get("closed") or not subject.get("customerId"):
                continue
            if not subject.get("accountReference"):
                continue
            ready.append((i, subject))
        return ready[:limit]

    LINE_AMOUNT = Decimal("3.00")

    @property
    def DRIVEN(self):
        return {
            "PlaceBatchPayment": type(self).place_batch,
            "PayBatchPart": type(self).pay_batch_part,
            "SettleWorld": type(self).settle_world,
            "CancelAllocation": type(self).cancel_allocation,
            "FundAccount": type(self).fund_account,
            "FundAccountInterrupted": type(self).fund_account_interrupted,
            "AdvanceBusinessDay": type(self).advance_business_day,
            "ProcessClosures": type(self).process_closures,
            "NoticeFallsDue": type(self).notice_falls_due,
            "ProcessDueNotice": type(self).process_due_notice,
            "CloseNoticeAfterDue": type(self).close_notice_after_due,
            "SetKycStatus": type(self).set_kyc_status,
            "CreateKycFailedCustomer": type(self).create_kyc_failed_customer,
            "UpdateCustomer": type(self).update_customer,

            "FreezeCustomer": type(self).freeze_customer,
            "UnfreezeCustomer": type(self).unfreeze_customer,
            "SlowTheBank": type(self).slow_the_bank,
            "BreakTheBank": type(self).break_the_bank,
            "SlowClearing": type(self).slow_clearing,
            "BreakClearing": type(self).break_clearing,
            "SlowBankForClearing": type(self).slow_bank_for_clearing,
            "BreakBankForClearing": type(self).break_bank_for_clearing,
            "HealTheNetwork": type(self).heal_the_network,
            "RestartClearing": type(self).restart_clearing,
            "RestartCore": type(self).restart_core,
            "RestartBank": type(self).restart_bank,
            "ReplayLastCall": type(self).replay_last_call,
            "FundAccountDuplicated": type(self).fund_account_duplicated,
            "RejectClosurePayment": type(self).reject_closure_payment,
            "ReturnClosurePayment": type(self).return_closure_payment,
            "DuplicateMessages": type(self).duplicate_messages,
            "StopDuplicating": type(self).stop_duplicating,
            "ProbeOtherPlatform": type(self).probe_other_platform,
            "RunDataFeed": type(self).run_data_feed,
            "WeirdCall": type(self).weird_call,
            "ChangePlatformFee": type(self).change_platform_fee,
            "ChangeBankRate": type(self).change_bank_rate,
            "TransferToProduct": type(self).transfer_to_product,
            "SetMaturityToNotice": type(self).set_maturity_to_notice,
            "OfficerCloseCustomer": type(self).officer_close_customer,
            "OfficerRejectCustomer": type(self).officer_reject_customer,
            "OfficerCancelCustomer": type(self).officer_cancel_customer,
            "AdjustNoticeWithdrawals": type(self).adjust_notice_withdrawals,
            "ReemitFeedEntity": type(self).reemit_feed_entity,
            **{name: (lambda run, name=name: run.run_journey(name)) for name in journeys.ALL},
        }

    # How many trials apart the driver forces a journey, and how many trials it waits first. A
    # journey takes a minute or two, so forcing one every trial would leave no exploration.
    JOURNEY_SPACING = int(os.environ.get("SIM_JOURNEY_SPACING", "20"))
    JOURNEY_FIRST = int(os.environ.get("SIM_JOURNEY_FIRST", "10"))
    # The trial spacing alone let journeys take 87% of fleet 193's member time (28 journeys in 147
    # trials), because steps outrun trials and least-tried picking also chooses a journey. A share
    # of the run's wall time caps them whatever picks them.
    JOURNEY_SHARE = float(os.environ.get("SIM_JOURNEY_SHARE", "0.3"))

    def journey_over_share(self):
        began = getattr(self, "journey_clock", None)
        if began is None:
            self.journey_clock = began = time.monotonic()
        spent = getattr(self, "journey_seconds", 0.0)
        return spent > self.JOURNEY_SHARE * (time.monotonic() - began)

    def journey_due(self):
        """The next journey in turn once JOURNEY_SPACING trials have passed since the last one."""
        if (not journeys.ALL or self.steps < self.JOURNEY_FIRST or self.live_fault()
                or self.journey_over_share()):
            return None
        last = getattr(self, "journey_at", None)
        if last is not None and self.steps - last < self.JOURNEY_SPACING:
            return None
        names = list(journeys.ALL)
        name = names[getattr(self, "journey_turn", 0) % len(names)]
        return name if self.someone_can(name) else None

    def run_journey(self, name):
        """Run one journey, unless another ran fewer than JOURNEY_SPACING trials ago."""
        last = getattr(self, "journey_at", None)
        if last is not None and self.steps - last < self.JOURNEY_SPACING:
            return Call("POST", name, 412, {
                "message": "a journey ran {} trials ago".format(self.steps - last)}, 0)
        if self.journey_over_share():
            return Call("POST", name, 412, {
                "message": "journeys have used more than {:.0%} of the run".format(
                    self.JOURNEY_SHARE)}, 0)
        self.journey_at = self.steps
        names = list(journeys.ALL)
        if names[getattr(self, "journey_turn", 0) % len(names)] == name:
            self.journey_turn = getattr(self, "journey_turn", 0) + 1
        print("  -- journey {} starts".format(name))
        began = time.monotonic()
        try:
            call = journeys.ALL[name](self)
        finally:
            self.journey_seconds = getattr(self, "journey_seconds", 0.0) + time.monotonic() - began
        print("  -- journey {} ends: {}".format(name, (call.body or {}).get("message")))
        return call

    def long_life_tick(self):
        """The long-lived bank's clock and this platform's population, when the cycle reuses one."""
        long = getattr(self, "long", None)
        if long is not None:
            long.tick()

    INTEREST_SWEEP_SECONDS = float(os.environ.get("SIM_INTEREST_SWEEP_SECONDS", "180"))
    INTEREST_API_ACCOUNTS = 3

    def interest_sweep(self, final=False):
        """Recompute this platform's accruals and realisations and record what disagrees.

        On a timer it judges only the accrual rows written since the last sweep; the final sweep
        judges every row written since the run began. Every run does this for its own platform.
        """
        if not self.platform_uid:
            return 0
        now = time.time()
        if not final and now - getattr(self, "interest_swept_at", 0.0) < self.INTEREST_SWEEP_SECONDS:
            return 0
        self.interest_swept_at = now
        long = getattr(self, "long", None)
        if not hasattr(self, "interest_since"):
            self.interest_since = self.interest_from = long.oracle_since() if long else 0
        since = self.interest_from if final else self.interest_since
        try:
            found, stats, newest = interest_oracle.check(platform_uid=self.platform_uid,
                                                         since_sid=since)
        except Exception as fault:  # noqa: BLE001 - a sweep that fails must not end the run
            print("  -- the interest oracle raised {}: {}".format(type(fault).__name__, fault))
            return 0
        if stats.get("error"):
            print("  -- the interest oracle could not read: {}".format(stats["error"]))
            return 0
        self.interest_since = max(self.interest_since, newest)
        if long:
            long.remember_oracle(self.interest_since)
        for finding in found:
            self.note_violation("InterestOracle", finding["rule"], finding["subject"],
                                finding["detail"], finding["expected"], finding["actual"])
        stats["apiChecked"], stats["apiDisagreed"] = self.interest_against_api()
        stats["final"] = final
        self.interest_stats = stats
        print("  -- interest oracle{}: {}".format(" (final)" if final else "",
                                                  json.dumps(stats, sort_keys=True, default=str)))
        return len(found)

    def interest_against_api(self):
        """The Direct API's INTEREST rows against the interest core realised, for a few accounts."""
        totals = interest_oracle.realised_totals(self.platform_uid, self.INTEREST_API_ACCOUNTS)
        checked, disagreed = 0, 0
        for account_id, (customer_id, realised) in (totals or {}).items():
            # The clock keeper realises a day every few seconds, so core is read on both sides of
            # the API read and the API may equal anything in between. Fleet 199 read 2.40 from the
            # API against 2.39 in core one realisation earlier; both held 3.47 when read together.
            listed, before, after = None, realised, realised
            for _ in range(3):
                before = (interest_oracle.realised_totals(self.platform_uid, 500) or {}).get(
                    account_id, (None, before))[1]
                rows = self.account_transactions(customer_id, account_id)
                if rows is None:
                    listed = None
                    break
                listed = sum((Decimal(str(r.get("amount") or "0")) for r in rows
                              if r.get("type") == "INTEREST"), Decimal("0"))
                after = (interest_oracle.realised_totals(self.platform_uid, 500) or {}).get(
                    account_id, (None, after))[1]
                if before <= listed <= after:
                    break
                time.sleep(self.CONFIRM_PAUSE_SECONDS)
            if listed is None:
                continue
            checked += 1
            realised = after
            if not before <= listed <= after:
                disagreed += 1
                self.note_violation(
                    "InterestOracle", "the INTEREST rows the API lists are the interest core realised",
                    account_id, "the Direct API lists {} of INTEREST on account {} and core's "
                    "interest_realised holds {}".format(listed, account_id, realised),
                    expected=realised, actual=listed)
        return checked, disagreed

    def note_violation(self, action_name, rule, subject, detail, expected, actual, body=None):
        """Record a finding made by one of the driven actions' own oracles."""
        violation = oracles.Violation(rule, subject, detail, expected=expected, actual=actual)
        row = violation.as_row()
        row["n"] = len(self.log) + 1
        row["action"] = action_name
        row["body"] = body if isinstance(body, (dict, list)) or body is None else str(body)
        row["inFlight"] = self.in_flight_now()
        row["attribution"] = "{} alone".format(action_name) if not row["inFlight"] else (
            "the run cannot attribute this to {}, because {} was also in flight".format(
                action_name, ", ".join(sorted({e["what"] for e in row["inFlight"]}))))
        row["leadUp"] = [
            {"n": t["n"], "action": t["action"], "status": t["status"],
             "message": t.get("message")}
            for t in self.log[-8:]]
        self.record_violation(row, violation)

    def product_of_type(self, kind):
        return next((p for t, p in sorted(self.products.values()) if t == kind), None)

    def customer_accounts(self, customer_id):
        """Every account of the customer as the Direct API lists it, or None when it will not."""
        call = self.client.call("GET", "/direct/v1/customers/{}/accounts".format(customer_id))
        if not call.ok:
            return None
        return [row for row in _rows(call.body) if isinstance(row, dict)]

    def account_transactions(self, customer_id, account_id):
        # Every page: the long-lived bank's accounts passed 1000 rows (a daily INTEREST row each),
        # and fleet 209 compared the first page's interest with all of core's.
        rows, skip = [], 0
        while True:
            call = self.client.call(
                "GET", "/direct/v1/customers/{}/accounts/{}/transactions?skip={}&take={}".format(
                    customer_id, account_id, skip, self.TRANSACTION_PAGE))
            if not call.ok:
                return None
            page = _rows(call.body)
            rows.extend(page)
            if len(page) < self.TRANSACTION_PAGE:
                return rows
            skip += len(page)

    def money_snapshot(self, customer_id):
        """{accountId: (status, balance, {transactionId: (type, amount)})} for every account."""
        accounts = self.customer_accounts(customer_id)
        if accounts is None:
            return None
        snapshot = {}
        for account in accounts:
            rows = self.account_transactions(customer_id, account.get("accountId"))
            if rows is None:
                return None
            snapshot[account.get("accountId")] = (
                account.get("status"), self.balance_of(account),
                {r.get("transactionId"): (r.get("type"), Decimal(str(r.get("amount") or "0")))
                 for r in rows})
        return snapshot

    TRANSFER_AMOUNT = Decimal("1.00")

    def transfer_to_product(self):
        """Move money from the subject's account into the other non-TERM product, then settle.

        The transfer books both legs in core at once, so across the customer's accounts the total
        is the same before and after, apart from interest that lands in between. The settle sweep
        runs first so the reading is taken after clearing has moved the money as well.
        """
        held = self.held
        kind = held.get("productType") or "INSTANT"
        if kind == "TERM":
            return Call("POST", "TransferToProduct", 412,
                        {"message": "a TERM account is not a transfer source the run tries"}, 0)
        destination = self.product_of_type("NOTICE" if kind == "INSTANT" else "INSTANT")
        if not destination:
            return Call("POST", "TransferToProduct", 412,
                        {"message": "the platform offers no product to transfer into"}, 0)
        account = self.read("account") or {}
        if account.get("status") != "OPEN" or self.balance_of(account) < self.TRANSFER_AMOUNT:
            return Call("POST", "TransferToProduct", 412, {
                "message": "the source account reads {} holding {}, and a transfer needs an OPEN "
                           "account holding {}".format(account.get("status"),
                                                       account.get("balance"),
                                                       self.TRANSFER_AMOUNT)}, 0)
        if self.pending_instructions(remember=False):
            # A deposit or withdrawal the sweep could complete would move the total by itself.
            return Call("POST", "TransferToProduct", 412, {
                "message": "the customer has an instruction in flight, so its total cannot be "
                           "compared across the transfer"}, 0)
        customer = held["customerId"]
        before = self.money_snapshot(customer)
        if before is None:
            return Call("POST", "TransferToProduct", 412,
                        {"message": "could not read the customer's accounts before the transfer"}, 0)
        reference = self.mint("t", 36)
        call = self.client.call(
            "POST", "/direct/v1/customers/{customerId}/accounts/{accountId}/instruction"
                    .format(**held),
            json_body={"instructionRequestType": "TRANSFER", "productId": destination,
                       "amount": "{:.2f}".format(self.TRANSFER_AMOUNT),
                       "instructionReference": reference,
                       "termsAndConditionsAcceptedAt": actions.past_instant()})
        if not call.ok:
            return call
        self.settle_world()
        after = self.money_snapshot(customer)
        if after is None:
            return Call("POST", "TransferToProduct", call.status, {
                "message": "transferred {} into {}, and the accounts could not be read after".format(
                    self.TRANSFER_AMOUNT, destination[:8]), "instruction": call.body},
                call.elapsed_ms)
        total_before = sum((b for _, b, _ in before.values()), Decimal("0"))
        total_after = sum((b for _, b, _ in after.values()), Decimal("0"))
        interest = Decimal("0")
        for account_id, (_, _, rows) in after.items():
            seen = before.get(account_id, (None, None, {}))[2]
            interest += sum((amount for tid, (kind_, amount) in rows.items()
                             if tid not in seen and kind_ == "INTEREST"), Decimal("0"))
        if total_after.compare(total_before + interest) != 0:
            self.note_violation(
                "TransferToProduct", "a transfer between a customer's accounts keeps the total",
                customer, "the customer's accounts summed {} before a transfer of {} and {} after "
                          "settling, with {} of new interest".format(
                              total_before, self.TRANSFER_AMOUNT, total_after, interest),
                expected=total_before + interest, actual=total_after,
                body={"before": {k: str(v[1]) for k, v in before.items()},
                      "after": {k: str(v[1]) for k, v in after.items()},
                      "instruction": call.body})
        negative = {k: str(v[1]) for k, v in after.items() if v[1] < 0}
        if negative:
            self.note_violation(
                "TransferToProduct", "no account holds a negative balance", customer,
                "after a transfer of {} the customer holds a negative balance".format(
                    self.TRANSFER_AMOUNT), expected="every balance >= 0", actual=negative)
        return Call("POST", "TransferToProduct", call.status, {
            "message": "transferred {} from {} to {}: total {} before, {} after".format(
                self.TRANSFER_AMOUNT, kind, destination[:8], total_before, total_after),
            "instruction": call.body}, call.elapsed_ms)

    TERM_OPENING_AMOUNT = Decimal("50.00")
    TERM_FUNDING_SWEEPS = 6
    TERM_FUNDING_PAUSE_SECONDS = 3

    def fund_with(self, amount):
        """FundAccount's sequence for an amount of the run's choosing, on the held account."""
        held = self.held
        batch = self.client.call("POST", "/direct/v1/batches", json_body={
            "batchReference": self.mint("batch", 36),
            "paymentReference": self.mint("p", 16),
            "totalPaymentRequired": "{:.2f}".format(amount),
            "allocations": [{
                "customerId": held["customerId"],
                "accountReference": held["accountReference"],
                "instructionReference": self.mint("i", 36),
                "instructionType": "DEPOSIT",
                "productId": held["productId"],
                "amount": "{:.2f}".format(amount),
            }],
        })
        if not batch.ok:
            return batch
        steps = [
            world.raise_platform_dues(self.ops, self.platform_uid),
            self.credit_and_count("{:.2f}".format(amount), batch.body["paymentReference"]),
            self.poll_if_new_money(),
            world.drain_transactions(self.ops),
            world.settle_payments(self.ops),
            world.drain_transactions(self.ops),
        ]
        self.sweeps += 1
        return _first_failure(steps) or batch

    def set_maturity_to_notice(self):
        """Point a funded TERM account's maturity at the customer's NOTICE account.

        DirectMaturityDestinationService needs the account funded (a maturity date), the date at
        least a day away, no pending instruction, and an account of the customer's on the
        destination product that is not CANCELLED, CLOSING or CLOSED. The run opens that NOTICE
        account when the customer has none, and funds the TERM account to its opening amount when
        it is still REQUESTED.
        """
        held = self.held
        if (held.get("productType") or "INSTANT") != "TERM":
            return Call("POST", "SetMaturityToNotice", 412,
                        {"message": "only a TERM account has a maturity destination"}, 0)
        notice = self.product_of_type("NOTICE")
        if not notice:
            return Call("POST", "SetMaturityToNotice", 412,
                        {"message": "the platform offers no NOTICE product"}, 0)
        if not held.get("accountReference"):
            return Call("POST", "SetMaturityToNotice", 412,
                        {"message": "the subject holds no account reference to fund"}, 0)
        if self.pending_instructions(account_id=held["accountId"], remember=False):
            return Call("POST", "SetMaturityToNotice", 412,
                        {"message": "the TERM account has an instruction in flight"}, 0)
        account = self.read("account") or {}
        if account.get("status") == "REQUESTED":
            owed = self.TERM_OPENING_AMOUNT - self.balance_of(account)
            if owed > 0:
                self.fund_with(owed)
                self._pending_cache = {}
                account = self.read("account") or {}
                # The deposit lands on the account a sweep or two after the credit is read.
                for _ in range(self.TERM_FUNDING_SWEEPS):
                    if account.get("status") != "REQUESTED":
                        break
                    time.sleep(self.TERM_FUNDING_PAUSE_SECONDS)
                    self.settle_world()
                    account = self.read("account") or {}
        if account.get("status") != "OPEN" or not account.get("maturityDate"):
            return Call("POST", "SetMaturityToNotice", 412, {
                "message": "the TERM account reads {} with maturity date {}, so it is not "
                           "funded".format(account.get("status"), account.get("maturityDate"))}, 0)
        today = datetime.now(world.LONDON).date()
        if date.fromisoformat(str(account["maturityDate"])[:10]) <= today:
            return Call("POST", "SetMaturityToNotice", 412, {
                "message": "the account matures on {}, less than a day away".format(
                    account["maturityDate"])}, 0)
        customer = held["customerId"]
        accounts = self.customer_accounts(customer)
        if accounts is None:
            return Call("POST", "SetMaturityToNotice", 412,
                        {"message": "could not list the customer's accounts"}, 0)
        target = next((a for a in accounts
                       if (a.get("product") or {}).get("productId") == notice
                       and a.get("status") not in ("CANCELLED", "CLOSING", "CLOSED")), None)
        if target is None:
            # Not absorbed: the subject stays on its TERM product, and the NOTICE account is only
            # the destination.
            opened = self.client.call(
                "POST", "/direct/v1/customers/{}/accounts".format(customer),
                json_body={"productId": notice, "accountReference": self.mint("acct", 18),
                           "termsAndConditionsAcceptedAt": actions.past_instant()})
            if not opened.ok or not isinstance(opened.body, dict):
                return opened
            target = opened.body
        call = self.client.call(
            "POST", "/direct/v1/customers/{customerId}/accounts/{accountId}/maturityDestination"
                    .format(**held), json_body={"productId": notice})
        if not call.ok:
            return call
        read = self.read("account") or {}
        if read.get("maturityDestination") != target.get("accountId"):
            self.note_violation(
                "SetMaturityToNotice", "an accepted maturity destination reads back", customer,
                "the maturity destination was accepted and the account read shows {}".format(
                    read.get("maturityDestination")),
                expected=target.get("accountId"), actual=read.get("maturityDestination"),
                body=call.body)
        return Call("POST", "SetMaturityToNotice", call.status, {
            "message": "TERM account maturing {} now matures into NOTICE account {}".format(
                account.get("maturityDate"), (read.get("maturityDestination") or "?")[:8])},
            call.elapsed_ms)

    OFFICER_WAIT_SECONDS = 30
    OFFICER_MIN_AGE_SECONDS = 60
    # The statuses from which compliance-api refuses each action outright, and CLOSED, which
    # every action leaves alone.
    OFFICER_REFUSES = {"CLOSE": {"FROZEN", "CANCELLED", "CLOSED"},
                       "REJECT": {"CLOSED"},
                       "CANCEL": {"FROZEN", "CLOSED"}}
    OFFICER_LANDS = {"CLOSE": "CLOSED", "REJECT": "DEACTIVATED", "CANCEL": "CANCELLED"}

    def officer_close_customer(self):
        return self.officer_override("OfficerCloseCustomer", "CLOSE")

    def officer_reject_customer(self):
        return self.officer_override("OfficerRejectCustomer", "REJECT")

    def officer_cancel_customer(self):
        return self.officer_override("OfficerCancelCustomer", "CANCEL")

    def officer_override(self, action_name, action):
        """An officer's CLOSE, REJECT or CANCEL through compliance-api, and the status it reaches.

        CLOSE goes to core synchronously; REJECT and CANCEL on a customer that is not FROZEN go
        to compliance, which tells core through its queue, so the Direct read is polled. The
        customer leaves the pool afterwards: it is closed or stopped for good.
        """
        held = self.held
        if not self.compliance or not held.get("customerId"):
            return Call("PUT", action_name, 412,
                        {"message": "no compliance client, or no customer to act on"}, 0)
        customer = held["customerId"]
        read = self.client.call("GET", "/direct/v1/customers/{}".format(customer))
        status = read.body.get("customerStatus") if read.ok and isinstance(read.body, dict) \
            else None
        if status is None or status in self.OFFICER_REFUSES[action]:
            return Call("PUT", action_name, 412, {
                "message": "the customer reads {}, which {} is not tried from".format(
                    status, action)}, 0)
        created = str(read.body.get("createdAt") or "")
        try:
            age = time.time() - datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
        except ValueError:
            age = None
        if age is not None and age < self.OFFICER_MIN_AGE_SECONDS:
            # The onboarding KYC flow finishes after the create answers, and its result writes
            # over an override that landed first.
            return Call("PUT", action_name, 412, {
                "message": "the customer is {:.0f}s old and its onboarding check may still be "
                           "running".format(age)}, 0)
        window = self.ASYNC_WINDOW_TRIALS["compliance"]
        if any(e["kind"] == "compliance" and e["subject"] == customer
               and 0 <= self.steps - e["trial"] <= window for e in self.in_flight):
            # A KYC result still on its way lands after the override and writes over it.
            return Call("PUT", action_name, 412,
                        {"message": "a compliance result for this customer is still in flight"}, 0)
        if action == "CLOSE":
            if self.pending_instructions(remember=False):
                # Closing drains each account, and cash already moving would reach finding 15.
                return Call("PUT", action_name, 412,
                            {"message": "the customer has an instruction in flight"}, 0)
            holding = sum((self.balance_of(a) for a in self.customer_accounts(customer) or []),
                          Decimal("0"))
            if holding:
                # Core refuses it: "Unable to close customer while accounts hold a non-zero
                # balance".
                return Call("PUT", action_name, 412, {
                    "message": "the customer's accounts hold {}".format(holding)}, 0)
        call = world.override_customer_status(
            self.compliance, customer, action, "simulation explorer {}".format(action.lower()))
        if not getattr(call, "ok", False):
            return call
        self.note_in_flight("compliance", "an officer {} of {}".format(action, status), customer)
        expected = self.OFFICER_LANDS[action]
        deadline = time.monotonic() + self.OFFICER_WAIT_SECONDS
        while True:
            read = self.client.call("GET", "/direct/v1/customers/{}".format(customer))
            landed = read.body.get("customerStatus") if read.ok and isinstance(read.body, dict) \
                else None
            if landed == expected or time.monotonic() >= deadline:
                break
            time.sleep(2)
        if landed != expected:
            self.note_violation(
                action_name, "an accepted officer override reaches the Direct customer", customer,
                "compliance accepted {} on a {} customer and the Direct read shows {} after "
                "{}s".format(action, status, landed, self.OFFICER_WAIT_SECONDS),
                expected=expected, actual=landed, body=read.body)
        if action == "CLOSE":
            accounts = self.customer_accounts(customer) or []
            open_left = {a.get("accountId"): a.get("status") for a in accounts
                         if a.get("status") not in ("CLOSING", "CLOSED", "CANCELLED")}
            if open_left:
                self.note_violation(
                    action_name, "a closed customer holds no live account", customer,
                    "an officer closed the customer and {} of its accounts are still live".format(
                        len(open_left)),
                    expected="every account CLOSING, CLOSED or CANCELLED", actual=open_left)
        self.subjects[self.current]["closed"] = True
        return Call("PUT", action_name, call.status,
                    read.body if isinstance(read.body, dict) else {"message": str(read.body)},
                    call.elapsed_ms + read.elapsed_ms)

    ADJUST_PATH = "/operations/processor/order/notice/adjust"
    STACK_CALL_EVERY_TRIALS = 25
    NOTICE_SUBJECTS_CHECKED = 4

    def spaced(self, attribute):
        """True when this stack-wide call last ran long enough ago, and marks it as running now."""
        last = getattr(self, attribute, None)
        if last is not None and self.steps - last < self.STACK_CALL_EVERY_TRIALS:
            return False
        setattr(self, attribute, self.steps)
        return True

    def adjust_notice_withdrawals(self):
        """Run the notice withdrawal adjustment, then read every NOTICE subject's pending ones.

        Forced straight after an accepted ChangeBankRate by shape_gap, and otherwise spaced out,
        because it reads every incomplete notice withdrawal on the stack.
        """
        after_rate = bool(self.log) and self.log[-1]["action"] == "ChangeBankRate" \
            and self.log[-1]["ok"]
        if not after_rate and not self.spaced("adjusted_at"):
            return Call("POST", "AdjustNoticeWithdrawals", 412, {
                "message": "the adjustment ran fewer than {} trials ago".format(
                    self.STACK_CALL_EVERY_TRIALS)}, 0)
        self.adjusted_at = self.steps
        call = self.ops.call("POST", self.ADJUST_PATH)
        if not call.ok:
            return call
        self.note_in_flight("notice", "a notice withdrawal adjustment")
        # The service submits each account's adjustment to an executor and answers at once.
        time.sleep(3)
        checked, over = 0, []
        saved = self.current
        try:
            for i, subject in enumerate(self.subjects):
                if checked >= self.NOTICE_SUBJECTS_CHECKED:
                    break
                if subject.get("closed") or not subject.get("customerId"):
                    continue
                if (subject.get("productType") or "INSTANT") != "NOTICE":
                    continue
                checked += 1
                lines = self.client.call(
                    "GET", "/direct/v1/customers/{}/instructions".format(subject["customerId"]))
                waiting = [l for l in (_rows(lines.body) if lines.ok else [])
                           if l.get("status") == "PENDING"
                           and (l.get("type") or l.get("instructionType")) == "WITHDRAWAL"]
                for line in waiting:
                    account = self.client.call("GET", "/direct/v1/customers/{}/accounts/{}".format(
                        subject["customerId"], line.get("accountId")))
                    if not account.ok or not isinstance(account.body, dict):
                        continue
                    balance = self.balance_of(account.body)
                    amount = Decimal(str(line.get("amount") or "0"))
                    if amount > balance:
                        over.append({"instruction": line.get("instructionId"),
                                     "account": line.get("accountId"),
                                     "amount": str(amount), "balance": str(balance)})
        finally:
            self.current = saved
        for entry in over:
            self.note_violation(
                "AdjustNoticeWithdrawals",
                "no pending notice withdrawal asks for more than its account holds",
                entry["account"], "a pending notice withdrawal of {} on an account holding {} "
                                  "after the adjustment".format(entry["amount"], entry["balance"]),
                expected="<= {}".format(entry["balance"]), actual=entry["amount"], body=entry)
        return Call("POST", "AdjustNoticeWithdrawals", call.status, {
            "message": "adjusted{}; {} NOTICE customers read, {} withdrawals over balance".format(
                " after a rate change" if after_rate else "", checked, len(over))},
            call.elapsed_ms)

    REEMIT_PATH = "/operations/processor/direct/feed/reemit/bank/{}/{}/{}/{}"
    REEMIT_FILE_TYPES = ("CUSTOMER", "ACCOUNT", "PRODUCT")
    # UPDATE only. An INSERT re-emit of an entity the feed already sent makes the next file carry a
    # second INSERT for it, which the feed checker rightly flags: fleet 190 reported ACCOUNT
    # 2c40bb8d "appears again as INSERT" after this action asked for exactly that.
    REEMIT_MODES = ("UPDATE",)
    REEMIT_ENTRY = {
        "CUSTOMER": ("investec_file_customer_entry", "customer_sid", "platform_customer"),
        "ACCOUNT": ("investec_file_account_entry", "account_sid", "customer_product_account"),
        "PRODUCT": ("investec_file_product_entry", "product_sid", "bank_product"),
    }

    @staticmethod
    def is_uuid(value):
        try:
            uuid.UUID(str(value))
            return True
        except ValueError:
            return False

    def newest_feed_file(self, file_type):
        rows, error = integrity._psql(
            "SELECT coalesce(max(f.sid), 0) FROM investec_file f JOIN partner_bank b "
            "ON b.sid = f.bank_sid WHERE b.uid = '{}' AND f.file_type = '{}'".format(
                self.bank_uid, file_type))
        return int(rows[0][0]) if rows and not error else None

    def judge_reemits(self):
        """For each re-emit still waiting, look at the sealed files the feed has cut since."""
        waiting = []
        for entry in getattr(self, "reemits", []):
            table, column, owner = self.REEMIT_ENTRY[entry["fileType"]]
            rows, error = integrity._psql(
                "SELECT count(DISTINCT f.sid), count(e.file_sid) FROM investec_file f "
                "JOIN partner_bank b ON b.sid = f.bank_sid "
                "LEFT JOIN {table} e ON e.file_sid = f.sid AND e.{column} = "
                "(SELECT sid FROM {owner} WHERE uid = '{entity}') "
                "WHERE b.uid = '{bank}' AND f.file_type = '{kind}' AND f.sid > {after} "
                "AND f.control_total_sha256 IS NOT NULL".format(
                    table=table, column=column, owner=owner, entity=entry["entity"],
                    bank=self.bank_uid, kind=entry["fileType"], after=entry["after"]))
            if error or not rows:
                waiting.append(entry)
                continue
            files, entries = int(rows[0][0]), int(rows[0][1])
            if files == 0:
                waiting.append(entry)
            elif entries == 0:
                self.note_violation(
                    "ReemitFeedEntity", "a re-emitted entity is in the next feed file",
                    entry["entity"], "{} {} was re-emitted with {} and the {} sealed {} files cut "
                                     "since hold no row for it".format(
                                         entry["fileType"], entry["entity"], entry["mode"], files,
                                         entry["fileType"]),
                    expected="a row in the next {} file".format(entry["fileType"]),
                    actual="no row in {} files".format(files), body=entry)
            else:
                print("  -- re-emitted {} {} is in the {} file cut since".format(
                    entry["fileType"], entry["entity"][:8], entry["fileType"]))
        self.reemits = waiting[-20:]

    def reemit_feed_entity(self):
        """Ask the next Direct feed run to re-emit one of this run's customers, accounts or products.

        Judged when a later ReemitFeedEntity finds that RunDataFeed has sealed a file of that type
        since: the entity must be in one of them.
        """
        if not self.bank_uid:
            return Call("POST", "ReemitFeedEntity", 412, {"message": "BANK_UID is not set"}, 0)
        self.judge_reemits()
        if not self.spaced("reemitted_at"):
            return Call("POST", "ReemitFeedEntity", 412, {
                "message": "a re-emit was asked for fewer than {} trials ago".format(
                    self.STACK_CALL_EVERY_TRIALS)}, 0)
        turn = getattr(self, "reemit_turns", 0)
        self.reemit_turns = turn + 1
        file_type = self.REEMIT_FILE_TYPES[turn % len(self.REEMIT_FILE_TYPES)]
        mode = self.REEMIT_MODES[(turn // len(self.REEMIT_FILE_TYPES)) % len(self.REEMIT_MODES)]
        held = self.held
        if file_type == "CUSTOMER":
            entity = held.get("customerId")
        elif file_type == "ACCOUNT":
            entity = held.get("accountId")
        else:
            found = world.bank_product_for(held.get("productId") or self.product_id)
            entity = found[0] if found else None
        if not self.is_uuid(entity):
            return Call("POST", "ReemitFeedEntity", 412,
                        {"message": "no {} uid to re-emit".format(file_type)}, 0)
        after = self.newest_feed_file(file_type)
        call = self.ops.call("POST", self.REEMIT_PATH.format(self.bank_uid, file_type, entity,
                                                             mode))
        if not call.ok:
            return call
        outcome = call.body.get("outcome") if isinstance(call.body, dict) else None
        if outcome == "REEMIT_QUEUED" and after is not None:
            self.reemits = getattr(self, "reemits", []) + [
                {"fileType": file_type, "entity": entity, "mode": mode, "after": after,
                 "trial": self.steps}]
        self.note_in_flight("feed", "a feed re-emit of {} {}".format(file_type, entity[:8]))
        return Call("POST", "ReemitFeedEntity", call.status, {
            "message": "{} {} {}: {}".format(mode, file_type, entity[:8], call.body),
            "rowsAffected": (call.body or {}).get("rowsAffected")
            if isinstance(call.body, dict) else None, "outcome": outcome}, call.elapsed_ms)

    # How long a received message stays hidden from other consumers while duplication is on.
    # Zero means it comes back the instant it is received, so the consumer sees it again.
    NO_HIDING = 0
    NORMAL_HIDING = 30

    def duplicate_messages(self):
        """Make one of clearing's queues deliver every message more than once.

        The queues carry the events that move money between core and clearing, so a consumer that
        is not idempotent double-counts. Nothing here reads or writes a message: taking one off
        the queue to send it again would steal it from the consumer it was meant for.
        """
        if self.duplicating:
            return Call("POST", "duplicate-messages", 412,
                        {"message": "the {} queue is already duplicating".format(
                            self.duplicating)}, 0)
        name = sorted(faults.DUPLICATING_QUEUES)[
            self.duplications % len(faults.DUPLICATING_QUEUES)]
        self.duplications += 1
        queue = faults.DUPLICATING_QUEUES[name]
        if not faults.set_visibility_timeout(queue, self.NO_HIDING):
            return Call("POST", "duplicate-messages", 412,
                        {"message": "could not reach the {} queue on LocalStack".format(name)}, 0)
        self.duplicating = name
        self.faulted_boundary = "duplicate delivery of {}".format(name)
        self.faulted_at = self.steps
        return Call("POST", "duplicate-messages", 200, {
            "message": "the {} queue now delivers every message more than once, for the next {} "
                       "trials".format(name, self.FAULT_WINDOW_TRIALS)}, 0)

    def stop_duplicating(self):
        """Put the queue back to hiding a received message for the normal thirty seconds."""
        if not self.duplicating:
            return Call("POST", "stop-duplicating", 412,
                        {"message": "no queue is duplicating"}, 0)
        queue = faults.DUPLICATING_QUEUES[self.duplicating]
        faults.set_visibility_timeout(queue, self.NORMAL_HIDING)
        was = self.duplicating
        self.duplicating = None
        self.faulted_boundary = None
        return Call("POST", "stop-duplicating", 200,
                    {"message": "the {} queue delivers once again".format(was)}, 0)

    # A second of latency, and a connection cut after 1.2 seconds. Both are long enough to change
    # what the caller sees and short enough that a run of a few hundred trials still moves.
    LATENCY = {"latency": 1000, "jitter": 400}
    CUT = {"timeout": 1200}

    def slow_clearing(self):
        return self.network_fault(faults.CORE_TO_CLEARING, "slow", "latency", self.LATENCY,
                                  "core now reaches clearing a second late")

    def break_clearing(self):
        return self.network_fault(faults.CORE_TO_CLEARING, "cut", "timeout", self.CUT,
                                  "core's calls to clearing are cut mid-flight")

    def slow_bank_for_clearing(self):
        return self.network_fault(faults.CLEARING_TO_BANK, "slow", "latency", self.LATENCY,
                                  "clearing now reaches the bank a second late")

    def break_bank_for_clearing(self):
        return self.network_fault(faults.CLEARING_TO_BANK, "cut", "timeout", self.CUT,
                                  "clearing's calls to the bank are cut mid-flight")

    def heal_the_network(self):
        """Take every network fault off every boundary."""
        if not self.faults.ready:
            return Call("POST", "heal-the-network", 412,
                        {"message": "toxiproxy is not running, so nothing was injected"}, 0)
        self.faults.clear()
        self.faulted_boundary = None
        return Call("POST", "heal-the-network", 200,
                    {"message": "every boundary passes traffic through again"}, 0)

    # How many trials a fault stays in force before the run takes it off by itself. A fault left
    # on makes every later call fail, so the run stops exploring and the report fills with 5xx
    # that only say the fault is working. Eight trials is long enough for a settle sweep and a
    # couple of reads to cross the broken boundary.
    FAULT_WINDOW_TRIALS = 8

    def live_fault(self):
        """The fault in force right now, or None. Also takes an expired fault off."""
        if not self.faulted_boundary:
            return None
        if self.steps - self.faulted_at >= self.FAULT_WINDOW_TRIALS:
            self.faults.clear()
            if self.duplicating:
                faults.set_visibility_timeout(
                    faults.DUPLICATING_QUEUES[self.duplicating], self.NORMAL_HIDING)
                self.duplicating = None
            healed = self.faulted_boundary
            self.faulted_boundary = None
            print("  -- the injected fault on {} expired and was taken off".format(healed))
            return None
        return self.faulted_boundary

    def network_fault(self, boundary, name, kind, attributes, note):
        """Inject one network fault on one boundary, replacing whatever was in force.

        One fault at a time across the whole stack, so a finding names the fault that was live.
        Two at once could not be told apart afterwards.
        """
        if not self.faults.ready:
            return Call("POST", boundary, 412,
                        {"message": "toxiproxy is not running, so no fault can be injected"}, 0)
        if boundary not in self.faults.proxies():
            return Call("POST", boundary, 412,
                        {"message": "no proxy called {} exists, so this boundary carries no "
                                    "traffic".format(boundary)}, 0)
        self.faults.clear()
        self.faults.add(boundary, name, kind, attributes)
        self.faulted_boundary = boundary
        self.faulted_at = self.steps
        fleet.note("fault", note)
        return Call("POST", boundary, 200, {
            "message": "{}, for the next {} trials".format(note, self.FAULT_WINDOW_TRIALS)}, 0)

    def restart_clearing(self):
        return self.restart("clearing")

    def restart_core(self):
        return self.restart("core")

    def restart_bank(self):
        return self.restart("bank")

    # Core takes about ninety seconds to come back, so a restart is expensive and must be rare:
    # three of them eat half a ten-minute run, and restarting again before the last one finished
    # left core unable to answer at all while the run recorded every call as a finding.
    RESTART_COOLDOWN_TRIALS = 60

    def restart(self, which):
        """Stop a service and start it again, then wait for it to report itself healthy.

        Waiting matters: without it every following trial reports a connection failure that says
        nothing about the service and everything about the restart not having finished.
        """
        since = self.steps - self.restarted_at
        if self.restarted_at and since < self.RESTART_COOLDOWN_TRIALS:
            return Call("POST", "restart-" + which, 412, {
                "message": "a service was restarted {} trials ago, and the run waits {} between "
                           "restarts".format(since, self.RESTART_COOLDOWN_TRIALS)}, 0)
        if self.live_fault():
            return Call("POST", "restart-" + which, 412, {
                "message": "a fault is already injected, and two at once cannot be told apart"}, 0)
        self.restarted_at = self.steps
        self.note_in_flight("restart", "a restart of {}".format(which))
        done, note = faults.restart_service(which)
        if not done:
            return Call("POST", "restart-" + which, 412, {"message": note}, 0)
        # The restart counts as an injected fault for a few trials afterwards, because a service
        # that has just come back is still warming its caches and reconnecting, and a 5xx in that
        # window says nothing about the code.
        self.faulted_boundary = "a restart of " + which
        healthy = faults.wait_until_healthy(which)
        # The window is counted from the moment the service says it is up, not from the restart,
        # so the trials that follow are judged against a stack that has actually come back.
        self.faulted_at = self.steps
        self.wait_for_stack()
        return Call("POST", "restart-" + which, 200, {
            "message": "{}{}".format(note, "" if healthy else
                                     ", and it did not report itself healthy again")}, 0)

    RESTART_WAIT_SECONDS = float(os.environ.get("SIM_RESTART_WAIT", "120"))

    def wait_for_stack(self):
        """Block until the Direct API answers again, or the wait runs out."""
        deadline = time.time() + self.RESTART_WAIT_SECONDS
        while time.time() < deadline:
            call = self.client.call("GET", "/direct/v1/products")
            if getattr(call, "ok", False):
                return True
            time.sleep(3)
        return False

    def replay_last_call(self):
        """Send the last change the run made a second time, exactly as it was sent.

        This is what a client retry looks like after an answer it could not read, and it is the
        case idempotency keys and unique references exist for. The reply is judged by the oracles
        like any other: a second acceptance that makes a second object is a finding the run can
        see in the reads that follow.
        """
        held = getattr(self, "last_change", None)
        if not held:
            return Call("POST", "replay", 412,
                        {"message": "the run has made no change to replay yet"}, 0)
        method, path, body, name = held
        call = self.client.call(method, path, json_body=body)
        self.replayed = (name, call.status)
        return call

    def slow_the_bank(self):
        """Put a second of latency on the run's own credit into the bank."""
        return self.network_fault(faults.OWN_BANK_PROXY, "slow", "latency", self.LATENCY,
                                  "the run's credit into the bank now answers a second late")

    def break_the_bank(self):
        """Cut the credit part way through, so the payment may or may not have landed."""
        return self.network_fault(faults.OWN_BANK_PROXY, "cut", "timeout", self.CUT,
                                  "the run's credit into the bank is cut mid-call")

    # DEACTIVATED and CANCELLED are the two compliance stops, and PENDING puts a live customer back
    # before its check. ACTIVATED follows every stop, because a customer that is not ACTIVATED can
    # neither open nor fund an account: one run left all eleven customers DEACTIVATED and not one
    # account reached OPEN, so the stops have to be visited and then released.
    KYC_STATUSES = ("DEACTIVATED", "ACTIVATED", "PENDING", "ACTIVATED", "CANCELLED", "ACTIVATED")

    # At most this share of the live customers may sit outside ACTIVATED at once, so the run keeps
    # enough working customers to fund a batch while it explores the compliance states.
    STOPPED_SHARE = 0.34

    def set_kyc_status(self):
        """Force this customer's compliance status, then read what the Direct API now says."""
        held = self.held
        if not self.sim or not held.get("customerId"):
            return Call("POST", "set-kyc-status", 412,
                        {"message": "no simulator client, or no customer to act on"}, 0)
        subject = self.subjects[self.current]
        turn = subject.get("kycTurn", 0)
        status = self.KYC_STATUSES[turn % len(self.KYC_STATUSES)]
        if status != "ACTIVATED" and self.too_many_stopped():
            status = "ACTIVATED"
        subject["kycTurn"] = turn + 1
        call = world.set_kyc_status(self.sim, held["customerId"], status)
        if not getattr(call, "ok", False):
            return call
        # Compliance answers through its own queue, so this status lands on a later trial than the
        # one that asked for it, and a customer status that moves afterwards may be this and not
        # whatever the run was doing at the time.
        self.note_in_flight("compliance", "a compliance status change to {}".format(status),
                            held["customerId"])
        # The simulator answers with no body, so the trial would record no change at all. Read the
        # customer back, which is the state the run actually wants to see.
        return self.client.call("GET", "/direct/v1/customers/{}".format(held["customerId"]))

    def create_kyc_failed_customer(self):
        """Create a customer whose surname makes the compliance simulator fail its first KYC check.

        Refused while too much of the pool is already stopped, for the same reason SetKycStatus
        falls back to ACTIVATED: a pool of stopped customers can open and fund nothing.
        """
        if self.too_many_stopped():
            return Call("POST", "create-kyc-failed-customer", 412,
                        {"message": "too many live customers already sit outside ACTIVATED"}, 0)
        method, path, body = actions.BY_NAME["CreateKycFailedCustomer"].build(
            self.held, self.mint)
        call = self.client.call(method, path, json_body=body)
        if getattr(call, "ok", False) and isinstance(call.body, dict):
            # The check result comes back through compliance's queue, so the stop lands on a later
            # trial than the create.
            self.note_in_flight("compliance", "a KYC failure for {}".format(
                body["person"]["lastName"]), call.body.get("customerId"))
        return call

    def update_customer(self):
        """Change this customer's contact details, address and income through the Direct API."""
        held = self.held
        path = "/direct/v1/customers/{}".format(held["customerId"])
        read = self.client.call("GET", path)
        persons = read.body.get("persons") if read.ok and isinstance(read.body, dict) else None
        last_name = persons[0].get("lastName") if persons else None
        if not last_name:
            return Call("PUT", "update-customer", 412,
                        {"message": "could not read the surname the update has to keep"}, 0)
        subject = self.subjects[self.current]
        turn = subject.get("updateTurn", 0)
        subject["updateTurn"] = turn + 1
        call = self.client.call(
            "PUT", path, json_body=actions.update_customer_body(self.mint, last_name, turn))
        if getattr(call, "ok", False):
            # The update raises a fresh compliance check, whose answer comes back through
            # compliance's queue on a later trial.
            self.note_in_flight("compliance", "a KYC recheck after a details update",
                                held["customerId"])
        return call

    def freeze_customer(self):
        """Freeze this customer as an officer would, then read what the Direct API now says.

        Core refuses a freeze on anything but ACTIVATED, and that refusal is kept as an
        observation. The stopped share still holds, so a freeze never strands the cohort.
        """
        held = self.held
        if not self.compliance or not held.get("customerId"):
            return Call("PUT", "freeze-customer", 412,
                        {"message": "no compliance client, or no customer to act on"}, 0)
        if held.get("customerStatus") == "ACTIVATED" and self.too_many_stopped():
            return Call("PUT", "freeze-customer", 412,
                        {"message": "too many customers are already outside ACTIVATED"}, 0)
        return self.override_customer(held["customerId"], "FREEZE", "simulation explorer freeze")

    def unfreeze_customer(self):
        """Lift this customer's freeze, which also settles the instructions it held."""
        held = self.held
        if not self.compliance or not held.get("customerId"):
            return Call("PUT", "unfreeze-customer", 412,
                        {"message": "no compliance client, or no customer to act on"}, 0)
        return self.override_customer(held["customerId"], "APPROVE",
                                      "simulation explorer unfreeze")

    def override_customer(self, customer_id, action, notes):
        call = world.override_customer_status(self.compliance, customer_id, action, notes)
        if not getattr(call, "ok", False):
            return call
        # Unfreezing releases the held instructions, and the money they move lands on later trials.
        if action == "APPROVE":
            self.note_in_flight("compliance", "an unfreeze releasing held instructions", customer_id)
        # The override answers with no body. Read the customer back instead.
        return self.client.call("GET", "/direct/v1/customers/{}".format(customer_id))

    def advance_business_day(self):
        """Puts the cohort's bank one business day forward, accruing and realising over that day.

        This is the only way the run reaches a balance that has earned interest, or an account
        that has lived long enough for a term or a notice period to matter, because nothing else
        in the harness moves logical time.
        """
        if not self.bank_uid:
            return Call("POST", "advance-business-day", 412,
                        {"message": "BANK_UID is not set, so no bank can be advanced"}, 0)
        call = world.advance_business_day(self.ops, self.bank_uid)
        if getattr(call, "ok", False):
            self.days_advanced += 1
            self.note_in_flight("interest", "an interest accrual and realisation")
        return call

    def too_many_stopped(self):
        live = [s for s in self.subjects if s.get("customerId") and not s.get("closed")]
        if not live:
            return False
        stopped = [s for s in live if s.get("customerStatus") not in (None, "ACTIVATED")]
        return len(stopped) >= max(1, int(len(live) * self.STOPPED_SHARE))

    def process_closures(self):
        """Sweeps accounts sitting at CLOSING. Without it a closed account never finishes."""
        self.note_in_flight("closure", "a closure sweep")
        call = world.process_closures(self.ops)
        self.check_watched_closures()
        return call

    def notice_falls_due(self):
        """Bring this NOTICE account's waiting withdrawals due, then run the notice processor."""
        held = self.held
        if (self.subjects[self.current].get("productType") or "INSTANT") != "NOTICE":
            return Call("POST", "notice-falls-due", 412,
                        {"message": "only a NOTICE account has a notice withdrawal to fall due"}, 0)
        moved = world.bring_notice_due(held.get("accountId"))
        if moved is None:
            return Call("POST", "notice-falls-due", 412,
                        {"message": "core could not be read to bring the notice due"}, 0)
        if not moved:
            return Call("POST", "notice-falls-due", 409,
                        {"message": "the account has no notice withdrawal waiting"}, 0)
        self.note_in_flight("notice", "a notice withdrawal brought due and raised",
                            held.get("accountId"))
        return world.process_due_notice(self.ops)

    # The two orders the injection alternates between. "after": the processor raises the partial
    # withdrawal and settles it, then the customer closes. "during": the close and the processor
    # start together, so the close can land between the processor's read and its raise.
    NOTICE_CLOSURE_ORDERS = ("after", "during")
    NOTICE_PARTIAL = "1.00"
    TRIALS_BEFORE_A_NOTICE_CLOSURE = 40
    NOTICE_ATTEMPTS_ALLOWED = 10
    NOTICE_SETTLE_ROUNDS = 6

    def notice_subject(self):
        """Stand on a NOTICE account that can take a partial withdrawal, funding it if need be."""
        saved = self.current
        refusals = []
        for i, subject in enumerate(self.subjects):
            if subject.get("closed") or not subject.get("customerId"):
                continue
            if (subject.get("productType") or "INSTANT") != "NOTICE":
                continue
            if not subject.get("accountId") or not subject.get("accountReference"):
                continue
            self.current = i
            self.point_at_an_open_account()
            account = self.read("account") or {}
            if account.get("status") not in ("OPEN", "REQUESTED"):
                refusals.append("the NOTICE account reads {}".format(account.get("status")))
                continue
            if self.pending_instructions(account_id=subject["accountId"], remember=False):
                self.pay_off_batches()
                self.settle_world()
                self._pending_cache = {}
                if self.pending_instructions(account_id=subject["accountId"], remember=False):
                    refusals.append("the NOTICE account has an instruction in flight")
                    continue
                account = self.read("account") or {}
            if self.balance_of(account) <= Decimal(self.NOTICE_PARTIAL):
                # Core refuses a deposit line with "Customer is not in a state to deposit" unless
                # the customer is ACTIVATED, and the batch still answers 200. The one NOTICE
                # customer of cycle 42 was born PENDING, so every funding moved nothing.
                if not self.activate_for_deposit(subject["customerId"]):
                    refusals.append("the NOTICE customer did not become ACTIVATED")
                    continue
                funded = self.fund_account()
                self._pending_cache = {}
                account = self.read("account") or {}
                if self.balance_of(account) <= Decimal(self.NOTICE_PARTIAL):
                    refusals.append("funding the NOTICE account answered {} and moved nothing"
                                    .format(getattr(funded, "status", "nothing")))
                    continue
            return self.held, None
        self.current = saved
        return None, "; ".join(sorted(set(refusals))[:3]) or "no NOTICE account in the pool"

    ACTIVATION_WAIT_SECONDS = 30

    def activate_for_deposit(self, customer_id):
        """Make the customer ACTIVATED through the simulator, and wait until core reads it."""
        def status():
            call = self.client.call("GET", "/direct/v1/customers/{}".format(customer_id))
            return call.body.get("customerStatus") if call.ok and isinstance(call.body, dict) else None
        if status() == "ACTIVATED":
            return True
        if not self.sim or not getattr(world.set_kyc_status(self.sim, customer_id, "ACTIVATED"),
                                       "ok", False):
            return False
        deadline = time.monotonic() + self.ACTIVATION_WAIT_SECONDS
        while time.monotonic() < deadline:
            if status() == "ACTIVATED":
                return True
            time.sleep(2)
        return False

    @staticmethod
    def balance_of(account):
        try:
            return Decimal(str(account.get("balance") or "0"))
        except (ArithmeticError, ValueError):
            return Decimal("0")

    def close_notice_after_due(self):
        """Close a NOTICE account whose partial withdrawal is already due and being raised.

        `orchestrateNoticeClosureRequest` cancels every pending partial withdrawal on status alone,
        so a partial that the notice processor has already raised is cancelled while its money
        still moves. The payout then cannot complete a CANCELLED instruction. The sweep judges what
        is left: a cancelled withdrawal that pays out, and a message on the dead letter queue.
        """
        live = self.live_fault()
        if live:
            return Call("POST", "close-notice-after-due", 412,
                        {"message": "{} is already injected".format(live)}, 0)
        self.notice_attempts += 1
        self.notice_forced_at = self.steps
        held, refused = self.notice_subject()
        if not held:
            return Call("POST", "close-notice-after-due", 412, {"message": refused}, 0)
        order = self.NOTICE_CLOSURE_ORDERS[
            len(self.notice_closures) % len(self.NOTICE_CLOSURE_ORDERS)]
        partial = self.client.call(
            "POST", "/direct/v1/customers/{customerId}/accounts/{accountId}/instruction"
                    .format(**held),
            json_body={"instructionRequestType": "WITHDRAW", "productId": held["productId"],
                       "amount": self.NOTICE_PARTIAL,
                       "instructionReference": self.mint("n", 36)})
        if not partial.ok:
            return partial
        if not world.bring_notice_due(held["accountId"]):
            return Call("POST", "close-notice-after-due", 412,
                        {"message": "the partial withdrawal left no notice row to bring due"}, 0)
        close_path = ("/direct/v1/customers/{customerId}/accounts/{accountId}/close"
                      "?reason=NO_LONGER_NEEDED".format(**held))
        if order == "after":
            processed = world.process_due_notice(self.ops)
            self.settle_world()
            close = self.client.call("POST", close_path)
        else:
            (processed, close), _ = race.fire([
                lambda: world.process_due_notice(self.ops),
                lambda: self.client.call("POST", close_path),
            ])
        self.note_in_flight("notice", "a notice closure after the partial fell due",
                            held["accountId"])
        # The partial's payout reaches the consumer that fails only once the bank has paid it and
        # clearing has read that debit, so poll and enquire rather than settle alone.
        for _ in range(self.NOTICE_SETTLE_ROUNDS):
            world.enquire_payment_status(self.ops)
            world.poll_bank_transactions(self.ops)
            world.drain_transactions(self.ops)
            world.settle_payments(self.ops)
            time.sleep(5)
        self.sweeps += 1
        account = self.read("account") or {}
        body = getattr(close, "body", None)
        self.notice_closures.append({
            "order": order,
            "trial": self.steps,
            "customerId": held.get("customerId"),
            "accountId": held.get("accountId"),
            "partialStatus": partial.status,
            "processorStatus": getattr(processed, "status", None),
            "closeStatus": getattr(close, "status", None),
            "closeMessage": body.get("message") if isinstance(body, dict) else None,
            "statusAfter": account.get("status"),
            "balanceAfter": account.get("balance"),
        })
        print("  -- notice closure {}: account {} close answered {}, account reads {} holding {}"
              .format(order, (held.get("accountId") or "?")[:8], getattr(close, "status", None),
                      account.get("status"), account.get("balance")))
        return close

    def process_due_notice(self):
        """Run the notice processor, which raises every notice withdrawal already due."""
        self.note_in_flight("notice", "a notice processor run")
        return world.process_due_notice(self.ops)

    def place_batch(self):
        """A new batch across as many customers as the pool offers, unpaid, alongside any others.

        Creating does not pay, so several batches can be outstanding at once and the run can act
        between the two — cancel a line, pay part of it, pay another batch first.
        """
        members = self.batch_members()
        if not members:
            return self.client.call("POST", "/direct/v1/batches", json_body={})
        shape = self.BATCH_SHAPES[len(self.batches) % len(self.BATCH_SHAPES)]
        payment_reference = self.mint("p", 16)
        if shape == "shared":
            # The request record states that several batches may share one paymentReference, and
            # that a payment covering only one of them advances none while the reference is shared.
            # Nothing reaches that state unless the run deliberately reuses a reference.
            live = [b for b in self.batches if not b.get("done") and b.get("paymentReference")]
            if live:
                payment_reference = live[-1]["paymentReference"]
        lines = []
        for _, subject in members:
            reference = self.mint("i", 36)
            lines.append({
                "reference": reference,
                "customerId": subject["customerId"],
                "accountReference": subject["accountReference"],
                # Each line takes its own amount from its reference, so one batch can hold a penny
                # beside a hundred pounds and the total stops being a multiple of one number.
                "amount": Decimal(actions.amount_for(reference)),
                "type": "DEPOSIT",
                "status": "PENDING",
            })
        if shape == "mixed" and len(lines) > 1:
            # A batch is not deposits only: the allocation type accepts WITHDRAWAL, and a batch
            # holding both directions is a state no sequence of deposit-only batches reaches.
            lines[-1]["type"] = "WITHDRAWAL"
        # A withdrawal line asks for no money, so it must not be counted into what the payment
        # has to cover.
        required = sum((l["amount"] for l in lines if l["type"] == "DEPOSIT"), Decimal("0"))
        call = self.client.call("POST", "/direct/v1/batches", json_body={
            "batchReference": self.mint("batch", 36),
            "paymentReference": payment_reference,
            "totalPaymentRequired": "{:.2f}".format(required),
            "allocations": [{
                "customerId": l["customerId"],
                "accountReference": l["accountReference"],
                "instructionReference": l["reference"],
                "instructionType": l["type"],
                "productId": self.product_id,
                "amount": "{:.2f}".format(l["amount"]),
            } for l in lines],
        })
        if call.ok:
            body = call.body if isinstance(call.body, dict) else {}
            self.batches.append({
                "batchId": body.get("batchId"),
                "paymentReference": payment_reference,
                "required": required,
                "paid": Decimal("0"),
                "lines": lines,
                "members": [subject["customerId"] for _, subject in members],
                "done": False,
            })
            self.batch_at = payment_reference
        return call

    def guarded(self, driven, action_name):
        """Run an action the driver builds itself, turning a crash into one failed trial.

        A run is meant to go unattended for ten minutes and longer, so an action that raises must
        cost that trial and nothing more. One returning a list where a Call was expected ended a
        whole run at trial 293 with `'list' object has no attribute 'body'`.
        """
        try:
            call = driven(self)
        except Exception as raised:  # noqa: BLE001 - any fault here is a finding, not a stop
            return Call("POST", action_name, 599,
                        {"message": "the harness raised {}: {}".format(
                            type(raised).__name__, raised)}, 0)
        if not hasattr(call, "body"):
            return Call("POST", action_name, 599,
                        {"message": "the harness returned {} rather than one call".format(
                            type(call).__name__)}, 0)
        return call

    def product_for_new_subject(self):
        """The product the next customer opens on, alternating over what the platform offers.

        Alternating rather than always INSTANT, because the actions that only a TERM account can
        satisfy stay refused for the whole run otherwise.
        """
        offered = dict(self.products) or {"INSTANT": ("INSTANT", self.product_id)}
        instant = next(((t, p) for t, p in offered.values() if t == "INSTANT"), None)
        others = sorted((t, p) for t, p in offered.values() if t != "INSTANT")
        if not others or not instant:
            pairs = sorted(offered.values())
            opened = self.customers_made
            return pairs[opened % len(pairs)]
        # One subject in four opens on a product other than INSTANT. Measured rather than chosen:
        # a TERM account read costs 1.5 to 2.7 seconds against 0.19 for an INSTANT one, so a pool
        # split evenly between them ran at a fifth of the trials, and the run covered far fewer
        # states in the same time. One in four keeps the TERM states reachable and the run moving.
        opened = self.customers_made
        if opened % 3 == 2:
            # One subject in three rather than one in four, because there are now two products
            # other than INSTANT and each needs customers of its own to be reachable.
            return others[(opened // 3) % len(others)]
        return instant

    # How many accounts one customer may hold before the run stops opening more for it. Three is
    # enough to hold one being funded, one closing and one spare.
    ACCOUNTS_PER_SUBJECT = 3

    FAULT_SHARE = 1.0 / 8

    def fault_share(self):
        """How much of the recent run has gone on breaking the machinery."""
        recent = self.log[-40:]
        if not recent:
            return 0.0
        spent = len([t for t in recent
                     if t["action"].replace("RACE ", "").split(" + ")[0]
                     in actions.EXPENSIVE_FAULTS])
        return spent / float(len(recent))

    def rotate_batch(self):
        """Point the run at the outstanding batch it has acted on least.

        Without this `batch` always answers with the newest one, so several batches could be
        outstanding at once and every payment, cancellation and settle still landed on the last
        one placed. The older batches sat untouched until they aged out of the list.
        """
        live = [b for b in self.batches if not b.get("done")]
        if len(live) < 2:
            return
        least = min(live, key=lambda b: (b.get("touches", 0), b["paymentReference"]))
        self.batch_at = least["paymentReference"]

    def touch_batch(self, batch):
        if batch is not None:
            batch["touches"] = batch.get("touches", 0) + 1

    BATCH_SHAPES = ("fresh", "shared", "mixed", "fresh")

    def owed(self, batch):
        """What the batch still asks for: its live deposit lines, less what has been paid."""
        live = sum((l["amount"] for l in batch["lines"]
                    if l.get("status") not in ("CANCELLED", "REJECTED")
                    and l.get("type", "DEPOSIT") == "DEPOSIT"), Decimal("0"))
        return live - batch["paid"]

    PAYMENT_SHAPES = ("third", "half", "exact", "over", "penny")

    def pay_batch_part(self):
        """Credit the bank with part, all, or more than the batch still owes.

        The shape rotates rather than always paying the exact amount, because the states worth
        reaching are the ones either side of it: a batch part paid and still short, and a batch
        paid more than it asked for.
        """
        batch = self.batch
        if batch is None:
            # A race can settle the last live batch while this call is already in flight.
            return self.client.call("GET", "/direct/v1/batches")
        self.touch_batch(batch)
        owed = self.owed(batch)
        shape = self.PAYMENT_SHAPES[len(batch.get("payments", [])) % len(self.PAYMENT_SHAPES)]
        if owed <= 0:
            amount = self.LINE_AMOUNT
            shape = "over"
        elif shape == "third":
            amount = (owed / 3).quantize(Decimal("0.01"))
        elif shape == "half":
            amount = (owed / 2).quantize(Decimal("0.01"))
        elif shape == "penny":
            amount = Decimal("0.01")
        elif shape == "over":
            amount = owed + self.LINE_AMOUNT
        else:
            amount = owed
        amount = max(amount, Decimal("0.01"))

        call = world.credit_platform_at_bank(
            self.hsb, self.virtual_iban, "{:.2f}".format(amount), batch["paymentReference"],
            counterpart="GB29NWBK60161331926819")
        if getattr(call, "ok", False):
            self.credits_since_poll += 1
            batch["paid"] += amount
            batch.setdefault("payments", []).append({"shape": shape, "amount": str(amount)})
            if batch["paid"] >= batch["required"] and "paid_at_sweep" not in batch:
                batch["paid_at_sweep"] = self.sweeps
        call.own_wire = True
        return call

    def poll_if_new_money(self):
        """Pull the bank's statement only when something has been credited since the last pull."""
        if not self.credits_since_poll:
            return None
        self.credits_since_poll = 0
        return world.poll_bank_transactions(self.ops)

    def settle_world(self):
        """Pull what the bank holds through clearing and settle it.

        This moves every cohort's money, not one batch's, so it is a world action even though the
        driver reaches it while standing on a batch.
        """
        # Dues first, for the reason given in fund_account: a funding record only matches against
        # dues that already exist when the statement line drains.
        steps = [
            world.raise_platform_dues(self.ops, self.platform_uid),
            self.poll_if_new_money(),
            world.drain_transactions(self.ops),
            world.settle_payments(self.ops),
            world.drain_transactions(self.ops),
        ]
        failed = _first_failure(steps)
        if failed is not None:
            return failed
        self.note_in_flight("settlement", "a settlement sweep")
        self.sweeps += 1
        if self.decision_watch:
            self.check_decided_closures()
        self.refresh_batches()
        self.check_batches_settled()
        batch = self.batch
        if not batch or not batch.get("batchId"):
            # A plain read, because the caller needs one Call and the sweep's own steps answer
            # with lists and with None; returning steps[0] handed back the list of payment-due
            # calls and the step crashed on `call.body`.
            return self.client.call("GET", "/direct/v1/batches")
        return self.client.call("GET", "/direct/v1/batches/{}".format(batch["batchId"]))

    def check_batches_settled(self):
        """Ask of every outstanding batch whether the money it was paid actually moved.

        Run after the sweep, because that is the moment the answer is owed.
        """
        for batch in self.batches:
            if batch.get("done") or not batch.get("batchId"):
                continue
            shared = len([b for b in self.batches
                          if b["paymentReference"] == batch["paymentReference"]]) > 1
            sweeps = self.sweeps - batch.get("paid_at_sweep", self.sweeps)
            rejected = any(line.get("status") in ("REJECTED", "CANCELLED")
                           for line in batch.get("lines", []))
            violation = oracles.paid_batch_settles(
                batch["batchId"], batch["required"], batch["paid"],
                batch.get("status") or "unknown", sweeps, shared, rejected)
            if violation is None:
                continue
            row = violation.as_row()
            row["n"] = len(self.log) + 1
            row["action"] = "SettleWorld"
            row["inFlight"] = self.in_flight_now()
            row["leadUp"] = [
                {"n": t["n"], "action": t["action"], "status": t["status"],
                 "message": t.get("message")}
                for t in self.log[-8:]]
            self.record_violation(row, violation)

    def refresh_batches(self):
        """Re-read every outstanding batch, so line statuses and the amount owed stay true."""
        for batch in self.batches:
            if batch.get("done") or not batch.get("batchId"):
                continue
            read = self.client.call("GET", "/direct/v1/batches/{}".format(batch["batchId"]))
            if not read.ok:
                continue
            rows = _rows(read.body)
            by_reference = {r.get("instructionReference"): r for r in rows}
            for line in batch["lines"]:
                row = by_reference.get(line["reference"])
                if row and row.get("status"):
                    line["status"] = row["status"]
            status = (read.body or {}).get("status") if isinstance(read.body, dict) else None
            batch["status"] = status
            if status in ("SETTLED", "REJECTED", "CANCELLED"):
                batch["done"] = True

    def paid_state(self, batch):
        """How what has been paid stands against what is owed, as a state the run can aim at."""
        if batch["paid"] == 0:
            return "unpaid"
        owed = self.owed(batch)
        if owed > 0:
            return "part"
        if owed == 0:
            return "exact"
        return "over"

    def cancel_allocation(self):
        """Cancel one line of the batch, leaving the others in place."""
        batch = self.batch
        if batch is None:
            return self.client.call("GET", "/direct/v1/batches")
        self.touch_batch(batch)
        line = self.cancellable_line(batch)
        if line is None:
            return self.client.call("GET", "/direct/v1/batches/{}".format(batch["batchId"]))
        call = self.client.call(
            "DELETE", "/direct/v1/batches/{}".format(batch["batchId"]),
            json_body={"cancelAll": False, "instructionIds": [line["reference"]]})
        if getattr(call, "ok", False):
            line["status"] = "CANCELLED"
        return call

    def step(self):
        self.steps += 1
        self.explorer.steps = self.steps
        self.sweep_transition_chains()
        self.account_webhooks()
        self.ignore_repeats_now()
        self.top_up_accounts_now()
        self.long_life_tick()
        self.interest_sweep()
        self.rotate_batch()
        # Journey shape drives what to construct; entity keys drive what to do to what exists.
        # Without this the pool only grows when CreateCustomer happens to be the least-tried
        # action, so the driver runs out of reachable states and churns against the ones it has.
        wanted = self.shape_gap()
        if not wanted:
            wanted = self.journey_due()
        # A forced action is still subject to what this state has already refused. Without this
        # the refusal rule never applied to anything shape_gap named, because `remaining` held a
        # single action and the filter that drops refused ones had nothing else to choose.
        if wanted and self.ruled_out_here(wanted):
            wanted = None
        self.why = ""
        if wanted:
            self.current = self.pick_subject(for_action=wanted)
            constructible = [wanted]
            self.why = "shape gap"
            self.why_detail = "the pool is too thin, so {} is forced".format(wanted)
        else:
            self.current = self.pick_subject()
            self.pick_account()
            constructible = self.explorer.constructible(self.held)
        if not constructible:
            # Nothing left to do from this subject. A long run must not end on that, so it makes
            # another customer, as the shape gap does when the pool runs thin.
            self.current = self.pick_subject(for_action="CreateCustomer")
            constructible = ["CreateCustomer"]
            self.why = "nothing constructible"
        # Spending an entity is allowed once another subject exists to fall back on, which is what
        # lets the driver reach the closed and cancelled states at all.
        spendable = len([s for s in self.subjects
                         if s.get("customerId") and not s.get("closed")]) > 1
        remaining = constructible if spendable else [
            n for n in constructible if n not in actions.SPENDS] or constructible
        remaining = [n for n in remaining if n not in actions.DISABLED] or ["CreateCustomer"]
        if fleet.is_member():
            # Only faults were left to try here, and a member takes none, so it grows the pool
            # instead: returning nothing ended the whole run after 39 trials.
            remaining = [n for n in remaining if n not in fleet.STACK_FAULTS] or [
                "CreateCustomer"]

        # Read first, so the state key that chooses the action is the same key the trial is
        # recorded against. Choosing on one key and counting on another leaves every count at
        # zero, and the driver then repeats one action for the whole run.
        # Reuse the previous step's after-read as this step's before-read when it is of the same
        # subject and entity and the previous step is the one just finished. Reading a TERM
        # account costs one and a half to two and a half seconds, and the run reads the account
        # twice a step, so the second read was most of a ten-second trial. Nothing but the
        # driver's own bookkeeping happens between the two reads, so the reading still holds.
        wanted = {actions.BY_NAME[n].entity for n in remaining}
        carried = getattr(self, "_carried_read", None)
        before_by_entity = {}
        for entity in wanted:
            if (carried and carried[0] == self.current and carried[1] == entity
                    and carried[3] == self.steps - 1):
                before_by_entity[entity] = carried[2]
            else:
                before_by_entity[entity] = self.read(entity)
        keys = {n: self.key(actions.BY_NAME[n].entity, before_by_entity[actions.BY_NAME[n].entity])
                for n in remaining}
        for name in remaining:
            self.explorer.offer(keys[name], [name])

        # Stop calling what this state has already refused. The refusal is the rule the run was
        # looking for, so repeating the call buys nothing and crowds out actions never tried here.
        # A TERM product is exempt from check_non_term_cpa_uniqueness, so OpenAccount succeeds on
        # a TERM customer however many accounts it already holds. Least-tried then kept choosing
        # it: one run ended with 67 open TERM accounts and every other action starved. Opening is
        # dropped once the subject holds enough to act on.
        if len(self.subjects[self.current].get("accounts") or []) >= self.ACCOUNTS_PER_SUBJECT:
            without_opening = [n for n in remaining if n != "OpenAccount"]
            if without_opening:
                remaining = without_opening

        faulted = self.subjects[self.current].get("faulted") or set()
        allowed = [n for n in remaining
                   if not self.explorer.ruled_out(keys[n], n) and n not in faulted]
        if allowed:
            remaining = allowed

        # Faults are expensive and they crowd out exploration. Fourteen of the roughly thirty
        # actions now break the machinery, so picking by least-tried alone made nearly half the
        # run faults: 160 trials in ten minutes against 2600 without them, because a restart costs
        # ninety seconds and every call inside a fault window waits out a timeout. The budget
        # keeps one trial in eight for faults, which is enough to reach the states they open.
        within_budget = [n for n in remaining if n not in actions.EXPENSIVE_FAULTS]
        if within_budget and self.fault_share() >= self.FAULT_SHARE:
            remaining = within_budget

        # A route that walked to the end without landing in its target followed a false edge, so
        # count that as a failure. Rejection was the only thing that set a target aside, and this
        # loop succeeds on every call while arriving nowhere.
        if self.route_state == "reached" and self.route_target is not None:
            arrived = keys.get("ReadAccount") or keys.get("ReadCustomer")
            if arrived != self.route_target:
                self.stuck[self.route_target] = self.stuck.get(self.route_target, 0) + 1
                self.stuck_at[self.route_target] = self.steps
                self.route_note = "route ended somewhere other than its target"
            self.route_state = "none"

        # Follow a learned route to a state that still has untried work, rather than waiting to
        # drift into it. The route is only ever a sequence the run has already walked.
        if not self.route:
            here = keys.get("ReadAccount") or keys.get("ReadCustomer")
            self.route, self.route_target = self.plan_route(keys)
            if self.route:
                self.route_plan = list(self.route)
                self.route_from = here
                self.route_state = "walking"
                self.route_note = ""
            else:
                self.route_note = "no learned route to any state with untried work"
        if self.route and self.route[0] in remaining:
            action_name = self.route.pop(0)
            self.why = self.why or "route"
            self.why_detail = "step {} of {} on a learned route".format(
                len(self.route_plan) - len(self.route), len(self.route_plan))
            if not self.route:
                self.route_state = "reached"
                self.route_note = "route walked to the end"
        else:
            if self.route:
                self.route_note = "next step {} is not buildable from here".format(self.route[0])
                self.route_state = "abandoned"
            self.route = []
            action_name = min(remaining, key=lambda n: (self.explorer.tried[(keys[n], n)], n))
            if not self.why:
                self.why = "least tried"
                self.why_detail = "no route from here, so the least-tried buildable action wins"
        self.why_counts[self.why] = self.why_counts.get(self.why, 0) + 1

        action = actions.BY_NAME[action_name]
        before = before_by_entity[action.entity]
        key = keys[action_name]

        # Race the least-tried pair that is buildable here, now and then, rather than always
        # acting alone. A race is recorded whole: it cannot be attributed, so it is judged by how
        # many of a mutually exclusive set were accepted.
        if self.race_due(constructible, key):
            self._acted_customer = self.held.get("customerId")
            return self.run_race(constructible, key, before_by_entity)

        # One table decides which actions the driver runs itself, shared with the race path. Two
        # lists meant an action added to the race path alone was sent as a raw HTTP call here,
        # against the placeholder path its catalogue entry carries.
        driven = self.DRIVEN.get(action_name)
        if driven is not None:
            call = self.guarded(driven, action_name)
            if action_name == "FundAccount" and getattr(call, "ok", False):
                self.subjects[self.current]["funded"] = True
        else:
            try:
                method, path, body = action.build(self.held, self.mint)
            except KeyError as missing:
                # The subject held this value when the action was offered and lost it since, so
                # the action is unbuildable now rather than wrong. Killing the run on it costs
                # every trial that would have followed.
                call = Call("POST", action_name, 412, {
                    "message": "{} needs {} and the subject no longer holds it".format(
                        action_name, missing)}, 0)
            else:
                self._last_built = (method, path, body)
                call = self.client.call(method, path, json_body=body)
                # Only a call built here can be replayed. Setting this after a driven action paired
                # that action's name with an older raw request, so ReplayLastCall sent a stale
                # request and a 500 from a trigger was blamed on the replay.
                if (action.method or "GET").upper() != "GET" and action_name not in actions.WORLD:
                    self.last_change = (method, path, body, action_name)
        self.absorb(call.body, action_name)

        # Read the result from the subject the action was taken on, and read it BEFORE retiring
        # that subject. CloseCustomer used to pop the subject first, so the read landed on
        # whichever customer was next in the pool: every CloseCustomer edge recorded that
        # customer's state instead, `customer CLOSED` never became a landing, and the planner
        # could never route to it.
        acted_on = self.current
        # A server error is a defect, not a rule the service is teaching, and the oracle has
        # already recorded it with its evidence. Re-running it buys nothing and it is bound to the
        # subject it happened to, so remember it there: OpenAccount answering 500 for one customer
        # says nothing about a customer whose account slot is free.
        if 500 <= (call.status or 0) < oracles.TRANSPORT_FAULT:
            # A transport fault is left out: a timeout is transient and says nothing about this
            # subject, so excluding the action there would drop it for a fault that has passed.
            self.subjects[acted_on].setdefault("faulted", set()).add(action_name)
        self._acted_customer = self.subjects[acted_on].get("customerId")
        self._last_action = action_name
        moved_to = self.current
        self.current = acted_on
        after = self.read(action.entity)
        self._carried_read = (acted_on, action.entity, after, self.steps)
        landed = self.key(action.entity, after)
        self.current = moved_to

        # A closed customer stays in the pool until the run has tried everything it can there.
        # Retiring it the moment it closed put the driver in a loop: `customer CLOSED` sat on the
        # frontier with untried reads, the planner routed to it with CloseCustomer, the subject
        # left the pool in the same step, and shape_gap then forced CreateCustomer to refill —
        # 82 closes and 85 creates in 217 trials, with the untried actions never falling.
        if call.ok and action_name in actions.SPENDS:
            if action_name == "CloseCustomer":
                self.subjects[acted_on]["closed"] = True
            else:
                subject = self.subjects[acted_on]
                subject.pop("accountId", None)
                subject.pop("accountReference", None)
                subject.pop("funded", None)
        getattr(self, "_subject_keys", {}).pop(acted_on, None)
        self.retire_finished()
        trial = self.explorer.record(
            action_name, key, call, before, after, in_flight=1, landed=landed)
        self.check(action_name, call, action.entity, before, after)
        mark = "ok " if call.ok else "REJ"
        print("  {} {:22} {:3} {}".format(
            mark, action_name, call.status, sorted(trial.changes) or "no change"))
        if not call.ok and isinstance(call.body, dict):
            print("        {}".format(json.dumps(call.body.get("errorDetails") or call.body)[:200]))

        if not call.ok:
            # A route that cannot take its next step is not a route. Drop it, and set the target
            # aside so the planner stops choosing it.
            if self.route_target is not None:
                self.stuck[self.route_target] = self.stuck.get(self.route_target, 0) + 1
                self.stuck_at[self.route_target] = self.steps
                self.route_note = "{} was rejected, so the route was dropped".format(action_name)
                self.route_state = "abandoned"
            self.route = []
            self.resync(action_name, call)

        self.log.append({
            "n": len(self.log) + 1,
            "action": action_name,
            "status": call.status,
            "ok": call.ok,
            "key": list(key),
            "changes": sorted(trial.changes),
            "attributed": trial.attributed or not trial.changes,
            # The status the entity actually holds after the call, not just the field names that
            # moved. A finding that turns on which status an entity passes through cannot be
            # settled from a list of changed field names.
            "landedStatus": (after or {}).get(
                self.STATUS_FIELD.get(action.entity, "status")) if isinstance(after, dict) else None,
            "message": _message(call),
            "why": self.why,
            "why_detail": getattr(self, "why_detail", ""),
            "ms": round((time.monotonic() - getattr(self, "_trial_started", time.monotonic())) * 1000),
            "slowCalls": client.take_slow_calls(),
            # A grouped action answers with the call that failed, so its path says which of the
            # six steps in FundAccount broke; the status alone could not place fleet 187's 500.
            "failedPath": None if call.ok else "{} {}".format(
                getattr(call, "method", ""), getattr(call, "path", "")),
        })
        self._trial_started = time.monotonic()
        self.publish()
        return True

    def retire_finished(self):
        """Drop a closed customer once its state holds nothing new to try.

        Keeping it forever would fill the pool with customers that only ever answer reads, and
        dropping it at once puts the planner in a loop, so the run keeps it exactly as long as
        its state still has an untried action.
        """
        keep = []
        for i, subject in enumerate(self.subjects):
            if not subject.get("closed"):
                keep.append(subject)
                continue
            saved, self.current = self.current, i
            try:
                key = self.key("customer", self.read("customer"))
            finally:
                self.current = saved
            untried = [n for n in self.explorer.offered[key]
                       if self.explorer.tried[(key, n)] == 0]
            if untried:
                keep.append(subject)
        if len(keep) != len(self.subjects):
            here = self.subjects[self.current] if self.current < len(self.subjects) else None
            self.subjects = keep or [{}]
            self.current = keep.index(here) if here in keep else 0

    def pick_account(self):
        """Point the subject at one of its accounts, preferring one with untried work.

        A customer holds many accounts but the driver carries a single accountId, so without a
        choice it can only ever act on whichever account it absorbed last. An account cancelled
        by a race then stranded every other account the customer held.
        """
        subject = self.subjects[self.current]
        known = subject.get("accounts") or []
        if len(known) < 2:
            return
        best, best_score = None, None
        for held in known:
            saved_id = subject.get("accountId")
            saved_ref = subject.get("accountReference")
            subject["accountId"] = held["id"]
            if held.get("reference"):
                subject["accountReference"] = held["reference"]
            try:
                key = self.key("account", self.read("account"))
            finally:
                subject["accountId"] = saved_id
                if saved_ref is None:
                    subject.pop("accountReference", None)
                else:
                    subject["accountReference"] = saved_ref
            untried = len([n for n in self.explorer.offered[key]
                           if self.explorer.tried[(key, n)] == 0])
            score = (0 if untried else 1, -untried,
                     self.account_picks.get(held["id"], 0), self.explorer.visits[key])
            if best_score is None or score < best_score:
                best, best_score = held, score
        if best:
            self.account_picks[best["id"]] = self.account_picks.get(best["id"], 0) + 1
            subject["accountId"] = best["id"]
            # Move both together or neither: leaving the previous account's reference behind made
            # every call built from accountReference target a different account than accountId.
            if best.get("reference"):
                subject["accountReference"] = best["reference"]
            else:
                subject.pop("accountReference", None)

    POOL_LIVE = int(os.environ.get("SIM_POOL_LIVE", "6"))

    def shape_gap(self):
        """Names the action that would widen the run, or None when the pool is already varied.

        Two counters, both from §6: how many customers are live, and how many of them hold an
        account. A pool that is all bare customers cannot reach an account state, and a pool of
        one cannot spend anything.
        """
        live = [s for s in self.subjects
                if s.get("customerId") and not s.get("closed")]
        with_account = [s for s in live if s.get("accountId")]
        # Six live customers rather than three, because a batch spans the pool and the batch
        # states worth reaching are the wide ones. With three, every batch touched one or two
        # customers and the "many customers" batch state was never built.
        if len(live) < self.POOL_LIVE:
            return "CreateCustomer"
        # The pool has to hold a live customer for every product type, or that product's states
        # are unreachable however long the run goes. A pool capped at three customers while one
        # subject in four opens on TERM held no TERM customer at all, so no TERM account was ever
        # opened and the run fell from 43 states to 17.
        # Coverage is per product, not per product type: two TERM products need a customer each,
        # and only one of them can ever mature inside a run.
        kinds = {p for _, p in self.products.values()} or {self.product_id}
        covered = {s.get("productId") for s in live}
        if kinds - covered:
            return "CreateCustomer"
        # Half the pool holding an account, so a wide batch has enough accounts to allocate to.
        if len(with_account) < 4:
            return "OpenAccount"
        # A funded account is a prerequisite for most of the account state machine, and nothing
        # protected one: the run cancelled accounts faster than it funded them, so 47 accounts
        # were CANCELLED against 2 REQUESTED and not one deposit ever completed.
        # Ask the catalogue whether the action can be built rather than listing its fields here,
        # because Action.needs already declares them and a copy goes stale the moment one changes.
        if not any(s.get("funded") for s in live) and self.someone_can("FundAccount"):
            return "FundAccount"
        # Make one closure payment fail per run. Both injections sit in the fault budget, which
        # gives one trial in eight to eighteen actions, so a five minute run chose neither of them
        # and the question they answer went unasked. Forcing the first one costs a single trial.
        # Wait out a live fault rather than asking now. The injection refuses while any other fault
        # is in force, and that refusal starts no wait, so asking every trial spent the whole
        # fault window on refusals.
        if (len(self.closure_injections) < len(self.CLOSURE_INJECTIONS)
                and not fleet.is_member()
                and not self.live_fault()
                and self.closure_attempts < self.CLOSURE_ATTEMPTS_ALLOWED
                and self.steps >= self.TRIALS_BEFORE_A_CLOSURE_PAYMENT_FAILS
                and self.steps - self.closure_forced_at >= self.TRIALS_BETWEEN_CLOSURE_ATTEMPTS):
            wanted = self.CLOSURE_INJECTIONS[len(self.closure_injections)]
            if self.someone_can(wanted):
                return wanted
        # One notice closure of each order per run, forced for the same reason as the closure
        # payments: the path is four actions deep, on a product that few customers open.
        if (len(self.notice_closures) < len(self.NOTICE_CLOSURE_ORDERS)
                and not fleet.is_member()
                and not self.live_fault()
                and self.notice_attempts < self.NOTICE_ATTEMPTS_ALLOWED
                and self.steps >= self.TRIALS_BEFORE_A_NOTICE_CLOSURE
                and self.steps - self.notice_forced_at >= self.TRIALS_BETWEEN_CLOSURE_ATTEMPTS
                and self.someone_can("CloseNoticeAfterDue")):
            return "CloseNoticeAfterDue"
        # The notice adjustment is what a bank rate change owes the notice withdrawals already
        # waiting, so it follows a rate change that was accepted.
        if (not fleet.is_member() and self.log and self.log[-1]["action"] == "ChangeBankRate"
                and self.log[-1]["ok"] and self.someone_can("AdjustNoticeWithdrawals")):
            return "AdjustNoticeWithdrawals"
        return None

    def ruled_out_here(self, name):
        """True when this action has already been refused from the state it would act on."""
        action = actions.BY_NAME[name]
        return self.explorer.ruled_out(self.key(action.entity, self.read(action.entity)), name)

    def someone_can(self, name):
        """True when some live subject holds everything the action declares it needs."""
        action = actions.BY_NAME[name]
        saved = self.current
        try:
            for i, subject in enumerate(self.subjects):
                if subject.get("closed") or not subject.get("customerId"):
                    continue
                # A subject this action has already faulted on cannot satisfy a shape gap, since
                # naming it there would force the run straight back into the same server error.
                if name in (subject.get("faulted") or set()):
                    continue
                self.current = i
                if action.can_build(self.held):
                    return True
        finally:
            self.current = saved
        return False

    def resync(self, action_name, call):
        """Re-attach an account the server says exists but the subject has lost track of.

        Cancelling an opening clears the identifiers held for it, while the account itself may
        still be there in a state that refuses a fresh opening. The subject then keys to
        "account absent" and asks for an account it already has, forever.
        """
        message = call.body.get("message") if isinstance(call.body, dict) else None
        if not message or "already exists" not in message:
            return
        listed = self.client.call(
            "GET", "/direct/v1/customers/{customerId}/accounts".format(**self.held))
        if not listed.ok:
            return
        # Use the same reader as every other list endpoint: this one used to miss a
        # {content: [...]} response entirely and return having done nothing, which is the exact
        # failure it exists to repair.
        accounts = _rows(listed.body)
        for account in accounts:
            if isinstance(account, dict) and account.get("accountId"):
                self.subjects[self.current]["accountId"] = account["accountId"]
                self.subjects[self.current]["accountReference"] = account.get("accountReference")
                return

    STUCK_DECAY_STEPS = 60

    def blacklisted(self, key):
        """A target is set aside for a while after two route failures, never for good.

        One failure falls off every STUCK_DECAY_STEPS steps, so a target that failed early becomes
        eligible again once the run has moved on, which is the only way the frontier recovers.
        """
        count = self.stuck.get(key, 0)
        if count == 0:
            return False
        aged = (self.steps - self.stuck_at.get(key, 0)) // self.STUCK_DECAY_STEPS
        live = count - aged
        if live <= 0:
            self.stuck.pop(key, None)
            self.stuck_at.pop(key, None)
            return False
        return live >= 2

    SCENARIO_PERIOD = 3
    SCENARIOS = (
        ("overlapping withdrawals", ["FundAccount", "FundAccount", "PlaceWithdrawal",
                                     "PlaceWithdrawal", "PlaceWithdrawal", "SettleWorld",
                                     "ReadAccount", "PlaceWithdrawal", "SettleWorld"]),
        ("a long life", ["FundAccount", "AdvanceBusinessDay", "AdvanceBusinessDay",
                         "PlaceWithdrawal", "AdvanceBusinessDay", "FundAccount", "SettleWorld",
                         "AdvanceBusinessDay", "AdvanceBusinessDay", "RunDataFeed",
                         "PlaceWithdrawal", "SettleWorld", "CloseAccount", "ProcessClosures",
                         "SettleWorld", "RunDataFeed"]),
        ("notice churn", ["FundAccount", "PlaceWithdrawal", "CancelWithdrawal", "PlaceWithdrawal",
                          "NoticeFallsDue", "ProcessDueNotice", "SettleWorld", "CloseAccount",
                          "ProcessClosures"]),
        ("compliance whiplash", ["FundAccount", "SetKycStatus", "PlaceWithdrawal", "SetKycStatus",
                                 "FundAccount", "SetKycStatus", "PlaceWithdrawal", "SettleWorld",
                                 "CloseAccount"]),
        ("frozen with money moving", ["FundAccount", "PlaceWithdrawal", "FreezeCustomer",
                                      "PlaceWithdrawal", "FundAccount", "SettleWorld",
                                      "UnfreezeCustomer", "SettleWorld", "ReadInstructions"]),
        ("strange requests", ["WeirdCall", "WeirdCall", "WeirdCall", "WeirdCall", "ReadCustomer",
                              "WeirdCall", "WeirdCall"]),
        ("maturity and close", ["FundAccount", "SetMaturityDestination", "AdvanceBusinessDay",
                                "AdvanceBusinessDay", "ClearMaturityDestination",
                                "SetMaturityDestination", "AdvanceBusinessDay", "SettleWorld"]),
    )

    def plan_route(self, keys):
        """Aim at the nearest state that still has untried buildable work.

        A target whose route keeps failing is set aside for a while. Without that the driver
        re-plans the same rejected first step every turn: one run spent 330 of 335 OpenAccount
        calls being told an underfunded account already existed, never moving and never giving up.
        """
        here = keys.get("ReadAccount") or keys.get("ReadCustomer")
        if here is None:
            return [], None
        # Every third plan is a written scenario rather than the shortest learned route, because
        # the learned routes are all short: least-tried picking never strings twelve actions on one
        # account together, and the defects live at the end of long histories.
        self.plans = getattr(self, "plans", 0) + 1
        if self.plans % self.SCENARIO_PERIOD == 0:
            name, steps = self.SCENARIOS[(self.plans // self.SCENARIO_PERIOD) % len(self.SCENARIOS)]
            self.route_note = "scenario: " + name
            return list(steps), None
        best, target = None, None
        for visits, key, untried in self.explorer.frontier():
            if key == here or self.blacklisted(key):
                continue
            path = self.explorer.route(here, key)
            if path and (best is None or len(path) < len(best)):
                best, target = path, key
        return list(best or []), target

    def raceable(self, constructible):
        """The actions of this state that may take part in a race."""
        return [n for n in constructible if n not in actions.NEVER_RACED]

    def race_due(self, constructible, key):
        """Race only after this state has nothing left to try alone, and at most once per four
        single trials there.

        Both rules matter. Gating on total visits let each race raise its own budget, because a
        race increments the visit count, so the run reached 647 races against 21 single actions
        and the frontier never moved. Clearing the untried actions first keeps exploration ahead
        of stress.
        """
        untried = [n for n in self.explorer.offered[key] if self.explorer.tried[(key, n)] == 0]
        if untried:
            return False
        solo = self.explorer.solo[key]
        if solo < 4:
            return False
        options = race.candidates(self.raceable(constructible), actions.BY_NAME, actions.SPENDS,
                                  world=actions.WORLD, driven=set(self.DRIVEN))
        if not options:
            return False
        # Budget the races at this state as a group, not the cheapest one of them. Comparing the
        # least-raced combination held while there were nine of them, but composing combinations
        # dynamically makes 175, so one has always been raced zero times, the test read `0 < solo
        # // 4`, and every step after the fourth became a race — 964 of 1229 trials.
        raced_here = sum(n for (k, _names), n in self.races.items() if k == key)
        return raced_here < solo // 4

    def run_race(self, constructible, key, before_by_entity):
        options = race.candidates(self.raceable(constructible), actions.BY_NAME, actions.SPENDS,
                                  world=actions.WORLD, driven=set(self.DRIVEN))
        names, allowed = min(options, key=lambda o: (self.races.get((key, o[0]), 0), o[0]))
        self.races[(key, names)] = self.races.get((key, names), 0) + 1
        entity = actions.BY_NAME[names[0]].entity
        # The pair is chosen here, so its entity can differ from the action the step planned.
        # Comparing a customer read after the race with a batch read before it made every field
        # look changed, and all four AddNominatedAccount races were counted unattributed.
        before = before_by_entity[entity] if entity in before_by_entity else self.read(entity)

        self._acted_customer = self.held.get("customerId")
        self._last_action = "RACE " + " + ".join(names)
        held = self.held
        calls = []
        for name in names:
            # Some actions are built by the driver rather than from `held`, because they span
            # several customers or drive the ops endpoints. Racing them through `act.build` would
            # send a request that means nothing.
            driven = self.DRIVEN.get(name)
            if driven:
                calls.append(driven.__get__(self, type(self)))
                continue
            act = actions.BY_NAME[name]
            method, path, body = act.build(held, self.mint)
            calls.append(lambda m=method, p=path, b=body: self.client.call(m, p, json_body=b))

        results, elapsed = race.fire(calls)
        # Read the entity the race acted on before taking the identifiers it hands back. A
        # CreateCustomer leg hands back a new PENDING customer, and reading that one after a
        # CloseCustomer leg reported a close that had worked as one that moved nothing.
        after = self.read(entity)

        # Take the identifiers a race hands back, and apply the same spend bookkeeping a single
        # action gets. Without this the driver kept the identifiers it held before the race: an
        # account opened by a race was never learnt, an account cancelled by one was never let go,
        # and the run then funded a CANCELLED account nine times in a row while the service
        # correctly answered "Account is not accepting instructions".
        for name, result in zip(names, results):
            if getattr(result, "ok", False):
                self.absorb(result.body, name)
        for name, result in zip(names, results):
            if getattr(result, "ok", False) and name in actions.SPENDS:
                subject = self.subjects[self.current]
                if name == "CloseCustomer":
                    subject["closed"] = True
                else:
                    subject.pop("accountId", None)
                    subject.pop("accountReference", None)

        outcome = race.RaceOutcome(list(names), results, before, after, elapsed, actions.BY_NAME,
                                   world=actions.WORLD, spends=actions.SPENDS,
                                   terminal=oracles.TERMINAL)
        # A 5xx while a fault is injected is the fault, so the race is judged on what it can still
        # answer: whether an accepted spend moved anything. Judging the 5xx made every race in one
        # run a finding, 78 of 78, all of them the injected fault answering as designed.
        verdict = outcome.verdict(allowed, ignore_server_errors=bool(self.live_fault()))

        label = "RACE " + " + ".join(names)
        mark = "!! " if verdict else "ok "
        print("  {} {:34} {} in {:.0f}ms{}".format(
            mark, label, outcome.statuses, elapsed, "  <- " + verdict if verdict else ""))

        self.explorer.record_race(key, verdict)
        for name, result in zip(names, results):
            self.check(name, result, entity, before, after)
        self.log.append({
            "n": len(self.log) + 1,
            "action": label,
            "status": max(outcome.statuses),
            "ok": verdict is None,
            "key": list(key),
            "changes": sorted(projector.diff(before, after)),
            "message": verdict,
            "race": True,
            "attributed": True,
            "statuses": outcome.statuses,
        })
        self.publish()
        return True

    def pick_subject(self, for_action=None):
        """Work on the subject whose own state the driver has seen least, so attention spreads."""
        if not any(s.get("customerId") for s in self.subjects):
            return 0
        if for_action:
            # A subject that has already produced a server error for this action cannot show
            # anything new, and the state key does not name the subject, so ruling the action out
            # globally would suppress it on every other customer too.
            clean = [i for i, s in enumerate(self.subjects)
                     if s.get("customerId") and not s.get("closed")
                     and for_action not in (s.get("faulted") or set())]
        else:
            clean = None
        if for_action == "OpenAccount":
            waiting = [i for i, subject in enumerate(self.subjects)
                       if subject.get("customerId") and not subject.get("accountId")
                       and (clean is None or i in clean)]
            if waiting:
                # Prefer a customer whose product type has the fewest accounts opened so far.
                # Taking the first waiting customer always found an early one, and since the
                # first three of every four are INSTANT, no TERM account was ever opened and the
                # whole term region stayed unreachable however long the run went.
                opened = {}
                for subject in self.subjects:
                    if subject.get("accountId") or subject.get("accounts"):
                        kind = subject.get("productType") or "INSTANT"
                        opened[kind] = opened.get(kind, 0) + 1
                return min(waiting, key=lambda i: (
                    opened.get(self.subjects[i].get("productType") or "INSTANT", 0), i))
        if for_action == "FundAccount":
            for i, subject in enumerate(self.subjects):
                if (subject.get("accountReference") and subject.get("accountId")
                        and not subject.get("funded") and (clean is None or i in clean)):
                    return i

        # Prefer the subject sitting in a state that still has untried work the run can actually
        # build from there. Visit count only breaks the tie, so a heavily-visited state with
        # something left to try beats a fresh one with nothing.
        best, best_score = self.current, None
        for i, subject in enumerate(self.subjects):
            if not subject.get("customerId"):
                continue
            if clean is not None and clean and i not in clean:
                continue
            entity = "account" if subject.get("accountId") else "customer"
            key = self.subject_key(i, entity)
            untried = len([n for n in self.explorer.offered[key]
                           if self.explorer.tried[(key, n)] == 0])
            score = (0 if untried else 1, -untried, self.explorer.visits[key])
            if best_score is None or score < best_score:
                best, best_score = i, score
        return best

    SUBJECT_KEY_SECONDS = float(os.environ.get("SIM_SUBJECT_KEY_TTL", "10"))

    def subject_key(self, index, entity):
        """This subject's state key, re-read at most once every SUBJECT_KEY_SECONDS.

        pick_subject scores every subject on every step, so it was reading the whole pool each
        time just to choose which one to work on. A TERM account read makes core build a daily
        interest projection over the whole term, so those reads went from cheap to costly and the
        run fell to one trial a minute. The key only steers the choice, so a reading a few seconds
        old is good enough, and the subject the run acts on is read fresh by `step` regardless.
        """
        cache = getattr(self, "_subject_keys", None)
        if cache is None:
            cache = self._subject_keys = {}
        now = time.time()
        found = cache.get(index)
        if found and now - found[0] < self.SUBJECT_KEY_SECONDS:
            return found[1]
        saved, self.current = self.current, index
        try:
            key = self.key(entity, self.read(entity))
        finally:
            self.current = saved
        cache[index] = (now, key)
        return key

    # A customer or account in one of these states answers the same way for the rest of the run,
    # so re-reading it only costs a call.
    SETTLED_FOR_GOOD = {"CLOSED", "CANCELLED"}
    WORLD_REFRESH_SECONDS = float(os.environ.get("SIM_WORLD_REFRESH", "6"))

    def world_state(self, acted_customer=None, action=None):
        """The exchange as the run last saw it: every customer, its account, its batch.

        Reads each subject rather than reporting the identifiers the driver happens to hold, so
        the page draws the service's own state instead of the driver's bookkeeping.

        Two costs are kept down, because this used to read every customer, batch and account on
        every step: a reading younger than WORLD_REFRESH_SECONDS is reused whole, and an entity
        already in a state it can never leave is never read a second time. A 10-subject pool with
        5 accounts each was 70 calls a step, which saturated the laptop and slowed the run itself.
        """
        now = time.time()
        fresh = getattr(self, "_world_cache", None)
        if fresh and now - fresh[0] < self.WORLD_REFRESH_SECONDS:
            view = dict(fresh[1])
            view["action"] = action
            view["current"] = self.current
            for row in view["subjects"]:
                row["hit"] = row["customerId"] == acted_customer
                row["current"] = row["i"] == self.current
            return view
        settled = getattr(self, "_world_settled", {})
        rows = []
        saved = self.current
        try:
            for i, subject in enumerate(self.subjects):
                if not subject.get("customerId"):
                    continue
                self.current = i
                if settled.get(("customer", subject["customerId"])):
                    customer = settled[("customer", subject["customerId"])]
                else:
                    customer = self.read("customer") or {}
                    if customer.get("customerStatus") in self.SETTLED_FOR_GOOD:
                        settled[("customer", subject["customerId"])] = customer
                batch = self.read("batch") or {}
                allocations = batch.get("content") or batch.get("allocations") or []
                accounts = []
                for held in subject.get("accounts", []):
                    body = settled.get(("account", held["id"]))
                    if body is None:
                        call = self.client.call(
                            "GET", "/direct/v1/customers/{}/accounts/{}".format(
                                subject["customerId"], held["id"]))
                        body = call.body if call.ok else {}
                        if (body or {}).get("status") in self.SETTLED_FOR_GOOD:
                            settled[("account", held["id"])] = body
                    product = (body or {}).get("product") or {}
                    accounts.append({
                        "id": held["id"],
                        "status": (body or {}).get("status"),
                        "product": product.get("productType"),
                        "balance": (body or {}).get("balance"),
                        "reference": (body or {}).get("accountReference") or held.get("reference"),
                        "live": held["id"] == subject.get("accountId"),
                    })
                rows.append({
                    "i": i,
                    "customerId": subject["customerId"],
                    "status": customer.get("customerStatus") or subject.get("customerStatus"),
                    # The product this customer opens on, so a product type the run never reached
                    # shows as a customer with no account rather than as an absence nobody sees.
                    "productType": subject.get("productType") or "INSTANT",
                    "nominated": len(customer.get("nominatedAccounts") or []),
                    "accounts": accounts,
                    "batch": {
                        "id": subject.get("batchId"),
                        "status": batch.get("status"),
                        "total": batch.get("totalPaymentRequired"),
                        "allocations": [{
                            "customerId": a.get("customerId"),
                            "amount": a.get("amount"),
                            "type": a.get("instructionType"),
                            "status": a.get("status"),
                            "rejection": a.get("rejectionMessage"),
                        } for a in allocations],
                    } if subject.get("batchId") else None,
                    "hit": subject["customerId"] == acted_customer,
                })
        finally:
            self.current = saved
        for row in rows:
            row["current"] = row["i"] == saved
        view = {"subjects": rows, "action": action, "current": saved}
        self._world_settled = settled
        self._world_cache = (now, view)
        return view

    def reach_state(self):
        """Which frontier states the driver can actually route to from where it stands.

        A frontier state nothing lands on is unreachable no matter how long the run goes, so the
        page shows that rather than leaving a target that never gets picked.
        """
        lands_on = set()
        crossing = 0
        within = 0
        for (from_key, _action), landings in self.explorer.edges.items():
            for landed in landings:
                if landed == from_key:
                    continue
                lands_on.add(landed)
                if landed[0] != from_key[0]:
                    crossing += 1
                else:
                    within += 1
        rows = []
        for visits, key, untried in self.explorer.frontier():
            rows.append({
                "key": list(key),
                "visits": visits,
                "untried": sorted(untried),
                "is_landing": key in lands_on,
                "set_aside": self.blacklisted(key),
            })
        return {"edges_within_kind": within, "edges_across_kinds": crossing, "targets": rows}

    def plan_state(self):
        """What the driver is aiming at, and how far along it is.

        `route_plan` holds every step as planned and `route` holds the steps left, so the
        difference is the number already taken.
        """
        if not self.route_plan or self.route_target is None:
            return {"target": None, "note": self.route_note, "state": self.route_state}
        done = len(self.route_plan) - len(self.route)
        target_untried = []
        for _, key, untried in self.explorer.frontier():
            if key == self.route_target:
                target_untried = sorted(untried)
                break
        return {
            "target": list(self.route_target),
            "from": list(self.route_from) if self.route_from else None,
            "steps": list(self.route_plan),
            "done": done,
            "next": self.route[0] if self.route else None,
            "why": target_untried,
            "note": self.route_note,
            "state": self.route_state,
        }

    def write_page(self):
        """Put this run's own copy of the live view beside its data, once.

        `live.html` is the view: it polls the data every five seconds and carries the tabs, the graph
        and the world panel. The run gives it a copy of its own rather than a link, so a finished
        run keeps a page that still works when a later run overwrites nothing of it.
        """
        if getattr(self, "_page_written", False):
            return
        source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live.html")
        try:
            with open(source) as handle:
                view = handle.read()
        except OSError:
            return
        name = os.path.basename(self.page)[:-len(".html")]
        view = view.replace(
            "var SOURCE = (new URLSearchParams(location.search).get('run') || 'current') + '.json';",
            "var SOURCE = (new URLSearchParams(location.search).get('run') || {!r}) + '.json';"
            .format(name))
        with open(self.page, "w") as handle:
            handle.write(view)
        self._page_written = True

    PUBLISH_SECONDS = 5

    def publish(self, finished=False):
        if not finished and time.time() - self.published_at < self.PUBLISH_SECONDS:
            return
        self.published_at = time.time()
        # Write then rename: the page fetches this every five seconds, and a reader that catches a
        # half-written file gets a parse error rather than stale-but-valid data.
        target = self.page.replace(".html", ".json")
        # The temporary name carries this process's id. Two runs sharing one temporary name means
        # whichever renames first takes the file away, and the other dies on the rename it was
        # about to make: that is how a run ended at trial 160 with FileNotFoundError on run.json.
        scratch = "{}.{}.tmp".format(target, os.getpid())
        with open(scratch, "w") as handle:
            json.dump({
                "run_id": self.run_id,
                "base_url": self.client.base_url,
                "platform_uid": self.platform_uid,
                "products": {label: kind for label, (kind, _) in sorted(self.products.items())},
                "trials": self.log[:],
                "trialTotals": self.log.totals(),
                "summary": self.explorer.summary(),
                "frontier": [{"key": list(k), "visits": v, "untried": sorted(u)}
                             for v, k, u in self.explorer.frontier()],
                "plan": self.plan_state(),
                "why_counts": self.why_counts,
                "violations": self.violations,
                "world": self.world_state(getattr(self, "_acted_customer", None),
                                          getattr(self, "_last_action", None)),
                "reach": self.reach_state(),
                "finished": finished,
                # Which boundary is carrying a network fault right now, so a finding can be read
                # against the fault that was live rather than against a guess made afterwards.
                "faults": {
                    "boundary": self.faulted_boundary,
                    "available": self.faults.proxies() if self.faults.ready else [],
                    "daysAdvanced": self.days_advanced,
                    "lastInterruption": self.last_interruption,
                    "interruptions": self.interruptions,
                    "trialsSinceRestart": (self.steps - self.restarted_at
                                           if self.restarted_at else None),
                    "faultShare": round(self.fault_share(), 3),
                    "duplicatingQueue": self.duplicating,
                    "messagesCopied": self.messages_copied,
                },
                "transitions": self.explorer.transitions(),
                "chains": {
                    "sweeps": self.chain_sweeps,
                    "rules": integrity.SWEEP_RULES,
                    "errors": self.chain_errors,
                    "sweptSecondsAgo": (round(time.time() - self.chain_swept_at)
                                        if self.chain_swept_at else None),
                },
                "closurePayments": self.closure_injections,
                "operatorDecisions": self.operator_decisions,
                "noticeClosures": self.notice_closures,
                "operatorQueues": getattr(self, "operator_queues", None),
                "inFlight": self.in_flight_now(),
                "webhooks": self.webhook_stats,
                "journeys": getattr(self, "journeys", []),
                "interestOracle": getattr(self, "interest_stats", None),
                "longLived": self.long.snapshot() if getattr(self, "long", None) else None,
            }, handle)
        os.replace(scratch, target)
        # live.html with no ?run= reads current.json, so the plain address always follows the
        # run in progress rather than whichever run last wrote under the default name.
        current = os.path.join(os.path.dirname(target), "current.json")
        shutil.copyfile(target, scratch)
        os.replace(scratch, current)
        self.write_page()


def _number(value):
    """The number in a reading like "2.01s", or None when it does not hold one."""
    try:
        return float(str(value).rstrip("s"))
    except (TypeError, ValueError):
        return None


def _worse(candidate, held):
    """True when a new reading is a bigger number than the one already recorded."""
    new, old = _number(candidate), _number(held)
    return new is not None and old is not None and new > old


def _first_failure(steps):
    """The first call in a sequence that did not succeed, or None.

    raise_platform_dues and settle_payments answer with a LIST of calls rather than one, so a
    plain `call.ok` check on the sequence skipped both of them without saying so.
    """
    for step in steps:
        if step is None:
            continue
        calls = step if isinstance(step, (list, tuple)) else [step]
        for call in calls:
            if not getattr(call, "ok", True):
                return call
    return None


def _rows(body):
    """The rows of a read, whether it answers with a list or wraps one in `content`."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        return body.get("content") or body.get("allocations") or []
    return []


def _message(call):
    """The rejection text, which is the rule the service enforces stated in its own words."""
    if call.ok or not isinstance(call.body, dict):
        return None
    details = call.body.get("errorDetails")
    if isinstance(details, list) and details:
        return "; ".join(d.get("message", "") + " (" + str(d.get("description", "")) + ")"
                         for d in details)
    return call.body.get("message")


def main():
    settings = config.load("local")
    base_url, token_url, client_id, client_secret = config.require(
        settings, "base_url", "auth_token_url", "auth_client_id", "auth_client_secret")
    client = DirectClient(base_url, token_url, client_id, client_secret, settings.get("auth_scope"))
    ops = BearerClient(settings["ops_base_url"], world.ops_token(settings),
                       renew=lambda: world.ops_token(settings))
    hsb = BearerClient(settings["hsb_base_url"])
    # The simulator authenticates against the same Cognito pool as the ops API and rejects an
    # unauthenticated call with 401, so it takes the ops token rather than the Direct client's.
    sim = (BearerClient(settings["simulator_base_url"], world.ops_token(settings),
                        renew=lambda: world.ops_token(settings))
           if settings.get("simulator_base_url") else None)
    # compliance-api takes the same ops token, and freezing needs an OPERATIONS officer.
    compliance = BearerClient(settings.get("compliance_base_url", "http://localhost:10005"),
                              world.ops_token(settings), renew=lambda: world.ops_token(settings))

    platform_uid = os.environ.get("PLATFORM_UID")
    virtual_iban = os.environ.get("PLATFORM_VIRTUAL_IBAN")

    # Stop before the first action rather than producing trials that mean nothing. A run once
    # spent 223 trials on a platform uid that matched nothing, against an INDIVIDUAL cohort that
    # refused every funding call, and the output was indistinguishable from real findings.
    try:
        found_product = preflight.check(client, ops, hsb, platform_uid, virtual_iban)
    except preflight.NotReady as not_ready:
        print("the environment is not ready for a run:")
        print(not_ready)
        print()
        print("stand up a POOLED cohort with standup_cohort.py, then export the PLATFORM_UID and")
        print("PLATFORM_VIRTUAL_IBAN it prints.")
        return 2

    # Put every queue timeout, every stopped service and every toxic back when the run ends,
    # whether it ends by itself, by KeyboardInterrupt, or because cycle.py sent it SIGTERM.
    # In a fleet the conductor owns toxiproxy. A member that set up or tore down the shared bank
    # proxy would cut the conductor's traffic in the middle of its trial.
    if not fleet.is_member():
        faults.install_cleanup()

    run = Run(client, ops, hsb, platform_uid, virtual_iban, sim, compliance)
    # Route the run's own bank credits through toxiproxy, so a network fault can be injected on
    # that one path. Without toxiproxy the run keeps the direct client and the fault actions say
    # so rather than pretending to inject anything.
    if fleet.is_member():
        print("  a fleet member: the conductor injects the faults")
    elif run.faults.start():
        run.hsb = BearerClient(faults.THROUGH_PROXY)
        hsb.close()
        hsb = run.hsb
        print("  bank credits route through toxiproxy at {}".format(faults.THROUGH_PROXY))
    else:
        print("  toxiproxy is not running, so no network fault can be injected")
    run.product_id = found_product
    print("run {} against {}".format(run.run_id, base_url))
    print("  platform {} · virtual account {} · product {}".format(
        platform_uid, virtual_iban, found_product))

    products = client.call("GET", "/direct/v1/products")
    rows = (products.body.get("content") or []) if products.ok else []
    # Keyed by productId, because every product in this cohort answers with the same `name` —
    # the read returns the fee alias, "Harness no fee", not the product name. Keying by type
    # threw away the second TERM product and keying by name collapsed all three into one; a
    # one-year term cannot mature inside a run, so losing the one-month one loses maturity.
    offered = {}
    for row in rows:
        kind = row.get("productType")
        months = (row.get("periodFeature") or {}).get("termPeriod")
        label = "{} {} month".format(kind, months) if months else kind
        offered[label] = (kind, row.get("productId"))
    instant = next((p for t, p in offered.values() if t == "INSTANT"), None)
    if not instant:
        print("no INSTANT product available — cannot explore")
        return 1
    run.product_id = instant
    run.products = offered
    if longlived.ENABLED:
        run.long = longlived.LongLife(run)
        print("  long-lived bank: {} the clock, population of {} customers".format(
            "keeping" if run.long.clock else "not keeping",
            len(run.long.population.live()) if run.long.population else 0))
    print("  products {}".format(", ".join(
        "{} ({}) {}".format(name, t, p[:8]) for name, (t, p) in sorted(offered.items()))))

    run.take_chain_baseline()
    step = 0
    started = time.time()
    try:
        while STEPS == 0 or step < STEPS:
            if SECONDS and time.time() - started >= SECONDS:
                print("\n  time limit of {:.0f}s reached after {} trials".format(SECONDS, step))
                break
            if not run.step():
                break
            step += 1
    except KeyboardInterrupt:
        print("\n  stopped after {} trials".format(step))

    # One last reading of the chains, because a break written by the final trials would otherwise
    # go unread until the next run.
    run.sweep_transition_chains(force=True)
    run.account_webhooks(final=True)
    run.interest_sweep(final=True)
    if getattr(run, "long", None) and run.long.population:
        run.long.population.save()

    print()
    print("  {:22} {}".format("oracle violations", len(run.violations)))
    print("  {:22} {}".format("chain sweeps", run.chain_sweeps))
    if run.closure_injections:
        print("  {:22} {}".format("closure injections", len(run.closure_injections)))
    for label, value in run.explorer.summary().items():
        print("  {:22} {}".format(label, value))
    print()
    print("  frontier (least visited first):")
    for seen, key, untried in run.explorer.frontier()[:6]:
        print("    {:2} visits  {}  untried: {}".format(seen, key, ", ".join(sorted(untried)[:4])))

    run.publish(finished=True)
    print()
    print("  page: {}".format(run.page))
    if not fleet.is_member():
        run.faults.close()
    for c in (client, ops, hsb, sim):
        if c is not None:
            c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
