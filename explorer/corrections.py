"""The payment-management corrections an operator makes by hand, driven through ops-api.

Every route here is four-eyes: `POST /operations/own/payment/management/<route>` stores a request
(`FourEyeService.request`), and a different ops user approves it with
`POST .../payment/approval` (`FourEyeService.approveRequest` -> `checkApproverIsNotRequester`,
then `PaymentManagementService.actionThePayload`). The run's own ops token is the requester; a
second local-auth user (`SECOND_OPS_USER`, the same `admin` role, which carries PAYMENT_AUTHORISER)
approves. Nothing here runs without local auth, because a deployed Cognito pool has no second user.

The sources the box runs (2fb856f49e):
- request routes: OpsPortalPaymentManagementController.java; bodies in api-model
  `FundingRecordSplitRequest`, `ReturnTransactionRequest`, `CustomerProductAccountAdjustmentRequest`.
- approval: `PaymentManagementService.approve` -> `FourEyeService.approveRequest`. A throwing action
  does not fail the call: it marks the request FAILED and answers 200, so the state is read back.
- matching routes: OpsPortalMatchingController.java, answered by clearing's PaymentMatchingController
  (`paymentMatchRunner.run(paymentMatcherChooser.chooseMatcherManually(...))`). No approval.

What each correction is meant to do:
- funding/split: `FundingRecordRepository.splitFundingRecord` inserts a NEW funding record of
  `valueToExtract` (new uid, same account, reference, direction, mark) and lowers the original by
  the same amount. `validate` refuses an extract that is not strictly below the value, and a record
  that is already linked.
- return: `RejectedTransactionResolutionService.returnTransaction` publishes one payment group that
  pays the credit's amount back to its counterpart and marks the partner payment RETURNED. It moves
  nothing in core. It does not check that the payment is not already RETURNED.
- account/adjustment: `CustomerProductAccountAdjustmentService.adjustBalances` books one ADJUSTMENT
  transaction on the product account in core and, for a non-zero customer amount, enqueues one
  SAVINGS_TRANSACTION webhook. It moves nothing in clearing or at the bank.

Routes not driven: `due/split` takes core payment dues built from Trust orders and distributions
(`PaymentDueSplittingService` has no Direct case), and `disaggregate` takes a clearing aggregate that
the Direct batch dues never form with more than one member.
"""

from __future__ import annotations

import time
from decimal import Decimal

import jwt

from explorer import integrity, ledger, local_auth, misref, webhooks, world
from explorer.client import BearerClient, Call

SECOND_OPS_USER = "3d5e8a71-9c2b-4b6f-8e47-1a0f5c7d2b93"
MANAGEMENT = "/operations/own/payment/management"
MATCHING = "/operations/own/matching"
COUNTERPART_IBAN = "GB29NWBK60161331926819"
WAIT_SECONDS = 120
PASS_SECONDS = 10
SNAPSHOT_CUSTOMERS = 5
PENNY = Decimal("0.01")

SPLIT_EVENT = "FundingRecordSplitRequest"
RETURN_EVENT = "ReturnTransactionRequest"
ADJUSTMENT_EVENT = "CustomerProductAccountAdjustmentRequest"
MATCH_MODES = ("paymentDue", "actualPayment", "set")


# --- request bodies ---------------------------------------------------------------------------


def split_body(funding_record_uid, value_to_extract):
    return {"fundingRecordUid": funding_record_uid, "valueToExtract": "{:.2f}".format(
        Decimal(value_to_extract))}


def return_body(transaction_id):
    return {"transactionId": transaction_id,
            "reasonForReturn": "BENEFICIARY_IS_NOT_EXPECTING_FUNDS_OR_INSTRUCTED_RETURN",
            "realAccountType": "DIRECT"}


def adjustment_body(product_account_uid, amount, reason):
    return {"productAccountUid": product_account_uid,
            "customerAdjustment": "{:.2f}".format(Decimal(amount)),
            "bondsmithFeeAdjustment": "0.00", "platformFeeAdjustment": "0.00", "reason": reason}


def match_request(mode, aggregate_uid, funding_uids, comment):
    """(path, body) for the manual match. `actualPayment` names one funding record, so it is
    only built for a single record; the request's own JSON property is `paymentDueUids`."""
    if mode == "actualPayment" and len(funding_uids) == 1:
        return (MATCHING + "/partner/PLATFORM/actualPayment",
                {"fundingRecordUid": funding_uids[0], "paymentDueUids": [aggregate_uid],
                 "comment": comment})
    if mode == "set":
        return (MATCHING + "/set", {"fundingRecordUids": list(funding_uids),
                                     "aggregatePaymentDueUids": [aggregate_uid],
                                     "comment": comment})
    return (MATCHING + "/partner/PLATFORM/paymentDue",
            {"aggregatePaymentDueUid": aggregate_uid, "fundingRecordUids": list(funding_uids),
             "comment": comment})


# --- the second approver ----------------------------------------------------------------------


