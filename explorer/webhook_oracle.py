"""Webhook deliveries held up against what the harness itself did, and against each other.

`webhooks.check` compares deliveries with the Direct API's reads, so a transaction core books wrong
reads the same on the webhook and on the API. Here the amounts come from the requests the run sent
(`intents`), and the state events are judged by their own sequence, because the Direct API reads
neither a nominated account's state nor an instruction's completion.

Findings have the shape `webhooks.check` returns: rule, subject, detail, expected, actual. `expected`
names the entity, because `Run.account_webhooks` confirms a finding by (rule, expected).
"""

from __future__ import annotations

import json
import re
import time
from decimal import Decimal

from explorer import webhooks

DEPOSIT_KINDS = {"DEPOSIT": ("CREDIT", "DEPOSIT"), "WITHDRAWAL": ("DEBIT", "WITHDRAWAL"),
                 "WITHDRAW": ("DEBIT", "WITHDRAWAL")}
STATES = ("UNVERIFIED", "AWAITING_REVIEW", "VERIFIED", "REJECTED", "NOT_REQUIRED")
BATCH_READ = re.compile(r"^/direct/v1/batches/[^/?]+$")
INSTRUCTION = re.compile(r"^/direct/v1/customers/([^/]+)/accounts/([^/]+)/instruction$")
CLOSE = re.compile(r"^/direct/v1/customers/([^/]+)/accounts/([^/]+)/close")


def intents(calls):
    """What the run asked for, from the calls it made.

    Returns {"instructions": {reference: {kind, amount, customerId, accountReference|accountId}},
    "completed": {reference: amount} for deposit lines the newest batch read shows COMPLETED,
    "pending": customers with a deposit line not yet in a final state, "closing": customers a close
    was sent for}.
    """
    asked, status, pending, closing = {}, {}, set(), set()
    for call in calls:
        if not call.ok:
            continue
        if call.method == "POST" and call.path == "/direct/v1/batches":
            for line in (call.request_body or {}).get("allocations") or []:
                amount = webhooks._amount(line.get("amount"))
                if line.get("instructionReference") and amount is not None:
                    asked[line["instructionReference"]] = {
                        "kind": line.get("instructionType"), "amount": amount,
                        "customerId": line.get("customerId"),
                        "accountReference": line.get("accountReference")}
        elif call.method == "POST" and INSTRUCTION.match(call.path):
            customer_id, account_id = INSTRUCTION.match(call.path).groups()
            body = call.request_body or {}
            amount = webhooks._amount(body.get("amount"))
            if body.get("instructionReference") and amount is not None:
                asked[body["instructionReference"]] = {
                    "kind": body.get("instructionRequestType"), "amount": amount,
                    "customerId": customer_id, "accountId": account_id}
        elif call.method == "POST" and CLOSE.match(call.path):
            closing.add(CLOSE.match(call.path).group(1))
        elif call.method == "GET" and BATCH_READ.match(call.path):
            for row in webhooks._rows(call.body):
                if row.get("instructionReference"):
                    status[row["instructionReference"]] = row.get("status")
    completed = {}
    for reference, line in asked.items():
        if line["kind"] == "DEPOSIT" and status.get(reference) == "COMPLETED":
            completed[reference] = line["amount"]
        elif line["kind"] == "DEPOSIT" and status.get(reference) not in (
                "CANCELLED", "REJECTED"):
            pending.add(line["customerId"])
    return {"instructions": asked, "completed": completed, "pending": pending,
            "closing": closing}


def _payload(event):
    return event["body"].get("payload") or {}


def _signed(payload):
    amount = webhooks._amount(payload.get("amount")) or Decimal("0")
    return amount if payload.get("paymentDirection") == "CREDIT" else -amount


