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
import unicodedata
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation

from explorer import webhooks

DEPOSIT_KINDS = {"DEPOSIT": ("CREDIT", "DEPOSIT"), "WITHDRAWAL": ("DEBIT", "WITHDRAWAL"),
                 "WITHDRAW": ("DEBIT", "WITHDRAWAL")}
STATES = ("UNVERIFIED", "AWAITING_REVIEW", "VERIFIED", "REJECTED", "NOT_REQUIRED")
BATCH_READ = re.compile(r"^/direct/v1/batches/[^/?]+$")
INSTRUCTION = re.compile(r"^/direct/v1/customers/([^/]+)/accounts/([^/]+)/instruction$")
CLOSE = re.compile(r"^/direct/v1/customers/([^/]+)/accounts/([^/]+)/close")
CUSTOMER = re.compile(r"^/direct/v1/customers/([^/?]+)$")
NOMINATED = re.compile(r"^/direct/v1/customers/([^/]+)/nominated-account$")
# PaymentPendingAllocationReason, with the message PlatformWebhookCreator writes for each.
PENDING_REASONS = ("BELOW_PRODUCT_MINIMUM", "NO_ASSOCIATED_REQUEST")
NO_REQUEST_MESSAGE = "Term product deposits must be accompanied by account opening requests."
BELOW_MINIMUM = re.compile(r"^Cash balance (\S+) does not meet minimum (\S+)$")
AER_TOLERANCE = Decimal("0.0002")


def _note_customer_write(call, created, nominated, updates):
    body = call.request_body if isinstance(call.request_body, dict) else {}
    if call.method == "POST" and call.path == "/direct/v1/customers" and call.ok \
            and isinstance(call.body, dict) and call.body.get("customerId"):
        created[call.body["customerId"]] = {
            "customerReference": body.get("customerReference"),
            "accountHolderType": body.get("accountHolderType")}
        nominated.setdefault(call.body["customerId"], []).extend(
            body.get("nominatedAccounts") or [])
    elif call.method == "PATCH" and NOMINATED.match(call.path):
        if isinstance(body.get("nominatedAccount"), dict):
            nominated.setdefault(NOMINATED.match(call.path).group(1), []).append(
                body["nominatedAccount"])
    elif call.method == "PUT" and CUSTOMER.match(call.path):
        customer_id = CUSTOMER.match(call.path).group(1)
        updates[customer_id] = updates.get(customer_id, 0) + 1
        nominated.setdefault(customer_id, []).extend(body.get("nominatedAccounts") or [])


def intents(calls):
    """What the run asked for, from the calls it made.

    Returns {"instructions": {reference: {kind, amount, customerId, accountReference|accountId}},
    "completed": {reference: amount} for deposit lines the newest batch read shows COMPLETED,
    "pending": customers with a deposit line not yet in a final state, "closing": customers a close
    was sent for, "created": {customerId: {customerReference, accountHolderType}} for customers the
    run created, "nominated": {customerId: [account bodies the run sent]}, "updates": {customerId:
    PUT count}}. A write that answered 5xx may have committed, so the last three count it too.
    """
    asked, status, pending, closing = {}, {}, set(), set()
    created, nominated, updates = {}, {}, {}
    for call in calls:
        if not 400 <= call.status < 500:
            _note_customer_write(call, created, nominated, updates)
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
            "closing": closing, "created": created, "nominated": nominated, "updates": updates}


def _payload(event):
    return event["body"].get("payload") or {}


def _signed(payload):
    amount = webhooks._amount(payload.get("amount")) or Decimal("0")
    return amount if payload.get("paymentDirection") == "CREDIT" else -amount


def announced_products(records, platform_uid):
    """The products the platform's INTEREST_RATE_CHANGED deliveries name, for the caller to read."""
    events, _ = webhooks._events(records, platform_uid)
    return sorted({_payload(e).get("productId") for e in events
                   if e["body"].get("type") == "INTEREST_RATE_CHANGED"} - {None})


def _text(value):
    """A name as the API stores it: composed, with format characters (a right-to-left override)
    dropped and the ends trimmed."""
    text = unicodedata.normalize("NFC", str(value or ""))
    return "".join(c for c in text if unicodedata.category(c) != "Cf").strip()