def second_ops(x):
    """An ops client for a different user, or None when the run is not on local auth."""
    cached = getattr(x, "_second_ops", None)
    if cached is not None:
        return cached or None
    local = False
    try:
        claims = jwt.decode(getattr(x.ops, "token", None) or "", options={"verify_signature": False})
        local = claims.get("iss") == "simulation-explorer-local"
    except Exception:  # noqa: BLE001 - an unreadable token means no local auth
        local = False
    x._second_ops = BearerClient(
        x.ops.base_url, local_auth.ops_token(SECOND_OPS_USER),
        renew=lambda: local_auth.ops_token(SECOND_OPS_USER)) if local else False
    return x._second_ops or None


def _psql(dsn, sql):
    rows, error = integrity._psql_on(dsn, sql)
    return None if error else rows


def _unavailable(label, why):
    return Call("POST", label, 412, {"message": why}, 0)


def _find_request(x, event, marker):
    """The REQUESTED request of this event whose payload carries `marker`, or None."""
    listed = x.ops.call("GET", MANAGEMENT + "/payment/request", params={
        "eventName": event, "state": "REQUESTED", "take": 50, "orderAscDesc": "DESC"})
    rows = (listed.body.get("content") or []) if listed.ok and isinstance(listed.body, dict) else []
    for row in rows:
        if marker in str(row.get("payload")):
            return str(row.get("requestUid"))
    found = _psql(integrity.CORE_DSN,
                  "SELECT uid FROM payment_management_request WHERE event_name = {} AND state = "
                  "'REQUESTED' AND payload::text LIKE {} ORDER BY sid DESC LIMIT 1".format(
                      misref.literal(event), misref.literal("%" + marker + "%")))
    return found[0][0] if found else None


def _request_state(uid):
    rows = _psql(integrity.CORE_DSN, "SELECT state FROM payment_management_request WHERE uid = {}"
                 .format(misref.literal(uid)))
    return rows[0][0] if rows else None


def submit_and_approve(x, label, route, body, event, marker, self_approval_probe=False):
    """Request a correction as the run's ops user and approve it as the second user.

    Returns a dict: request (Call), uid, probe (Call or None), approval (Call or None), state.
    A request that cannot be approved is rejected, so no REQUESTED row is left behind.
    """
    out = {"request": x.ops.call("POST", MANAGEMENT + route, json_body=body), "uid": None,
           "probe": None, "approval": None, "state": None}
    if not out["request"].ok:
        return out
    out["uid"] = _find_request(x, event, marker)
    if not out["uid"]:
        return out
    approve = MANAGEMENT + "/payment/approval"
    if self_approval_probe:
        out["probe"] = x.ops.call("POST", approve, json_body={"requestUid": out["uid"]})
        out["state"] = _request_state(out["uid"])
    if out["state"] in (None, "REQUESTED"):
        out["approval"] = second_ops(x).call("POST", approve, json_body={"requestUid": out["uid"]})
        out["state"] = _request_state(out["uid"])
    if out["state"] == "REQUESTED":
        second_ops(x).call("POST", MANAGEMENT + "/payment/rejection",
                           json_body={"requestUid": out["uid"]})
    return out


def _approval_text(done):
    return "request {} approval {} state {}".format(
        done["request"].status, done["approval"].status if done["approval"] else "none",
        done["state"])


# --- sweeping and waiting ---------------------------------------------------------------------


def _sweep(x):
    steps = [x.poll_if_new_money(), world.drain_transactions(x.ops), world.settle_payments(x.ops),
             world.drain_transactions(x.ops)]
    x.sweeps += 1
    for step in steps:
        if step is None:
            continue
        for call in step if isinstance(step, (list, tuple)) else [step]:
            if not getattr(call, "ok", True):
                return call
    return None


def _wait(x, check, seconds=WAIT_SECONDS):
    """The first truthy answer of `check`, sweeping between tries, or None at the deadline."""
    deadline = time.time() + seconds
    while True:
        found = check()
        if found:
            return found
        if time.time() >= deadline:
            return None
        time.sleep(PASS_SECONDS)
        _sweep(x)


# --- what clearing holds ----------------------------------------------------------------------

FUNDING_COLUMNS = ("uid", "value", "account", "direction", "mark", "currency", "ignored", "links")


def funding_rows(reference):
    """Every funding record carrying this customer reference, as dicts, or None when unreadable."""
    rows = _psql(integrity.CLEARING_DSN,
                 "SELECT f.uid, f.value_amount, f.account_sid, f.payment_direction, "
                 "f.debit_credit_mark, f.value_currency, f.ignored, (SELECT count(*) FROM "
                 "partner_payment_link l WHERE l.funding_record_sid = f.sid) FROM funding_record f "
                 "WHERE f.customer_reference = {} ORDER BY f.sid".format(misref.literal(reference)))
    if rows is None:
        return None
    return [dict(zip(FUNDING_COLUMNS, row), value=Decimal(row[1]), links=int(row[7]),
                 ignored=row[6] == "t") for row in rows]