def check(records, world, platform_uid, asked, failed_payouts=(), completed_seen=None,
          grace_seconds=0.0):
    """Returns (findings, stats). `completed_seen` maps a COMPLETED deposit's reference to the
    first moment a sweep saw it so, and is kept by the caller between sweeps. The grace is real
    seconds from that moment, or from the payout failure."""
    real_now = time.time()
    completed_seen = completed_seen if completed_seen is not None else {}
    findings = []

    def found(rule, subject, detail, expected, actual):
        findings.append({"rule": rule, "subject": subject, "detail": detail,
                         "expected": expected, "actual": actual})

    events, _ = webhooks._events(records, platform_uid)
    by_type = {}
    for event in events:
        by_type.setdefault(event["body"].get("type"), []).append(event)
    instructions = asked["instructions"]

    # (a) a SAVINGS_TRANSACTION naming a reference the run sent carries what the run asked for.
    delivered = {}
    for event in by_type.get("SAVINGS_TRANSACTION", []):
        payload = _payload(event)
        line = instructions.get(payload.get("reference"))
        if line is None:
            continue
        delivered.setdefault(payload["reference"], []).append(payload)
        got = (webhooks._amount(payload.get("amount")), payload.get("customerId"))
        want = (line["amount"], line["customerId"])
        direction = DEPOSIT_KINDS.get(line["kind"])
        if direction:
            got += (payload.get("paymentDirection"), payload.get("type"))
            want += direction
        if got != want:
            found("a delivered transaction carries what the run asked for",
                  "instruction {}".format(payload["reference"]),
                  "SAVINGS_TRANSACTION {} for reference {} ({}) disagrees with the {} request "
                  "(amount, customer{})".format(
                      payload.get("transactionId"), payload["reference"], payload.get("type"),
                      line["kind"], ", direction, type" if direction else ""),
                  "instruction {} {}".format(payload["reference"], [str(v) for v in want]),
                  [str(v) for v in got])
    for reference, payloads in delivered.items():
        line = instructions[reference]
        ids = {p.get("transactionId") for p in payloads}
        if line["kind"] != "TRANSFER" and len(ids) > 1:
            found("an instruction books one transaction", "instruction {}".format(reference),
                  "{} {} {} booked {} transactions: {}".format(
                      line["kind"], line["amount"], reference, len(ids), sorted(ids)),
                  "one transaction for instruction {}".format(reference),
                  "{} transactions".format(len(ids)))

    # (b) a deposit the run's newest batch read shows COMPLETED is delivered.
    for reference, amount in asked["completed"].items():
        first = completed_seen.setdefault(reference, real_now)
        if reference in delivered or real_now - first < grace_seconds:
            continue
        found("every deposit the run made is delivered with its amount",
              "instruction {}".format(reference),
              "the deposit {} of {} for customer {} reads COMPLETED and no SAVINGS_TRANSACTION "
              "names it".format(reference, amount, instructions[reference]["customerId"]),
              "one SAVINGS_TRANSACTION of {} for {}".format(amount, reference), "none")

    # (c) on an account the run only deposited into, the deliveries sum to the deposits plus the
    # interest and fees delivered. Any withdrawal, transfer, close or deposit in flight on the
    # customer leaves its accounts out, because the run keeps no record of what those paid.
    other = {line["customerId"] for line in instructions.values()
             if line["kind"] in ("WITHDRAW", "WITHDRAWAL", "TRANSFER")}
    deposits = {}
    for reference, amount in asked["completed"].items():
        line = instructions[reference]
        key = (line["customerId"], line["accountReference"])
        deposits[key] = deposits.get(key, Decimal("0")) + amount
    built = {}
    for event in by_type.get("SAVINGS_TRANSACTION", []):
        payload = _payload(event)
        built.setdefault(payload.get("savingsAccountId"), {})[payload.get("transactionId")] = payload
    judged = 0
    for account_id, account in world["accounts"].items():
        customer_id = account.get("customerId")
        key = (customer_id, account.get("accountReference"))
        if key not in deposits or customer_id in other or customer_id in asked["closing"] \
                or customer_id in asked["pending"] or account_id not in world["transactions"]:
            continue
        txs = built.get(account_id, {}).values()
        unexplained = [p for p in txs if p.get("type") != "DEPOSIT" and p.get("reference")]
        if unexplained or any(
                reference not in delivered for reference, line in instructions.items()
                if (line["customerId"], line.get("accountReference")) == key
                and reference in asked["completed"]):
            continue
        expected = deposits[key] + sum(
            (_signed(p) for p in txs if p.get("type") != "DEPOSIT"), Decimal("0"))
        actual = sum((_signed(p) for p in txs), Decimal("0"))
        judged += 1
        if expected != actual:
            found("the money the run put in matches the balance built from deliveries",
                  "account {}".format(account_id),
                  "account {} ({}) took {} in completed deposits from the run; its deliveries "
                  "sum to {}".format(account_id, account.get("status"), deposits[key], actual),
                  "account {} {}".format(account_id, expected), str(actual))

    # (d) each failed payout the run caused is announced once.
    announced = {}
    for event in by_type.get("PAYOUT_FAILED", []):
        payload = _payload(event)
        announced.setdefault((payload.get("customerId"), webhooks._amount(payload.get("amount"))),
                             []).append(event)
    owed = {}
    for failure in failed_payouts:
        owed.setdefault((failure["customerId"], webhooks._amount(failure["amount"])),
                        []).append(failure)
    for (customer_id, amount), failures in owed.items():
        sent = announced.get((customer_id, amount), [])
        if real_now - max(f["at"] for f in failures) < grace_seconds and len(sent) < len(failures):
            continue
        if len(sent) != len(failures):
            found("each failed payout is announced once", "customer {}".format(customer_id),
                  "the run failed {} payout(s) of {} for customer {} and {} PAYOUT_FAILED name "
                  "that customer and amount".format(len(failures), amount, customer_id,
                                                    len(sent)),
                  "{} PAYOUT_FAILED of {} for customer {}".format(len(failures), amount,
                                                                   customer_id),
                  "{}".format(len(sent)))

    # (e) nothing but ACTIVATED follows CLOSED (CustomerStatusUpdater refuses any other change). The
    # same state twice in a row is only counted: DefaultCustomerVerificationService writes a
    # compliance PENDING without a webhook, so ACTIVATED, PENDING, ACTIVATED announces ACTIVATED twice.
    sequence, repeats = {}, 0
    for event in by_type.get("CUSTOMER_STATE_CHANGED", []):
        payload = _payload(event)
        sequence.setdefault(payload.get("customerId"), []).append(
            (str(payload.get("updatedAt")), event.get("receivedAt", 0),
             payload.get("customerStatus"), event))
    for customer_id, steps in sequence.items():
        steps.sort(key=lambda s: s[:2])
        for before, after in zip(steps, steps[1:]):
            repeats += after[2] == before[2]
            if before[2] == "CLOSED" and after[2] != "ACTIVATED":
                found("nothing is announced for a closed customer but ACTIVATED",
                      "customer {}".format(customer_id),
                      "CUSTOMER_STATE_CHANGED names {} after CLOSED; {}".format(
                          after[2], webhooks.where_delivered(after[3])),
                      "customer {} ACTIVATED or silent after CLOSED".format(customer_id),
                      after[2])
                break

    # (f) the same for a nominated account's state, which the Direct API does not read.
    payees = {}
    for event in by_type.get("NOMINATED_ACCOUNT_STATE_CHANGED", []):
        payload = _payload(event)
        payees.setdefault(payload.get("nominatedAccountId"), []).append(
            (event.get("receivedAt", 0), payload.get("state"), event))
    for payee_id, steps in payees.items():
        steps.sort(key=lambda s: s[:2])
        for before, after in zip(steps, steps[1:]):
            if after[1] == before[1] or after[1] not in STATES:
                found("a nominated account state is announced only when it changes",
                      "nominated account {}".format(payee_id),
                      "NOMINATED_ACCOUNT_STATE_CHANGED names {} after {}; {}".format(
                          after[1], before[1], webhooks.where_delivered(after[2])),
                      "nominated account {} announces a state other than {} next".format(
                          payee_id, before[1]), after[1])
                break

    # (g) one announcement carries one event: the same body under two X-Request-IDs is two sends.
    # A transaction is judged by its id above. The same X-Request-ID twice is at-least-once
    # delivery, which the sender allows when it could not record the answer, so it is counted only.
    bodies = {}
    for event in events:
        kind = event["body"].get("type")
        if kind != "SAVINGS_TRANSACTION":
            bodies.setdefault((kind, json.dumps(_payload(event), sort_keys=True)), []).append(event)
    for (kind, text), copies in bodies.items():
        if len(copies) > 1:
            payload = json.loads(text)
            entity = payload.get("customerId") or payload.get("savingsAccountId")
            found("one event is announced under one id", "{} {}".format(kind, entity),
                  "{} deliveries of one {} body under different X-Request-IDs: {}".format(
                      len(copies), kind, "; ".join(webhooks.where_delivered(e) for e in copies)),
                  "one {} for {} {}".format(kind, entity, payload.get("updatedAt")),
                  "{} deliveries".format(len(copies)))
    answered, again = {}, 0
    for record in records:
        request_id = (record.get("headers") or {}).get("X-Request-ID")
        if record.get("platformUid") != platform_uid or not request_id:
            continue
        if answered.get(request_id) == 200:
            again += 1
        answered[request_id] = record.get("answered")

    stats = {"depositsAsked": sum(1 for l in instructions.values() if l["kind"] == "DEPOSIT"),
             "depositsCompleted": len(asked["completed"]),
             "withdrawalsAsked": sum(1 for l in instructions.values()
                                     if l["kind"] in ("WITHDRAW", "WITHDRAWAL")),
             "tiedToInstruction": sum(len(v) for v in delivered.values()),
             "depositOnlyAccountsJudged": judged, "failedPayoutsOwed": len(failed_payouts),
             "answeredThenRedelivered": again, "customerStateRepeats": repeats, "oracleFindings": len(findings)}
    return findings, stats