def _nominee(account):
    uk = account.get("ukAccountDetails") or {}
    address = account.get("accountHolderAddress") or {}
    return (_text(account.get("accountName")), account.get("currency"),
            re.sub(r"\D", "", str(uk.get("sortCode") or "")), str(uk.get("accountNumber") or ""),
            _text(address.get("addressLine1")), _text(address.get("postCode")))


def _iban_valid(iban):
    if not re.fullmatch(r"[A-Z]{2}\d\d[A-Z0-9]{11,30}", iban or ""):
        return False
    return int("".join(str(int(c, 36)) for c in iban[4:] + iban[:4])) % 97 == 1


def _day(stamp):
    try:
        return date.fromisoformat(str(stamp)[:10])
    except ValueError:
        return None


def _decimal(value):
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def values(by_type, world, asked, products):
    """What the payloads of seven event types say, held up against the run's requests and reads.

    Returns (findings, stats), where the stats count each type's deliveries judged and skipped. A
    delivery is skipped when the run cannot know its expected value: a customer an earlier run
    made, or a product whose rate history the read no longer shows.

    REJECTED_TRANSACTION, KYC_INFO_REQUIRED and PAYMENT_PENDING_ALLOCATION are only judged against
    their own definition, because nothing the run does causes them: a rejected transaction is an
    incoming payment the bank returns, KycDocumentsRequestHandler answers an operator's document
    request, and DirectTransactionDepositHandler raises the pending allocation for a customer
    cash account, which a POOLED Direct platform has none of.
    """
    findings, stats = [], {}
    customers = world["customers"]

    def found(rule, subject, detail, expected, actual):
        findings.append({"rule": rule, "subject": subject, "detail": detail,
                         "expected": expected, "actual": actual})

    def tally(kind, judged):
        key = "{}{}".format(kind, "Judged" if judged else "Skipped")
        stats[key] = stats.get(key, 0) + 1

    # ACCOUNT_CREATED names the customer by the reference and type the run gave, and carries a
    # valid GB IBAN over its own sort code and account number.
    for event in by_type.get("ACCOUNT_CREATED", []):
        payload = _payload(event)
        customer_id = payload.get("customerId")
        made = asked["created"].get(customer_id)
        read = customers.get(customer_id)
        details = payload.get("accountDetails") or {}
        uk = details.get("ukAccountDetails") or {}
        iban = (details.get("globalAccountDetails") or {}).get("accountIdentifier")
        problems = []
        if made or read:
            reference = (made or {}).get("customerReference") or (read or {}).get(
                "customerReference")
            holder = (made or {}).get("accountHolderType") or (read or {}).get(
                "accountHolderType")
            if reference and payload.get("externalId") != reference:
                problems.append(("externalId", reference, payload.get("externalId")))
            if holder and str(payload.get("customerType")).upper() != str(holder).upper():
                problems.append(("customerType", holder, payload.get("customerType")))
        tail = "{}{}".format(uk.get("sortCode"), uk.get("accountNumber"))
        if not _iban_valid(iban) or not iban.endswith(tail):
            problems.append(("accountDetails", "a valid IBAN ending {}".format(tail), iban))
        tally("accountCreated", bool(made or read))
        for field, want, got in problems:
            found("an account created announcement carries the customer's own details",
                  "customer {}".format(customer_id),
                  "ACCOUNT_CREATED for customer {} has {} {!r}, expected {!r}; {}".format(
                      customer_id, field, got, want, webhooks.where_delivered(event)),
                  "customer {} {} {}".format(customer_id, field, want), str(got))

    # CUSTOMER_DATA_CHANGED carries only the customer id. CustomerCreationAuditor sends one when the
    # customer is created and CustomerUpdateServiceDefault one per PUT, so a customer the run made
    # has at most 1 plus its PUTs. A shortfall is not judged, because a PUT may send none.
    sent = {}
    for event in by_type.get("CUSTOMER_DATA_CHANGED", []):
        sent.setdefault(_payload(event).get("customerId"), []).append(event)
    for customer_id, events in sent.items():
        if customer_id not in asked["created"]:
            tally("customerDataChanged", False)
            continue
        tally("customerDataChanged", True)
        allowed = 1 + asked["updates"].get(customer_id, 0)
        if len(events) > allowed:
            found("a customer data change is announced only for a change the run made",
                  "customer {}".format(customer_id),
                  "{} CUSTOMER_DATA_CHANGED for customer {}, which the run created and updated {} "
                  "time(s): {}".format(len(events), customer_id, allowed - 1,
                                       "; ".join(webhooks.where_delivered(e) for e in events)),
                  "at most {} CUSTOMER_DATA_CHANGED for customer {}".format(allowed, customer_id),
                  "{} deliveries".format(len(events)))

    # NOMINATED_ACCOUNT_ADDED names an account the run sent for that customer.
    for event in by_type.get("NOMINATED_ACCOUNT_ADDED", []):
        payload = _payload(event)
        customer_id = payload.get("customerId")
        if customer_id not in asked["created"]:
            tally("nominatedAccountAdded", False)
            continue
        tally("nominatedAccountAdded", True)
        sent_accounts = [_nominee(a) for a in asked["nominated"].get(customer_id, [])]
        got = _nominee(payload.get("externalBankAccount") or {})
        if got not in sent_accounts:
            found("a nominated account announcement carries an account the run nominated",
                  "customer {}".format(customer_id),
                  "NOMINATED_ACCOUNT_ADDED for customer {} names {} (name, currency, sort code, "
                  "account number, address line 1, post code), which no request the run sent for "
                  "that customer holds; {}".format(customer_id, list(got),
                                                   webhooks.where_delivered(event)),
                  "customer {} one of {}".format(customer_id, [list(a) for a in sent_accounts]),
                  list(got))

    for event in by_type.get("REJECTED_TRANSACTION", []):
        payload = _payload(event)
        amount = _decimal(payload.get("amount"))
        broken = [name for name, ok in (
            ("an amount above 0", amount is not None and amount > 0),
            ("a 3 letter currency", bool(re.fullmatch(r"[A-Z]{3}", str(payload.get("currency"))))),
            ("a reference", bool(payload.get("reference")))) if not ok]
        tally("rejectedTransaction", True)
        if broken:
            found("a webhook carries values its payload definition allows",
                  "customer {}".format(payload.get("customerId")),
                  "REJECTED_TRANSACTION lacks {}: {}; {}".format(
                      " and ".join(broken), json.dumps(payload, sort_keys=True),
                      webhooks.where_delivered(event)),
                  "REJECTED_TRANSACTION with an amount above 0, a 3 letter currency and a "
                  "reference", json.dumps(payload, sort_keys=True))
    for event in by_type.get("KYC_INFO_REQUIRED", []):
        payload = _payload(event)
        tally("kycInfoRequired", True)
        try:
            person_ok = payload.get("personId") is None or bool(uuid.UUID(str(payload["personId"])))
        except ValueError:
            person_ok = False
        if not payload.get("personExternalId") or not person_ok:
            found("a webhook carries values its payload definition allows",
                  "customer {}".format(payload.get("customerId")),
                  "KYC_INFO_REQUIRED has personExternalId {!r} and personId {!r}; {}".format(
                      payload.get("personExternalId"), payload.get("personId"),
                      webhooks.where_delivered(event)),
                  "KYC_INFO_REQUIRED with a personExternalId and a UUID personId",
                  json.dumps(payload, sort_keys=True))
    for event in by_type.get("PAYMENT_PENDING_ALLOCATION", []):
        payload = _payload(event)
        reason, message = payload.get("reason"), str(payload.get("message"))
        tally("paymentPendingAllocation", True)
        below = BELOW_MINIMUM.match(message)
        balance, minimum = (_decimal(below.group(1)), _decimal(below.group(2))) if below \
            else (None, None)
        agrees = (reason == "NO_ASSOCIATED_REQUEST" and message == NO_REQUEST_MESSAGE) or (
            reason == "BELOW_PRODUCT_MINIMUM" and balance is not None and minimum is not None
            and balance < minimum)
        if not agrees:
            found("a webhook carries values its payload definition allows",
                  "customer {}".format(payload.get("customerId")),
                  "PAYMENT_PENDING_ALLOCATION says {} with message {!r}; {}".format(
                      reason, message, webhooks.where_delivered(event)),
                  "reason {} with the message written for it, or {} with a balance below the "
                  "minimum".format(PENDING_REASONS[1], PENDING_REASONS[0]),
                  json.dumps(payload, sort_keys=True))

    # INTEREST_RATE_CHANGED (version 1) carries the product's reduced gross rate and AER on the day
    # it was sent. The read lists only the slices not ended before today, so that day's rate is
    # held up against it while the read still holds the slice that covered the day: the oldest
    # listed slice began two days or more before the delivery, none begins within a day of it, and
    # none was announced after it.
    for event in by_type.get("INTEREST_RATE_CHANGED", []):
        payload = _payload(event)
        product = products.get(payload.get("productId"))
        first_seen = str(event["body"].get("firstSeen"))
        sent_on = _day(first_seen)
        gross, aer = _decimal(payload.get("currentGrossRate")), _decimal(
            payload.get("currentAerRate"))
        slices = [d for d in (product or {}).get("rateDetails") or [] if _day(d.get("startDate"))]
        covering = [d for d in slices if sent_on and _day(d["startDate"]) <= sent_on
                    and (not d.get("endDate") or _day(d["endDate"]) >= sent_on)]
        starts = [_day(d["startDate"]) for d in slices]
        if (product is None or sent_on is None or gross is None or aer is None or not covering
                or (sent_on - min(starts)).days < 2
                or any(abs((s - sent_on).days) <= 1 for s in starts)
                or any(str(d.get("announcedAt"))[:19] > first_seen[:19] for d in slices)):
            tally("interestRateChanged", False)
            continue
        tally("interestRateChanged", True)
        piece = max(covering, key=lambda d: _day(d["startDate"]))
        want_gross, want_aer = _decimal(piece.get("grossRate")), _decimal(piece.get("aerRate"))
        problems = []
        if want_gross is None or gross != want_gross:
            problems.append(("currentGrossRate", want_gross, gross))
        if want_aer is None or abs(aer - want_aer) > AER_TOLERANCE:
            problems.append(("currentAerRate", want_aer, aer))
        if payload.get("productName") != product.get("name"):
            problems.append(("productName", product.get("name"), payload.get("productName")))
        for field, want, got in problems:
            found("an interest rate announcement carries the product's rates",
                  "product {}".format(payload.get("productId")),
                  "INTEREST_RATE_CHANGED for product {} says {} {}; the product read shows {} for "
                  "the slice starting {}; {}".format(
                      payload.get("productId"), field, got, want, piece["startDate"],
                      webhooks.where_delivered(event)),
                  "product {} {} {}".format(payload.get("productId"), field, want), str(got))
    return findings, stats


def check(records, world, platform_uid, asked, failed_payouts=(), completed_seen=None,
          grace_seconds=0.0, products=None):
    """Returns (findings, stats). `completed_seen` maps a COMPLETED deposit's reference to the
    first moment a sweep saw it so, and is kept by the caller between sweeps. The grace is real
    seconds from that moment, or from the payout failure. `products` maps a product id to its
    Direct API read, for the products `announced_products` names."""
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

    judged_values, value_stats = values(by_type, world, asked, products or {})
    findings.extend(judged_values)

    stats = {"depositsAsked": sum(1 for l in instructions.values() if l["kind"] == "DEPOSIT"),
             "depositsCompleted": len(asked["completed"]),
             "withdrawalsAsked": sum(1 for l in instructions.values()
                                     if l["kind"] in ("WITHDRAW", "WITHDRAWAL")),
             "tiedToInstruction": sum(len(v) for v in delivered.values()),
             "depositOnlyAccountsJudged": judged, "failedPayoutsOwed": len(failed_payouts),
             "answeredThenRedelivered": again, "customerStateRepeats": repeats, "oracleFindings": len(findings)}
    stats.update(value_stats)
    return findings, stats