def due_rows(reference):
    """[(aggregate uid, aggregate amount, status)] of the PLATFORM_SAFEGUARD dues with this
    payment reference, or None when unreadable."""
    rows = _psql(integrity.CLEARING_DSN,
                 "SELECT DISTINCT d.aggregate_uid, d.aggregate_amount, d.payment_status FROM "
                 "partner_payment_due d WHERE d.payment_reference = {} AND d.payment_direction = "
                 "'PLATFORM_SAFEGUARD' AND d.aggregate_uid IS NOT NULL".format(
                     misref.literal(reference)))
    return None if rows is None else [(r[0], Decimal(r[1]), r[2]) for r in rows]


def links_on_dues(aggregate_uid):
    """The funding record uids linked, as a pair, to any due of this aggregate."""
    rows = _psql(integrity.CLEARING_DSN,
                 "SELECT DISTINCT f.uid FROM partner_payment_link l JOIN partner_payment_due d ON "
                 "d.sid = l.payment_due_sid JOIN funding_record f ON f.sid = l.funding_record_sid "
                 "WHERE d.aggregate_uid = {}".format(misref.literal(aggregate_uid)))
    return None if rows is None else {r[0] for r in rows}


def plan_split(records, due):
    """The extraction that leaves credits summing exactly to the due, or None.

    `records` are the unlinked credits, [(uid, value)]. The excess is cut from the largest record,
    which must be strictly larger than it (FundingRecordPersistenceService.validate), and the
    records that stay are all of them, since the new record holds the excess.
    """
    total = sum((value for _, value in records), Decimal("0"))
    if not records or total <= due:
        return None
    excess = total - due
    largest_uid, largest = max(records, key=lambda r: r[1])
    if largest <= excess:
        return None
    return {"split": largest_uid, "excess": excess, "keep": [uid for uid, _ in records],
            "total": total}


def judge_split(before, after, split_uid, excess):
    """[(rule, expected, actual)] for how a split changed the funding records of one reference."""
    found = []
    old = {r["uid"]: r for r in before}
    new = {r["uid"]: r for r in after}
    added = [r for uid, r in new.items() if uid not in old]
    total_before = sum((r["value"] for r in before), Decimal("0"))
    total_after = sum((r["value"] for r in after), Decimal("0"))
    if total_after != total_before:
        found.append(("a split funding record keeps the credit's whole value",
                      "the records sum to {}".format(total_before),
                      "they sum to {}".format(total_after)))
    if len(added) != 1:
        found.append(("an approved funding split makes exactly one new funding record",
                      "one new record of {}".format(excess), "{} new records".format(len(added))))
    elif added[0]["value"] != excess:
        found.append(("an approved funding split makes exactly one new funding record",
                      "a new record of {}".format(excess),
                      "a new record of {}".format(added[0]["value"])))
    elif split_uid in old:
        template = old[split_uid]
        drift = [k for k in ("account", "direction", "mark", "currency") if added[0][k] != template[k]]
        if drift:
            found.append(("an extracted funding record carries the original's account, direction, "
                          "mark and currency", "the same {}".format(", ".join(drift)),
                          "a different {}".format(", ".join(drift))))
    if split_uid in new and split_uid in old and new[split_uid]["value"] != old[split_uid]["value"] - excess:
        found.append(("the split funding record loses exactly the extracted value",
                      "{}".format(old[split_uid]["value"] - excess),
                      "{}".format(new[split_uid]["value"])))
    for uid, record in old.items():
        if uid != split_uid and uid in new and new[uid]["value"] != record["value"]:
            found.append(("a split leaves every other funding record alone",
                          "{} stays {}".format(uid[:8], record["value"]),
                          "it reads {}".format(new[uid]["value"])))
    return found


# --- the customers' side ----------------------------------------------------------------------


def idle_customers(x, limit=SNAPSHOT_CUSTOMERS):
    """Customers no live batch of the run names, so a sweep has no deposit of theirs to settle."""
    busy = {c for b in x.batches if not b.get("done") for c in b.get("members", [])}
    return [s["customerId"] for s in x.subjects
            if s.get("customerId") and not s.get("closed") and s["customerId"] not in busy][:limit]


def snapshots(x, customers):
    out = {}
    for customer in customers:
        got = x.money_snapshot(customer)
        if got is not None:
            out[customer] = got
    return out


def money_moved(before, after):
    """[text] for each account that took a transaction other than INTEREST, or whose balance
    changed with no new transaction at all."""
    moved = []
    for customer, accounts in before.items():
        for account, (_, balance, transactions) in accounts.items():
            now = after.get(customer, {}).get(account)
            if now is None:
                continue
            fresh = {k: v for k, v in now[2].items() if k not in transactions}
            other = {k: v for k, v in fresh.items() if v[0] != "INTEREST"}
            if other:
                moved.append("account {} took {}".format(account[:8], sorted(
                    "{} {}".format(v[0], v[1]) for v in other.values())))
            elif balance != now[1] and not fresh:
                moved.append("account {} went {} to {} with no new transaction".format(
                    account[:8], balance, now[1]))
    return moved


