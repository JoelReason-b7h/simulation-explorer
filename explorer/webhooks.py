"""Webhook subscription, capture and accounting for the Direct platforms a cycle stands up.

Each platform is subscribed once, at standup, through ops-api's `POST /webhook/link`, for every
event type the Direct audience delivers, to a plain http address on this machine with the platform
uid in the path. The public `/direct/v1/webhooks` subscription requires https and is not used.

The receiver runs beside the token server in the fleet cycle's process and writes every delivery
to `<cycle>.webhooks.jsonl`. Each run's explore.py reads that file and holds the deliveries for its
own platform up against the Direct API's reads (`check`).

The sender accepts a delivery only when the answer carries the nonce it sent and
sha256("<nonce>:<shared key>") in hex (WebhookSender.validateResponse, ResponseSigner). Any other
answer leaves the event AWAITING_RESPONSE and it is sent again, so the receiver signs with the
platform's `platform_client_link.shared_key`, read once per platform.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from explorer import clock

PORT = int(os.environ.get("SIM_WEBHOOK_PORT", "8432"))
# The services run in docker and reach this machine by this name, as they reach the token server.
SERVICE_HOST = "host.docker.internal"
CORE_DSN = os.environ.get("SIM_CORE_DSN", "postgresql://core:password@localhost:5432/core")
# ExternalDirectWebhookVersion.VERSION_1.toInternal(). The ops endpoint takes the internal enum by
# name. A subscription at another version is never matched by the sender, and no event is written.
VERSION = "VERSION_1_2"
# ExternalDirectWebhookEventType mapped through toInternal(): the whole Direct vocabulary.
EVENT_TYPES = (
    "CUSTOMER_STATE_CHANGED", "SAVINGS_TRANSACTION", "INTEREST_REALISED", "REJECTED_TRANSACTION",
    "NOMINATED_ACCOUNT_CHANGED", "ACCOUNT_CREATED", "KYC_INFO_REQUIRED", "CUSTOMER_DATA_CHANGED",
    "CUSTOMER_DOCUMENT_CREATED", "PAYMENT_PENDING_ALLOCATION", "CASH_ACCOUNT_PAYOUT_FAILED",
    "ACCOUNT_CLOSED", "NOMINATED_ACCOUNT_STATE_CHANGED", "INTEREST_RATE_CHANGED",
)
HEADERS = ("X-Request-ID", "X-Webhook-Nonce", "X-Webhook-Signature", "X-Webhook-Key-Id",
           "Content-Type", "User-Agent")
# A cycle's capture stops growing here. One delivery is under 1 KB, so this holds about 60000.
MAX_CAPTURE_BYTES = int(os.environ.get("SIM_WEBHOOK_CAPTURE_MAX_BYTES", str(64 * 1024 * 1024)))
TRUNCATED = "capture truncated"


def url_for(platform_uid):
    return "http://{}:{}/webhook/{}".format(SERVICE_HOST, PORT, platform_uid)


def capture_path(here, run_name):
    return Path(here) / "{}.webhooks.jsonl".format(run_name)


# --- subscription ---------------------------------------------------------------------------


def subscribe(ops, platform_uid):
    """Subscribe the platform to every event type. Returns (subscribed types, refusals)."""
    url = url_for(platform_uid)
    done, refused = [], []
    for event in EVENT_TYPES:
        call = ops.call("POST", "/webhook/link", json_body={
            "platformId": platform_uid, "webhookEventType": event, "url": url,
            "version": VERSION})
        if call.ok:
            done.append(event)
        else:
            refused.append("{} {} {}".format(event, call.status, json.dumps(call.body)[:200]))
    return done, refused


def subscribed(ops, platform_uid):
    """The event types ops-api reads back for the platform at this receiver's url and version."""
    call = ops.call("GET", "/webhook/{}".format(platform_uid))
    if not call.ok or not isinstance(call.body, dict):
        return None
    url = url_for(platform_uid)
    return sorted({w.get("webhookName") for w in call.body.get("webhooks") or []
                   if w.get("url") == url and w.get("webhookVersion") == VERSION})


def outstanding(ops, platform_uid):
    """How many of the platform's webhook events still wait for an answer, or None if unreadable."""
    call = ops.call("GET", "/webhook/events", params={
        "platformUid": platform_uid, "eventState": "AWAITING_RESPONSE", "take": 50})
    if not call.ok or not isinstance(call.body, list):
        return None
    return len(call.body)