def _flag(x, action, rule, subject, detail, expected, actual):
    x.note_violation(action, rule, subject, detail, expected, actual)


# --- (a) split an over-paid credit, then match the right part ---------------------------------


def pick_split_target(x):
    """(batch, plan, aggregate uid, records) for a live batch whose unmatched credits exceed its
    due, or (None, reason)."""
    refs = [b["paymentReference"] for b in x.batches]
    saw = 0
    for batch in x.batches:
        if batch.get("done") or not batch.get("batchId") or batch.get("corrected"):
            continue
        reference = batch["paymentReference"]
        if refs.count(reference) != 1:
            continue
        dues = [d for d in (due_rows(reference) or []) if d[2] in ("EXPECTED", "SENT")]
        if len(dues) != 1:
            continue
        rows = funding_rows(reference)
        if rows is None:
            continue
        free = [(r["uid"], r["value"]) for r in rows
                if r["links"] == 0 and not r["ignored"] and r["mark"] == "CREDIT"
                and r["direction"] == "PLATFORM_SAFEGUARD"]
        saw += 1
        plan = plan_split(free, dues[0][1])
        if plan:
            return batch, plan, dues[0][0], rows
    return None, "{} live batches checked and none holds unmatched credits above its due".format(
        saw)


def _batch_lines(x, batch):
    read = x.client.call("GET", "/direct/v1/batches/{}".format(batch["batchId"]))
    if not read.ok or not isinstance(read.body, dict):
        return None, {}
    rows = read.body.get("allocations") or read.body.get("content") or []
    return read.body.get("status"), {r.get("instructionReference"): r.get("status")
                                      for r in rows if isinstance(r, dict)}


def _deposit_amounts(snapshot_by_customer, customer, seen):
    """The amounts of DEPOSIT rows on the customer's accounts that `seen` did not hold."""
    out = []
    for account, (_, _, transactions) in snapshot_by_customer.get(customer, {}).items():
        known = seen.get(customer, {}).get(account, (None, None, {}))[2]
        out.extend(v[1] for k, v in transactions.items() if k not in known and v[0] == "DEPOSIT")
    return sorted(out)


def split_overpaid_credit(x):
    label = "split-overpaid-credit"
    if not x.spaced("correction_split_at"):
        return _unavailable(label, "a payment correction ran fewer than {} trials ago".format(
            x.STACK_CALL_EVERY_TRIALS))
    if second_ops(x) is None:
        return _unavailable(label, "no second ops user: the run is not on local auth")
    picked = pick_split_target(x)
    if picked[0] is None:
        return _unavailable(label, picked[1])
    batch, plan, aggregate, before_rows = picked
    reference, turn = batch["paymentReference"], getattr(x, "split_turns", 0)
    x.split_turns = turn + 1
    notes = []
    status_before, lines_before = _batch_lines(x, batch)
    customers = list(dict.fromkeys(batch.get("members", [])))[:SNAPSHOT_CUSTOMERS]
    money_before = snapshots(x, customers)

    done = submit_and_approve(x, label, "/funding/split",
                              split_body(plan["split"], plan["excess"]), SPLIT_EVENT, plan["split"])
    summary = "batch {} ref {} credits {} due {}: split {} off credit {}: {}".format(
        batch["batchId"][:8], reference, plan["total"], plan["total"] - plan["excess"],
        plan["excess"], plan["split"][:8], _approval_text(done))
    if done["state"] != "COMPLETED":
        rows_now = funding_rows(reference) or []
        raced = [r for r in rows_now if r["uid"] in plan["keep"] and r["links"] > 0]
        if raced:
            return Call("POST", label, 200, {"message": summary + "; a credit was matched meanwhile"}, 0)
        if done["state"] == "FAILED":
            _flag(x, "SplitOverpaidCredit", "an approved funding split completes", reference,
                  summary, "state COMPLETED", "state FAILED")
        return Call("POST", label, done["request"].status if not done["request"].ok else 412,
                    {"message": summary}, done["request"].elapsed_ms)
    x.note_in_flight("settlement", "a funding record split for {}".format(reference))

    after_split = funding_rows(reference)
    split_findings = judge_split(before_rows, after_split, plan["split"], plan["excess"]) \
        if after_split is not None else []
    for rule, expected, actual in split_findings:
        _flag(x, "SplitOverpaidCredit", rule, reference, summary, expected, actual)

    free = [r for r in (after_split or []) if r["uid"] in plan["keep"] and r["links"] == 0]
    if len(free) < len(plan["keep"]):
        notes.append("{} of the {} credits to match were already linked, so the matcher ran "
                     "first".format(len(plan["keep"]) - len(free), len(plan["keep"])))
    mode = "paymentDue" if len(plan["keep"]) > 1 and turn % 2 == 0 else (
        "set" if len(plan["keep"]) > 1 else MATCH_MODES[turn % len(MATCH_MODES)])
    matched = None
    if free:
        path, body = match_request(mode, aggregate, [r["uid"] for r in free],
                                   "simulation-explorer split {}".format(reference))
        matched = x.ops.call("POST", path, json_body=body)
        notes.append("match {} answered {}".format(mode, matched.status))
        if not matched.ok:
            again = funding_rows(reference) or []
            if any(r["uid"] in plan["keep"] and r["links"] == 0 for r in again):
                _flag(x, "SplitOverpaidCredit",
                      "an operator's match of credits that sum exactly to the due is accepted",
                      reference, "{}; {}".format(summary, matched.body),
                      "a 2xx for {} credit(s) of {} against a due of {}".format(
                          len(free), sum((r["value"] for r in free), Decimal("0")),
                          plan["total"] - plan["excess"]),
                      "{} {}".format(matched.status, str(matched.body)[:300]))
            else:
                notes.append("the refusal came after the credits were linked by the matcher")
    batch["corrected"] = True
    batch["amended"] = True
    x.note_in_flight("settlement", "an operator match for {}".format(reference))

    def settled():
        rows = funding_rows(reference) or []
        linked = [r for r in rows if r["uid"] in plan["keep"] and r["links"] > 0]
        status, lines = _batch_lines(x, batch)
        if len(linked) == len(plan["keep"]) and status in ("SETTLED", "COMPLETED"):
            return {"rows": rows, "status": status, "lines": lines}
        return None

    result = _wait(x, settled)
    rows = funding_rows(reference) or []
    status_after, lines_after = (result["status"], result["lines"]) if result else _batch_lines(
        x, batch)
    notes.append("batch {} to {}".format(status_before, status_after))
    for r in rows:
        if r["uid"] in plan["keep"] and r["links"] != 1:
            _flag(x, "SplitOverpaidCredit", "a credit an operator matches is linked to the due once",
                  reference, summary, "link count 1 for {}".format(r["uid"][:8]),
                  "link count {}".format(r["links"]))
    extracted = [r for r in rows if r["uid"] not in plan["keep"] and r["uid"] not in
                 {b["uid"] for b in before_rows}]
    if any(r["links"] for r in extracted):
        _flag(x, "SplitOverpaidCredit", "the extracted excess stays unmatched", reference, summary,
              "no link on the new record", "a link on {}".format(extracted[0]["uid"][:8]))
    stray = (links_on_dues(aggregate) or set()) - set(plan["keep"])
    if stray:
        _flag(x, "SplitOverpaidCredit", "only the credits an operator chose are linked to the due",
              reference, summary, "links from {} only".format(len(plan["keep"])),
              "also {}".format(sorted(u[:8] for u in stray)))

    after_money = snapshots(x, customers)
    for customer in customers:
        expected = sorted(Decimal(str(l["amount"])) for l in batch["lines"]
                          if l["customerId"] == customer and l.get("type", "DEPOSIT") == "DEPOSIT"
                          and lines_before.get(l["reference"]) not in
                          ("COMPLETED", "CANCELLED", "REJECTED")
                          and lines_after.get(l["reference"]) not in ("CANCELLED", "REJECTED"))
        got = _deposit_amounts(after_money, customer, money_before)
        if result and got != expected:
            _flag(x, "SplitOverpaidCredit", "credits an operator matches settle the batch exactly once",
                  customer, "{}; deposits that appeared on the customer's accounts".format(summary),
                  "deposits of {}".format([str(a) for a in expected]),
                  "deposits of {}".format([str(a) for a in got]))
        elif not result and len(got) > len(expected):
            _flag(x, "SplitOverpaidCredit", "credits an operator matches settle the batch exactly once",
                  customer, summary, "at most {} deposit(s)".format(len(expected)),
                  "{} deposits".format(len(got)))
    if not result:
        notes.append("the batch was not SETTLED within {}s of the match".format(WAIT_SECONDS))
        linked_all = all(r["links"] == 1 for r in rows if r["uid"] in plan["keep"])
        if linked_all and rows:
            _flag(x, "SplitOverpaidCredit", "credits an operator matches settle the batch",
                  reference, summary, "batch SETTLED after {}s".format(WAIT_SECONDS),
                  "batch {} with every credit linked".format(status_after))
    return Call("POST", label, 200, {"message": "{}; {}".format(summary, "; ".join(notes))}, 0)


# --- (b) return an unmatched credit ------------------------------------------------------------


def unmatched_credit(reference):
    """(funding record uid, partner payment transaction id, amount) for the credit sent as
    `reference`, once clearing holds both, else None."""
    funding = funding_rows(reference)
    paid = _psql(integrity.CLEARING_DSN,
                 "SELECT transaction_id, value_amount, payment_state FROM partner_payment WHERE "
                 "end_to_end_id = {} AND debit_credit_mark = 'CREDIT'".format(
                     misref.literal(reference)))
    if not funding or not paid or len(funding) != 1 or len(paid) != 1:
        return None
    return funding[0]["uid"], paid[0][0], Decimal(paid[0][1]), paid[0][2]