def outstanding_transactions(ops, platform_uid):
    """Transaction ids whose SAVINGS_TRANSACTION event core still holds AWAITING_RESPONSE.

    Core's resend job would send these again, but it is a shedlock scheduler and off locally, and
    ops-api's PUT /webhook/{uid}/resend only moves the row's timestamps back for that job to find:
    nineteen PUTs on one event in fleet 185 sent nothing and left it dated before its transaction.
    """
    call = ops.call("GET", "/webhook/events", params={
        "platformUid": platform_uid, "eventState": "AWAITING_RESPONSE", "take": 500})
    if not call.ok or not isinstance(call.body, list):
        return frozenset()
    # Customer ids too: fleet 200's CANCELLED CUSTOMER_STATE_CHANGED sat AWAITING_RESPONSE for an
    # hour after the ACTIVATED one was delivered, so the last state received was one step behind.
    # And accounts: at the long-lived bank's volume core's delivery pool saturates and refuses
    # about 7% of events ("Webhook delivery pool saturated"), leaving each for the resend job.
    keys = {"SAVINGS_TRANSACTION": "transactionId", "CUSTOMER_STATE_CHANGED": "customerId",
            "ACCOUNT_CLOSED": "savingsAccountId"}
    ids = set()
    for event in call.body:
        key = keys.get(event.get("eventType"))
        if key is None:
            continue
        try:
            ids.add(json.loads(event.get("eventPayload") or "{}")["payload"][key])
        except (ValueError, KeyError, TypeError):
            continue
    return frozenset(ids)


# --- receiver -------------------------------------------------------------------------------


class _Capture:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.keys = {}
        self.truncated = False

    def shared_key(self, platform_uid):
        if platform_uid in self.keys:
            return self.keys[platform_uid]
        uuid.UUID(platform_uid)
        done = subprocess.run(
            ["psql", CORE_DSN, "-tA", "-c",
             "SELECT pcl.shared_key FROM platform_client_link pcl JOIN partner_platform pp "
             "ON pp.sid = pcl.platform_sid WHERE pp.uid = '{}'".format(platform_uid)],
            capture_output=True, text=True, timeout=30)
        rows = [r for r in done.stdout.splitlines() if r.strip()]
        key = rows[0] if len(rows) == 1 else None
        if key:
            self.keys[platform_uid] = key
        return key

    def write(self, record):
        line = json.dumps(record, sort_keys=True) + "\n"
        with self.lock:
            if self.truncated:
                return
            size = self.path.stat().st_size if self.path.exists() else 0
            if size + len(line) > MAX_CAPTURE_BYTES:
                self.truncated = True
                line = json.dumps({"receivedAt": time.time(), "error": TRUNCATED}) + "\n"
            with open(self.path, "a") as handle:
                handle.write(line)


def _handler(capture):
    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.1 keeps the connection core's client pools. Under HTTP/1.0 the handler closed
        # each connection after answering, and core lost requests sent on one it reused.
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode(errors="replace") if length else ""
            parts = self.path.split("?", 1)[0].strip("/").split("/")
            platform_uid = parts[1] if len(parts) == 2 and parts[0] == "webhook" else None
            try:
                body = json.loads(raw)
            except ValueError:
                body = raw
            nonce = self.headers.get("X-Webhook-Nonce") or self.headers.get("Bondsmith-Nonce")
            status, answer, error = 200, None, None
            try:
                key = capture.shared_key(platform_uid) if platform_uid and nonce else None
            except (ValueError, OSError, subprocess.SubprocessError) as fault:
                key, error = None, str(fault)
            if key:
                answer = {"Sign": hashlib.sha256("{}:{}".format(nonce, key).encode()).hexdigest(),
                          "Nonce": nonce}
            else:
                # Refused, so the sender keeps the event and sends it again later.
                status = 503
                error = error or "no platform uid, nonce or shared key"
                answer = {"message": error}
            capture.write({
                "receivedAt": time.time(), "platformUid": platform_uid, "path": self.path,
                "headers": {h: self.headers.get(h) for h in HEADERS if self.headers.get(h)},
                "body": body, "answered": status, "error": error})
            data = json.dumps(answer).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


class _Server(ThreadingHTTPServer):
    # The default backlog of 5 dropped deliveries when four platforms sent at once: core timed
    # out after 10 s on two SAVINGS_TRANSACTION events the handler never saw.
    request_queue_size = 128
    daemon_threads = True