def return_payments(floor_sid, amount):
    """The return payments clearing raised after `floor_sid` for this amount to the sender."""
    rows = _psql(integrity.CLEARING_DSN,
                 "SELECT sid, end_to_end_id, status, sent_at IS NOT NULL FROM payment_initiation "
                 "WHERE sid > {} AND is_return AND amount = {} AND to_account_identifier ->> "
                 "'value' = {} ORDER BY sid".format(int(floor_sid), Decimal(amount),
                                                    misref.literal(COUNTERPART_IBAN)))
    return rows


def bank_debits(end_to_end_id):
    rows = _psql(ledger.HSB_DSN, "SELECT amount, status FROM payment_transaction_status WHERE "
                 "end_to_end_id = {}".format(misref.literal(end_to_end_id)))
    return rows


def return_unmatched_credit(x):
    label = "return-unmatched-credit"
    if not x.spaced("correction_return_at"):
        return _unavailable(label, "a payment correction ran fewer than {} trials ago".format(
            x.STACK_CALL_EVERY_TRIALS))
    if second_ops(x) is None:
        return _unavailable(label, "no second ops user: the run is not on local auth")
    turn = getattr(x, "return_turns", 0)
    x.return_turns = turn + 1
    reference = x.mint("rt", 16)
    amount = Decimal("2.37") + PENNY * (turn % 40)
    customers = idle_customers(x)
    before_money = snapshots(x, customers)
    floor = ledger.newest_payment_sid()

    credited = x.credit_and_count("{:.2f}".format(amount), reference)
    if not getattr(credited, "ok", False):
        return credited
    x.note_in_flight("credit", "an unmatched credit {!r} to return".format(reference))
    _sweep(x)
    held = _wait(x, lambda: unmatched_credit(reference))
    if not held:
        return _unavailable(label, "clearing did not hold the {} credit {!r} within {}s".format(
            amount, reference, WAIT_SECONDS))
    funding_uid, transaction_id, credit_amount, state_before = held

    done = submit_and_approve(x, label, "/return", return_body(transaction_id), RETURN_EVENT,
                              transaction_id)
    summary = "credit {} ref {!r} transaction {}: {}".format(
        credit_amount, reference, transaction_id[:8], _approval_text(done))
    if done["state"] != "COMPLETED":
        if done["state"] == "FAILED":
            _flag(x, "ReturnUnmatchedCredit", "an approved return completes", reference, summary,
                  "state COMPLETED", "state FAILED")
        return Call("POST", label, done["request"].status if not done["request"].ok else 412,
                    {"message": summary}, done["request"].elapsed_ms)
    x.note_in_flight("closure payment", "a returned credit {!r}".format(reference))

    _sweep(x)

    def sent():
        payments = return_payments(floor, credit_amount)
        return payments if payments and all(p[3] == "t" for p in payments) else None

    payments = _wait(x, sent)
    if payments is None:
        payments = return_payments(floor, credit_amount) or []
    state_after = (_psql(integrity.CLEARING_DSN,
                         "SELECT payment_state FROM partner_payment WHERE transaction_id = {}"
                         .format(misref.literal(transaction_id))) or [[None]])[0][0]
    notes = ["return payments {}, partner payment {} to {}".format(
        len(payments), state_before, state_after)]
    if len(payments) > 1:
        _flag(x, "ReturnUnmatchedCredit", "a return sends exactly one payment of the credit's "
              "amount", reference, summary, "one return payment of {}".format(credit_amount),
              "{} return payments".format(len(payments)))
    elif not payments and state_after == "RETURNED":
        _flag(x, "ReturnUnmatchedCredit", "a return sends exactly one payment of the credit's "
              "amount", reference, summary, "one return payment of {}".format(credit_amount),
              "the partner payment reads RETURNED and no return payment exists")
    elif not payments:
        notes.append("no return payment within {}s, partner payment still {}".format(
            WAIT_SECONDS, state_after))
    for sid, end_to_end, status, was_sent in payments:
        debits = bank_debits(end_to_end)
        if debits is None:
            continue
        if was_sent == "t" and len(debits) != 1:
            _flag(x, "ReturnUnmatchedCredit", "a sent return is one debit at the bank for the "
                  "returned amount", reference, "{}; return payment {}".format(summary, end_to_end),
                  "one bank debit of {}".format(credit_amount), "{} bank rows".format(len(debits)))
        elif debits and Decimal(debits[0][0]) != credit_amount:
            _flag(x, "ReturnUnmatchedCredit", "a sent return is one debit at the bank for the "
                  "returned amount", reference, "{}; return payment {}".format(summary, end_to_end),
                  "a bank debit of {}".format(credit_amount), "a bank debit of {}".format(
                      debits[0][0]))
        notes.append("payment {} status {} bank rows {}".format(
            end_to_end[:8], status, [(d[0], d[1]) for d in debits]))

    again = None
    if turn % 2 == 1 and payments:
        second_floor = ledger.newest_payment_sid()
        repeat = submit_and_approve(x, label, "/return", return_body(transaction_id),
                                    RETURN_EVENT, transaction_id)
        _sweep(x)
        extra = return_payments(second_floor, credit_amount) or []
        again = "second return {}: {} new return payment(s)".format(_approval_text(repeat),
                                                                    len(extra))
        notes.append(again)
        if extra:
            _flag(x, "ReturnUnmatchedCredit", "a credit that was returned is not returned again",
                  reference, "{}; {}".format(summary, again),
                  "no new return payment for transaction {}".format(transaction_id[:8]),
                  "{} new return payment(s) of {}".format(len(extra), credit_amount))

    funding_after = funding_rows(reference) or []
    if funding_after and funding_after[0]["links"]:
        _flag(x, "ReturnUnmatchedCredit", "a returned unmatched credit stays unmatched", reference,
              summary, "no link on funding record {}".format(funding_uid[:8]), "it was linked")
    notes.append("funding record {} {}".format(
        funding_uid[:8], "still held unmatched" if funding_after and not funding_after[0]["links"]
        else "gone or linked"))
    moved = money_moved(before_money, snapshots(x, customers))
    for text in moved:
        _flag(x, "ReturnUnmatchedCredit", "a return of an unmatched credit moves no customer balance",
              reference, "{}; {}".format(summary, text), "no new transaction on idle accounts",
              text)
    return Call("POST", label, 200, {"message": "{}; {}".format(summary, "; ".join(notes))}, 0)


# --- (c) adjust a Direct customer account -------------------------------------------------------

ACCOUNT_CORE_SQL = ("SELECT product_account_balance, bondsmith_fee_account_balance, "
                    "platform_fee_account_balance FROM customer_product_account WHERE uid = {}")
TRANSACTIONS_CORE_SQL = ("SELECT t.uid, t.transaction_type, t.customer_amount FROM "
                         "account_transaction t JOIN customer_product_account c ON c.sid = "
                         "t.customer_product_account_sid WHERE c.uid = {} ORDER BY t.sid")
ADJUSTMENTS_CORE_SQL = ("SELECT a.customer_balance_adjustment FROM "
                        "customer_product_account_adjustment a JOIN customer_product_account c ON "
                        "c.sid = a.customer_product_account_sid WHERE c.uid = {} AND a.reason = {}")


def core_account(account_id):
    rows = _psql(integrity.CORE_DSN, ACCOUNT_CORE_SQL.format(misref.literal(account_id)))
    return tuple(Decimal(v) for v in rows[0]) if rows else None


def core_transactions(account_id):
    rows = _psql(integrity.CORE_DSN, TRANSACTIONS_CORE_SQL.format(misref.literal(account_id)))
    return None if rows is None else [(r[0], r[1], Decimal(r[2])) for r in rows]


def savings_deliveries(x, account_id):
    """Distinct SAVINGS_TRANSACTION deliveries for the account, {X-Request-ID: transactionId}."""
    if not x.webhook_capture:
        return None
    records, truncated = webhooks.load(x.webhook_capture)
    if truncated:
        return None
    seen = {}
    for record in records:
        body = record.get("body")
        if record.get("platformUid") != x.platform_uid or not isinstance(body, dict):
            continue
        payload = body.get("payload") or {}
        if body.get("type") == "SAVINGS_TRANSACTION" and payload.get("savingsAccountId") == account_id:
            key = (record.get("headers") or {}).get("X-Request-ID") or id(record)
            seen[key] = (payload.get("transactionId"), webhooks._amount(payload.get("amount")),
                         payload.get("paymentDirection"))
    return seen


def pick_adjustment_account(x, turn):
    customers = idle_customers(x, limit=50)
    for offset in range(len(customers)):
        customer = customers[(turn + offset) % len(customers)]
        accounts = x.customer_accounts(customer) or []
        for account in accounts:
            if account.get("status") == "OPEN" and account.get("accountId"):
                return customer, account
    return None, None