def serve(path, port=PORT):
    """Start the receiver on a daemon thread, writing to `path`, and return the server."""
    server = _Server(("0.0.0.0", port), _handler(_Capture(path)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def ensure_serving(path, port=PORT):
    """Start the receiver, or return None and say why when the port is taken."""
    try:
        return serve(path, port)
    except OSError as fault:
        print("  the webhook receiver did not start on port {}: {}".format(port, fault))
        return None


# --- accounting -----------------------------------------------------------------------------


def load(path):
    """Every record in the capture, and whether the capture stopped growing at its size limit."""
    records, truncated = [], False
    path = Path(path)
    if not path.exists():
        return records, truncated
    with open(path) as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("error") == TRUNCATED:
                truncated = True
                continue
            records.append(record)
    return records, truncated


def _amount(value):
    try:
        return abs(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _rows(body):
    if isinstance(body, dict):
        return body.get("content") or []
    return body if isinstance(body, list) else []


def _age_seconds(stamp, now):
    # Python 3.9's fromisoformat takes a fraction of exactly 3 or 6 digits, and the services drop
    # trailing zeros, so the fraction is padded to 6 digits first.
    match = re.match(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)?$",
                     str(stamp))
    if not match:
        return None
    zone = match.group(3) or "Z"
    text = "{}.{}{}".format(match.group(1), (match.group(2) or "0").ljust(6, "0")[:6],
                            "+00:00" if zone == "Z" else zone)
    try:
        return now - datetime.fromisoformat(text).timestamp()
    except (TypeError, ValueError):
        return None


def read_world(client, not_before=None):
    """What the Direct API shows for the platform, as plain dicts. None when a listing fails.

    A closed customer's accounts and transactions answer 400 "Customer not found" while the
    customer listing still names it, so those accounts are counted as unreadable, not as empty.
    """
    customers, skip = {}, 0
    while True:
        call = client.call("GET", "/direct/v1/customers?skip={}&take=100".format(skip))
        if not call.ok:
            return None
        chunk = _rows(call.body)
        for row in chunk:
            customers[row.get("customerId")] = row
        skip += len(chunk)
        total = call.body.get("totalSize") if isinstance(call.body, dict) else None
        if not chunk or (total is not None and skip >= total):
            break
    accounts, transactions, unreadable = {}, {}, []
    for customer_id in customers:
        # A customer made before `not_before` belongs to an earlier cycle on a long-lived bank,
        # whose deliveries went to that cycle's capture, so its accounts are not read.
        created = _age_seconds(customers[customer_id].get("createdAt"), clock.time())
        if not_before and created is not None and clock.time() - created < not_before:
            continue
        call = client.call("GET", "/direct/v1/customers/{}/accounts".format(customer_id))
        if not call.ok:
            unreadable.append(customer_id)
            continue
        for account in _rows(call.body):
            account_id = account.get("accountId")
            accounts[account_id] = dict(account, customerId=customer_id)
            rows, readable = [], True
            for page in range(5):
                listing = client.call(
                    "GET", "/direct/v1/customers/{}/accounts/{}/transactions?skip={}&take=1000"
                    .format(customer_id, account_id, page * 1000))
                if not listing.ok:
                    readable = False
                    break
                chunk = _rows(listing.body)
                rows.extend(chunk)
                if len(chunk) < 1000:
                    break
            if readable:
                transactions[account_id] = rows
    return {"customers": customers, "accounts": accounts, "transactions": transactions,
            "unreadable": unreadable}


def _events(records, platform_uid):
    """The platform's deliveries, one per sender event: a redelivery of one X-Request-ID is one."""
    seen, events, redelivered = {}, [], 0
    for record in records:
        if record.get("platformUid") != platform_uid or not isinstance(record.get("body"), dict):
            continue
        request_id = (record.get("headers") or {}).get("X-Request-ID")
        if request_id and request_id in seen:
            redelivered += 1
            continue
        if request_id:
            seen[request_id] = True
        events.append(record)
    return events, redelivered


def where_delivered(event):
    return "delivered {} X-Request-ID {}".format(
        time.strftime("%H:%M:%S", time.localtime(event.get("receivedAt", 0))),
        (event.get("headers") or {}).get("X-Request-ID"))


WITHHELD_SQL = """
  WITH accounts AS (SELECT d.sid FROM direct_customer_account d
                    JOIN customer_product_account a ON a.sid = d.customer_product_account_sid
                    WHERE a.uid IN ({uids})),
  debits AS (
    SELECT i.sid, i.direct_customer_account_sid AS account, i.amount, i.created_at, i.status,
           t.uid, t.customer_amount
    FROM direct_customer_instruction i
    JOIN direct_instruction_subject s ON s.instruction_sid = i.sid
     AND s.transaction_type = 'SAVINGS_WITHDRAWAL'
    JOIN account_transaction t ON t.balance_change_subject_id = s.sid
    WHERE i.direct_customer_account_sid IN (SELECT sid FROM accounts)
      AND NOT EXISTS (SELECT 1 FROM direct_instruction_subject c
                      WHERE c.instruction_sid = i.sid AND c.transaction_type = 'CASH_WITHDRAWAL')),
  rejected AS (
    SELECT d.*, row_number() OVER (PARTITION BY d.account, d.amount ORDER BY d.created_at) AS n
    FROM debits d WHERE d.status = 'CANCELLED'),
  returns AS (
    SELECT i.direct_customer_account_sid AS account, i.amount, t.uid, t.customer_amount,
           row_number() OVER (PARTITION BY i.direct_customer_account_sid, i.amount
                              ORDER BY i.created_at) AS n
    FROM direct_customer_instruction i
    JOIN direct_instruction_subject s ON s.instruction_sid = i.sid
     AND s.transaction_type = 'SAVINGS_DEPOSIT'
    JOIN account_transaction t ON t.balance_change_subject_id = s.sid
    WHERE i.instruction_type = 'DEPOSIT' AND i.direct_batch_instruction_sid IS NULL
      AND i.direct_customer_account_sid IN (SELECT sid FROM accounts)
      AND EXISTS (SELECT 1 FROM rejected r WHERE r.account = i.direct_customer_account_sid
                  AND r.amount = i.amount AND r.created_at < i.created_at))
  SELECT uid, customer_amount FROM debits
  UNION ALL
  SELECT r.uid, r.customer_amount FROM returns r
  JOIN rejected j ON j.account = r.account AND j.amount = r.amount AND j.n = r.n
"""


def withheld_transactions(account_ids):
    """{transaction id: signed amount} for the rows SAV-11198 books with no SAVINGS_TRANSACTION.

    A withdrawal's debit is announced only once its payout settles (CASH_WITHDRAWAL), so a debit
    whose payout is pending or was rejected has no delivery yet, or ever. A rejected payout's
    return to savings is booked with none either (DirectDepositSweepService
    .returnRejectedPayoutToSavings). Core keeps no link from the return to the withdrawal, so a
    return is paired with a rejected withdrawal of the same account and amount, oldest first.
    None when core cannot be read.
    """
    from explorer.interest_oracle import _psql
    uids = [a for a in account_ids if a]
    if not uids:
        return {}
    rows, _ = _psql(WITHHELD_SQL.format(uids=",".join("'{}'".format(u) for u in uids)),
                    timeout=120)
    if rows is None:
        return None
    return {uid: Decimal(amount) for uid, amount in rows}


def check(records, world, platform_uid, minted=None, grace_seconds=0.0, now=None,
          outstanding=frozenset(), withheld=None):
    """Hold the platform's deliveries up against the API's reads.

    Returns (findings, stats). A finding is a dict with rule, subject, detail, expected, actual.
    A transaction younger than `grace_seconds` is left out of the completeness and balance checks,
    because its delivery can still be on the way.
    """
    # The ages are of timestamps the services wrote on their clock; the grace is real seconds.
    now = now or clock.time()
    grace_seconds = clock.system_seconds(grace_seconds)
    minted = minted or {}
    withheld = withheld or {}
    findings = []

    def found(rule, subject, detail, expected, actual):
        findings.append({"rule": rule, "subject": subject, "detail": detail,
                         "expected": expected, "actual": actual})

    events, redelivered = _events(records, platform_uid)
    by_type = {}
    for event in events:
        by_type.setdefault(event["body"].get("type"), []).append(event)
    customers, accounts = world["customers"], world["accounts"]
    transactions = world["transactions"]

    api_tx = {}
    for account_id, rows in transactions.items():
        for row in rows:
            api_tx[row.get("transactionId")] = dict(row, accountId=account_id)

    def where(event):
        payload = event["body"].get("payload") or {}
        reference = payload.get("reference")
        tie = " reference {} minted by this run for {}".format(
            reference, minted[reference]) if reference in minted else (
            " reference {}".format(reference) if reference else "")
        return "delivered {} X-Request-ID {}{}".format(
            time.strftime("%H:%M:%S", time.localtime(event.get("receivedAt", 0))),
            (event.get("headers") or {}).get("X-Request-ID"), tie)

    # (a) each SAVINGS_TRANSACTION delivery matches the API's transaction of that id.
    delivered = {}
    tied = 0
    for event in by_type.get("SAVINGS_TRANSACTION", []):
        payload = event["body"].get("payload") or {}
        tx_id, account_id = payload.get("transactionId"), payload.get("savingsAccountId")
        delivered.setdefault(tx_id, []).append(event)
        if payload.get("reference") in minted:
            tied += 1
        if account_id not in transactions:
            continue
        row = api_tx.get(tx_id)
        mine = (account_id, _amount(payload.get("amount")), payload.get("paymentDirection"),
                payload.get("type"))
        if row is None:
            found("a delivered transaction matches the API's transaction",
                  "transaction {}".format(tx_id),
                  "SAVINGS_TRANSACTION {} for account {} is not in the account's transaction "
                  "list; {}".format(tx_id, account_id, where(event)),
                  "transaction {} listed on account {}".format(tx_id, account_id),
                  "absent; payload {}".format(json.dumps(payload, sort_keys=True)))
            continue
        theirs = (row.get("accountId"), _amount(row.get("amount")), row.get("mark"),
                  row.get("type"))
        if mine != theirs:
            found("a delivered transaction matches the API's transaction",
                  "transaction {}".format(tx_id),
                  "SAVINGS_TRANSACTION {} disagrees with the API read (account, amount, mark, "
                  "type); {}".format(tx_id, where(event)),
                  "transaction {} {}".format(tx_id, [str(v) for v in theirs]),
                  [str(v) for v in mine])
    # INTEREST_REALISED names the same transaction as its SAVINGS_TRANSACTION.
    for event in by_type.get("INTEREST_REALISED", []):
        payload = event["body"].get("payload") or {}
        tx_id, account_id = payload.get("transactionId"), payload.get("savingsAccountId")
        row = api_tx.get(tx_id)
        if account_id not in transactions or row is None and (
                _age_seconds(payload.get("updatedAt"), now) or 0) < grace_seconds:
            continue
        theirs = (row.get("accountId"), _amount(row.get("amount")), row.get("type")) if row \
            else None
        mine = (account_id, _amount(payload.get("interestRealised")), "INTEREST")
        if theirs != mine:
            found("a delivered transaction matches the API's transaction",
                  "transaction {}".format(tx_id),
                  "INTEREST_REALISED {} disagrees with the API read (account, amount, type); "
                  "{}".format(tx_id, where(event)),
                  "transaction {} {}".format(tx_id, [str(v) for v in theirs] if theirs
                                              else "listed"),
                  [str(v) for v in mine])

    # (b) every listed transaction has exactly one delivery.
    young = set()
    for tx_id, row in api_tx.items():
        age = _age_seconds(row.get("createdAt"), now)
        if age is not None and age < grace_seconds:
            young.add(row.get("accountId"))
            continue
        copies = delivered.get(tx_id, [])
        if not copies and tx_id in outstanding:
            # Core still holds the event AWAITING_RESPONSE; a deployed core's resend job sends it
            # again, and locally that job is off, so it is outstanding, not undelivered.
            young.add(row.get("accountId"))
            continue
        if not copies and tx_id in withheld:
            continue
        if not copies:
            found("every transaction the API shows is delivered",
                  "transaction {}".format(tx_id),
                  "{} {} {} on account {} created {} has no SAVINGS_TRANSACTION delivery".format(
                      row.get("mark"), row.get("type"), row.get("amount"), row.get("accountId"),
                      row.get("createdAt")),
                  "one SAVINGS_TRANSACTION for {}".format(tx_id), "none")
        elif len(copies) > 1:
            found("no transaction is delivered twice", "transaction {}".format(tx_id),
                  "{} deliveries of {} under different X-Request-IDs: {}".format(
                      len(copies), tx_id, "; ".join(where(e) for e in copies)),
                  "one SAVINGS_TRANSACTION for {}".format(tx_id),
                  "{} deliveries".format(len(copies)))

    # (c) the balance built from deliveries alone equals the API balance.
    built = {}
    for tx_id, copies in delivered.items():
        payload = copies[0]["body"].get("payload") or {}
        amount = _amount(payload.get("amount")) or Decimal("0")
        sign = 1 if payload.get("paymentDirection") == "CREDIT" else -1
        account_id = payload.get("savingsAccountId")
        built[account_id] = built.get(account_id, Decimal("0")) + sign * amount
    # A withheld row moves the balance with no delivery, so it is added to what was delivered.
    for tx_id, amount in withheld.items():
        row = api_tx.get(tx_id)
        if row is not None and tx_id not in delivered:
            built[row["accountId"]] = built.get(row["accountId"], Decimal("0")) + amount
    balanced = 0
    for account_id, account in accounts.items():
        if account_id not in transactions or account_id in young:
            continue
        try:
            balance = Decimal(str(account.get("balance") or "0"))
        except (InvalidOperation, ValueError):
            continue
        mine = built.get(account_id, Decimal("0"))
        if mine != balance:
            found("the balance built from deliveries equals the API balance",
                  "account {}".format(account_id),
                  "account {} ({}) reads balance {}; its SAVINGS_TRANSACTION deliveries sum to "
                  "{}".format(account_id, account.get("status"), balance, mine),
                  "account {} balance {}".format(account_id, balance), str(mine))
        else:
            balanced += 1

    # (d) state webhooks agree with the reads.
    closed_delivered = {(e["body"].get("payload") or {}).get("savingsAccountId"): e
                        for e in by_type.get("ACCOUNT_CLOSED", [])}
    for account_id, account in accounts.items():
        if account.get("status") == "CLOSED" and account_id not in closed_delivered \
                and account_id not in outstanding:
            found("each closed account is announced", "account {}".format(account_id),
                  "account {} reads CLOSED and no ACCOUNT_CLOSED names it".format(account_id),
                  "ACCOUNT_CLOSED for {}".format(account_id), "none")
    for account_id, event in closed_delivered.items():
        account = accounts.get(account_id)
        if account is not None and account.get("status") != "CLOSED":
            found("each closed account is announced", "account {}".format(account_id),
                  "ACCOUNT_CLOSED names account {}, which reads {}; {}".format(
                      account_id, account.get("status"), where(event)),
                  "account {} CLOSED".format(account_id), account.get("status"))
    last_state = {}
    for event in by_type.get("CUSTOMER_STATE_CHANGED", []):
        payload = event["body"].get("payload") or {}
        order = (str(payload.get("updatedAt")), event.get("receivedAt", 0))
        held = last_state.get(payload.get("customerId"))
        if held is None or order >= held[0]:
            last_state[payload.get("customerId")] = (order, payload.get("customerStatus"), event)
    for customer_id, (_, status, event) in last_state.items():
        customer = customers.get(customer_id)
        if customer is not None and customer.get("customerStatus") != status \
                and customer_id not in outstanding:
            found("the last customer state delivered matches the customer read",
                  "customer {}".format(customer_id),
                  "the last CUSTOMER_STATE_CHANGED for {} says {}; {}".format(
                      customer_id, status, where(event)),
                  "customer {} {}".format(customer_id, customer.get("customerStatus")), status)

    # (e) a delivery on this platform's url names only this platform's customers and accounts.
    mine_customers = set(customers)
    mine_accounts = set(accounts)
    for event in events:
        payload = event["body"].get("payload") or {}
        customer_id = payload.get("customerId")
        if customer_id and customer_id not in mine_customers:
            found("a delivery names only its own platform's entities",
                  "customer {}".format(customer_id),
                  "{} on platform {}'s url names customer {}, which this platform's customer "
                  "listing does not hold; {}".format(event["body"].get("type"), platform_uid,
                                                    customer_id, where(event)),
                  "customer of platform {}".format(platform_uid), "not listed")
    for record in records:
        if record.get("platformUid") in (platform_uid, None) or not isinstance(
                record.get("body"), dict):
            continue
        payload = record["body"].get("payload") or {}
        named = {payload.get("customerId")} & mine_customers or {
            payload.get("savingsAccountId")} & mine_accounts
        if named:
            found("a delivery names only its own platform's entities",
                  "{}".format(named.pop()),
                  "{} delivered on platform {}'s url names an entity of platform {}".format(
                      record["body"].get("type"), record.get("platformUid"), platform_uid),
                  "delivered on platform {}'s url".format(platform_uid),
                  "platform {}".format(record.get("platformUid")))

    stats = {
        "deliveries": len(events), "redelivered": redelivered,
        "byType": {k: len(v) for k, v in sorted(by_type.items(), key=lambda kv: str(kv[0]))},
        "apiTransactions": len(api_tx), "tiedToMintedReference": tied,
        "accountsBalanced": balanced, "unreadableCustomers": len(world["unreadable"]),
        "youngAccountsSkipped": len(young), "findings": len(findings),
    }
    return findings, stats