def adjust_customer_account(x):
    label = "adjust-customer-account"
    if not x.spaced("correction_adjust_at"):
        return _unavailable(label, "a payment correction ran fewer than {} trials ago".format(
            x.STACK_CALL_EVERY_TRIALS))
    if second_ops(x) is None:
        return _unavailable(label, "no second ops user: the run is not on local auth")
    turn = getattr(x, "adjust_turns", 0)
    x.adjust_turns = turn + 1
    customer, account = pick_adjustment_account(x, turn)
    if account is None:
        return _unavailable(label, "no idle customer holds an OPEN account")
    account_id = account["accountId"]
    amount = Decimal("0.05") * (1 + turn % 8)
    reason = "simulation-explorer adjustment {}".format(x.mint("adj", 16))

    core_before = core_account(account_id)
    txs_before = core_transactions(account_id)
    direct_before = x.account_transactions(customer, account_id)
    balance_before = x.balance_of(account)
    hooks_before = savings_deliveries(x, account_id)
    if core_before is None or txs_before is None or direct_before is None:
        return _unavailable(label, "the account could not be read before the adjustment")

    done = submit_and_approve(x, label, "/account/adjustment",
                              adjustment_body(account_id, amount, reason), ADJUSTMENT_EVENT,
                              reason, self_approval_probe=True)
    summary = "account {} customer {} +{}: {}".format(account_id[:8], customer[:8], amount,
                                                      _approval_text(done))
    notes = []
    probe = done["probe"]
    if probe is not None:
        notes.append("the requester's own approval answered {}".format(probe.status))
        if probe.ok:
            _flag(x, "AdjustCustomerAccount",
                  "the requester cannot approve their own payment management request",
                  account_id, summary, "a 4xx from the requester's own approval",
                  "{}".format(probe.status))
    if done["state"] != "COMPLETED":
        if done["state"] == "FAILED":
            _flag(x, "AdjustCustomerAccount", "an approved adjustment completes", account_id,
                  summary, "state COMPLETED", "state FAILED")
        return Call("POST", label, done["request"].status if not done["request"].ok else 412,
                    {"message": summary}, done["request"].elapsed_ms)
    x.note_in_flight("settlement", "an adjustment of {}".format(account_id[:8]))

    txs_after = core_transactions(account_id) or []
    known = {t[0] for t in txs_before}
    fresh = [t for t in txs_after if t[0] not in known]
    adjustments = [t for t in fresh if t[1] == "ADJUSTMENT"]
    core_after = core_account(account_id)
    rows = _psql(integrity.CORE_DSN, ADJUSTMENTS_CORE_SQL.format(
        misref.literal(account_id), misref.literal(reason))) or []
    if len(adjustments) != 1 or adjustments[0][2] != amount or len(rows) != 1:
        _flag(x, "AdjustCustomerAccount", "an adjustment books exactly one transaction of its amount",
              account_id, summary, "one ADJUSTMENT of {} and one adjustment row".format(amount),
              "{} ADJUSTMENT transaction(s) {} and {} adjustment row(s)".format(
                  len(adjustments), [str(a[2]) for a in adjustments], len(rows)))
    if core_after is not None:
        moved = core_after[0] - core_before[0]
        explained = sum((t[2] for t in fresh), Decimal("0"))
        if moved != explained:
            _flag(x, "AdjustCustomerAccount", "an adjustment moves the account by exactly its amount",
                  account_id, summary,
                  "balance moved by {} (every new transaction)".format(explained),
                  "balance moved by {}".format(moved))
        if core_after[1:] != core_before[1:]:
            _flag(x, "AdjustCustomerAccount", "a customer-only adjustment leaves the fee balances alone",
                  account_id, summary, "fee balances {}".format([str(v) for v in core_before[1:]]),
                  "fee balances {}".format([str(v) for v in core_after[1:]]))

    direct_after = x.account_transactions(customer, account_id) or []
    direct_ids = {r.get("transactionId"): r for r in direct_after}
    if adjustments:
        row = direct_ids.get(adjustments[0][0])
        if row is None:
            notes.append("the Direct transaction list does not carry the adjustment")
        elif Decimal(str(row.get("amount") or "0")) != amount:
            _flag(x, "AdjustCustomerAccount", "the Direct API lists the adjustment at its amount",
                  account_id, summary, "{}".format(amount), "{}".format(row.get("amount")))
        else:
            notes.append("Direct lists it as {} {}".format(row.get("type"), row.get("mark")))
    account_now = next((a for a in (x.customer_accounts(customer) or [])
                        if a.get("accountId") == account_id), None)
    if account_now is not None and core_after is not None and x.balance_of(account_now) != core_after[0]:
        notes.append("Direct balance {} against core {}".format(x.balance_of(account_now),
                                                                core_after[0]))
    notes.append("balance {} to {}".format(balance_before, core_after[0] if core_after else "?"))

    if hooks_before is not None and adjustments:
        transaction = adjustments[0][0]
        mine = _wait(x, lambda: [k for k, v in (savings_deliveries(x, account_id) or {}).items()
                                 if v[0] == transaction and k not in hooks_before] or None,
                     seconds=45)
        got = [(k, v) for k, v in (savings_deliveries(x, account_id) or {}).items()
               if v[0] == transaction]
        if len(got) != 1 or (got and (got[0][1][1] != amount or got[0][1][2] != "CREDIT")):
            _flag(x, "AdjustCustomerAccount",
                  "an adjustment sends exactly one SAVINGS_TRANSACTION webhook of its amount",
                  account_id, "{}; transaction {}".format(summary, transaction),
                  "one delivery of {} CREDIT".format(amount),
                  "{} deliveries {}".format(len(got), [(v[1], v[2]) for _, v in got]))
        notes.append("{} SAVINGS_TRANSACTION delivery(ies)".format(len(got)) if mine or got
                     else "no SAVINGS_TRANSACTION delivery")
    elif hooks_before is None:
        notes.append("no webhook capture, so the delivery was not counted")
    return Call("POST", label, 200, {"message": "{}; {}".format(summary, "; ".join(notes))}, 0)
