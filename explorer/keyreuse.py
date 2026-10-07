"""Reusing a key the Direct API says is unique, with a different but valid body.

Each action makes the first call itself (or uses the object the run already holds), sends the second
with the same key, and judges both: the second must be refused, or (where the contract says so)
answer with the first call's result, and in every case no second object may exist, the first object
must read back unchanged, and no balance may move.

The contracts, from the exchange source the box runs (2fb856f49e):
- batchReference: "used for idempotency/deduplication" (ExternalDirectBatchPaymentRequest.java:24);
  DirectBatchOrderHandler.placeBatchOrder answers 409 "Batch reference has already been used" (:142).
- instructionReference: "Your unique reference for this instruction"
  (ExternalDirectBatchPaymentRequest.java:44); DirectBatchOrderHandler.validateOrder rejects the
  order with existingInstructionReference, as one rejected allocation in a 200.
- accountReference: "Your reference for this savings account" (ExternalDirectAccountOpeningRequest
  .java:30), no word on uniqueness; DirectAccountValidator.validateAccountOpeningOrder:56 refuses it.
- customerReference: "Your reference for this customer" (ExternalDirectCustomerRequest.java:22), no
  word on uniqueness; CustomerValidationService:93-101 refuses it.
- Idempotency-Key header: IdempotencyFilter replays the first response for the same partner, route
  and key and never compares the body, so a different body gets the first answer back.

The second-object and first-unchanged checks apply to all five keys. The accept-or-replay check
applies only where the contract is written down (batchReference and the header); for the other three
the observed answer is reported in the action's message.
"""

from __future__ import annotations

import json

from explorer import actions
from explorer.client import Call

SHOWN = 700
REFUSAL_OR_FIRST = "a refusal, or the first call's answer, and no second object"


def _rows(body):
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        return body.get("content") or body.get("allocations") or []
    return []


def _text(value):
    return json.dumps(value, sort_keys=True, default=str)[:SHOWN]


def _pick(row, keys):
    return {k: row.get(k) for k in keys if isinstance(row, dict) and k in row}


def account_rows(x, customer_id):
    rows = x.customer_accounts(customer_id)
    if rows is None:
        return None
    return sorted((_pick(r, ("accountId", "accountReference", "balance")) for r in rows),
                  key=_text)


def instruction_ids(x, customer_id):
    call = x.client.call("GET", "/direct/v1/customers/{}/instructions".format(customer_id))
    if not call.ok:
        return None
    return sorted(str(r.get("instructionId")) for r in _rows(call.body) if isinstance(r, dict))


def batch_view(x, batch_id):
    call = x.client.call("GET", "/direct/v1/batches/{}".format(batch_id))
    if not call.ok or not isinstance(call.body, dict):
        return None
    body = call.body
    return {
        "batch": _pick(body, ("batchReference", "paymentReference", "totalPaymentRequired")),
        "allocations": sorted((_pick(r, ("instructionReference", "instructionId", "amount",
                                          "accountId", "customerId"))
                               for r in _rows(body)), key=_text),
    }


def customer_view(x, customer_id):
    call = x.client.call("GET", "/direct/v1/customers/{}".format(customer_id))
    if not call.ok or not isinstance(call.body, dict):
        return None
    body = call.body
    view = _pick(body, ("customerReference", "customerName", "accountHolderType"))
    view["persons"] = [_pick(p, ("firstName", "lastName", "email", "dateOfBirth"))
                       for p in body.get("persons") or []]
    return view


def customers_with_reference(x, reference):
    call = x.client.call("GET", "/direct/v1/customers", params={"externalId": reference})
    if not call.ok:
        return None
    return [r for r in _rows(call.body)
            if isinstance(r, dict) and r.get("customerReference") == reference]


def _message(call):
    body = call.body
    if isinstance(body, dict):
        return body.get("message") or body.get("detail") or _text(body)
    return str(body)[:SHOWN]


def _evidence(first, second):
    return {
        "first": {"request": first.request_body, "status": first.status, "response": first.body},
        "second": {"request": second.request_body, "status": second.status,
                   "response": second.body},
    }


def _flag(x, action, rule, subject, expected, actual, first, second):
    detail = "first {} {} -> {} {}; second (same key, different body) {} -> {} {}".format(
        first.method, first.path, first.status, _text(first.body),
        _text(second.request_body), second.status, _text(second.body))
    x.note_violation(action, rule, subject, detail, expected, actual,
                     body=_evidence(first, second))


def _observed(second):
    return "{} {}".format(second.status, _message(second))[:SHOWN]


def _result(label, second, note):
    # Message only: the driver absorbs ids from a body, and the second call's ids must not become
    # what the run holds.
    return Call("POST", label, second.status, {"message": note}, second.elapsed_ms)


def _first_failed(label, first, what):
    return Call("POST", label, first.status if not first.ok else 412, {
        "message": "{}: {} {}".format(what, first.status, _message(first))}, first.elapsed_ms)


def judge(x, action, rule, subject, first, second, documented, same_as_first, extra=()):
    """Raise a violation for each broken expectation; return what was observed.

    `extra` is a list of (expected, actual) pairs the caller already found wrong, such as a second
    object it counted itself.
    """
    broken = list(extra)
    if second.status >= 500:
        broken.append((REFUSAL_OR_FIRST, "the second call failed with {}".format(
            _observed(second))))
    elif second.ok and documented and not same_as_first(second):
        broken.append((REFUSAL_OR_FIRST, "the second call was accepted with {}".format(
            _observed(second))))
    for expected, actual in broken:
        _flag(x, action, rule, subject, expected, actual, first, second)
    return not broken


def reuse_batch_reference(x):
    label = "reuse-batch-reference"
    held = x.held
    first_body = x.one_line_batch()
    first = x.client.call("POST", "/direct/v1/batches", json_body=first_body)
    if not first.ok or not isinstance(first.body, dict) or not first.body.get("batchId"):
        return _first_failed(label, first, "the first batch was not accepted")
    customer = held["customerId"]
    second_body = x.one_line_batch()
    second_body["batchReference"] = first_body["batchReference"]
    second_body["totalPaymentRequired"] = "7.33"
    second_body["allocations"][0]["amount"] = "7.33"
    view = batch_view(x, first.body["batchId"])
    accounts, instructions = account_rows(x, customer), instruction_ids(x, customer)
    second = x.client.call("POST", "/direct/v1/batches", json_body=second_body)

    extra = []
    if second.ok and isinstance(second.body, dict) and second.body.get("batchId") not in (
            None, first.body["batchId"]):
        extra.append(("no second batch", "batch {} exists beside {}".format(
            second.body["batchId"], first.body["batchId"])))
    _unchanged(x, extra, customer, accounts, instructions,
               lambda: batch_view(x, first.body["batchId"]), view, "the first batch")
    ok = judge(x, "ReuseBatchReference",
               "a batch reference that was used is refused or answered with the first batch, "
               "and moves nothing", first_body["batchReference"], first, second, True,
               lambda s: s.body == first.body, extra)
    return _result(label, second, "{} reuse of batchReference: {}".format(
        "clean" if ok else "BROKEN", _observed(second)))


def reuse_instruction_reference(x):
    label = "reuse-instruction-reference"
    held = x.held
    first_body = x.one_line_batch()
    first = x.client.call("POST", "/direct/v1/batches", json_body=first_body)
    first_lines = _rows(first.body) if first.ok else []
    accepted = next((l for l in first_lines if isinstance(l, dict) and l.get("instructionId")), None)
    if accepted is None:
        return _first_failed(label, first, "the first allocation was not accepted")
    reference = first_body["allocations"][0]["instructionReference"]
    customer = held["customerId"]
    second_body = x.one_line_batch()
    second_body["totalPaymentRequired"] = "7.33"
    second_body["allocations"][0]["instructionReference"] = reference
    second_body["allocations"][0]["amount"] = "7.33"
    view = batch_view(x, first.body["batchId"])
    accounts, instructions = account_rows(x, customer), instruction_ids(x, customer)
    second = x.client.call("POST", "/direct/v1/batches", json_body=second_body)

    extra = []
    for line in _rows(second.body) if second.ok else []:
        if (isinstance(line, dict) and line.get("instructionId")
                and line.get("instructionId") != accepted["instructionId"]):
            extra.append(("no second instruction", "instruction {} was created for the reused "
                          "reference".format(line["instructionId"])))
    _unchanged(x, extra, customer, accounts, instructions,
               lambda: batch_view(x, first.body["batchId"]), view, "the first batch")
    ok = judge(x, "ReuseInstructionReference",
               "an instruction reference that was used is refused and creates no second "
               "instruction", reference, first, second, False, lambda s: True, extra)
    return _result(label, second, "{} reuse of instructionReference: {}".format(
        "clean" if ok else "BROKEN", _observed(second)))


def reuse_account_reference(x):
    label = "reuse-account-reference"
    held = x.held
    customer, reference = held["customerId"], held.get("accountReference")
    if not reference:
        return Call("POST", label, 412, {"message": "the subject holds no account reference"}, 0)
    body = actions.open_account_body(held, x.mint)
    body["accountReference"] = reference
    other = next((pid for _, pid in x.products.values() if pid != held["productId"]), None)
    if other:
        body["productId"] = other
    elif held.get("productType") == "TERM":
        body["amount"] = "75.00"
    path = "/direct/v1/customers/{}/accounts".format(customer)
    first = x.client.call("GET", path)
    accounts = account_rows(x, customer)
    if not first.ok or accounts is None:
        return _first_failed(label, first, "the accounts could not be read")
    second = x.client.call("POST", path, json_body=body)

    extra = []
    if second.ok and isinstance(second.body, dict) and second.body.get("accountId"):
        extra.append(("no second account", "account {} was opened under reference {}".format(
            second.body["accountId"], reference)))
    _unchanged(x, extra, customer, accounts, None, None, None, "the first account")
    ok = judge(x, "ReuseAccountReference",
               "an account reference that was used is refused and opens no second account",
               reference, first, second, False, lambda s: True, extra)
    return _result(label, second, "{} reuse of accountReference: {}".format(
        "clean" if ok else "BROKEN", _observed(second)))


def reuse_customer_reference(x):
    label = "reuse-customer-reference"
    customer = x.held["customerId"]
    existing = x.client.call("GET", "/direct/v1/customers/{}".format(customer))
    reference = existing.body.get("customerReference") if existing.ok and isinstance(
        existing.body, dict) else None
    if not reference:
        return _first_failed(label, existing, "the customer's reference could not be read")
    body = actions.customer_body(x.held, x.mint)
    body["customerReference"] = reference
    body["person"].update({"firstName": "Other", "dateOfBirth": "1990-01-15"})
    body["person"]["email"] = "{}@example.com".format(x.mint("sim", 40))
    body["nominatedAccounts"][0]["accountName"] = "second nominated account"
    view = customer_view(x, customer)
    before = customers_with_reference(x, reference)
    second = x.client.call("POST", "/direct/v1/customers", json_body=body)

    extra = []
    if second.ok and isinstance(second.body, dict) and second.body.get("customerId") not in (
            None, customer):
        extra.append(("no second customer", "customer {} was created under reference {}".format(
            second.body["customerId"], reference)))
    after = customers_with_reference(x, reference)
    if before is not None and after is not None and len(after) != len(before):
        extra.append(("one customer holds the reference", "{} customers now hold it".format(
            len(after))))
    _unchanged(x, extra, customer, None, None, lambda: customer_view(x, customer), view,
               "the first customer")
    ok = judge(x, "ReuseCustomerReference",
               "a customer reference that was used is refused and creates no second customer",
               reference, existing, second, False, lambda s: True, extra)
    return _result(label, second, "{} reuse of customerReference: {}".format(
        "clean" if ok else "BROKEN", _observed(second)))


def reuse_idempotency_key(x):
    label = "reuse-idempotency-key"
    customer = x.held["customerId"]
    key = x.mint("idem", 36)
    header = {"Idempotency-Key": key}
    first_body = x.one_line_batch()
    first = x.client.call("POST", "/direct/v1/batches", json_body=first_body, headers=header)
    if not first.ok or not isinstance(first.body, dict) or not first.body.get("batchId"):
        return _first_failed(label, first, "the first batch was not accepted")
    if first.headers.get("idempotency-key") != key:
        return Call("POST", label, 412, {
            "message": "the answer did not echo the key, so the idempotency filter is off here"},
            first.elapsed_ms)
    second_body = x.one_line_batch()
    second_body["totalPaymentRequired"] = "7.33"
    second_body["allocations"][0]["amount"] = "7.33"
    view = batch_view(x, first.body["batchId"])
    accounts, instructions = account_rows(x, customer), instruction_ids(x, customer)
    second = x.client.call("POST", "/direct/v1/batches", json_body=second_body, headers=header)

    extra = []
    if second.ok and isinstance(second.body, dict) and second.body.get("batchId") not in (
            None, first.body["batchId"]):
        extra.append(("no second batch", "batch {} was created under key {}".format(
            second.body["batchId"], key)))
    _unchanged(x, extra, customer, accounts, instructions,
               lambda: batch_view(x, first.body["batchId"]), view, "the first batch")
    ok = judge(x, "ReuseIdempotencyKey",
               "an idempotency key that was used answers with the first response and runs "
               "nothing a second time", key, first, second, True, lambda s: s.body == first.body,
               extra)
    return _result(label, second, "{} reuse of Idempotency-Key{}: {}".format(
        "clean" if ok else "BROKEN",
        ", replayed" if second.headers.get("idempotent-replayed") == "true" else "",
        _observed(second)))


def _unchanged(x, extra, customer, accounts, instructions, read_first, first_view, what):
    """Append each way the first object, the balances or the instruction list moved."""
    if accounts is not None:
        now = account_rows(x, customer)
        if now is not None and now != accounts:
            extra.append(("no balance moves and no account appears",
                          "accounts read {} before and {} after".format(_text(accounts), _text(now))))
    if instructions is not None:
        now = instruction_ids(x, customer)
        if now is not None and now != instructions:
            extra.append(("no second instruction", "instructions read {} before and {} "
                          "after".format(_text(instructions), _text(now))))
    if read_first is not None and first_view is not None:
        now = read_first()
        if now is not None and now != first_view:
            extra.append(("{} is unchanged".format(what), "it read {} and now reads {}".format(
                _text(first_view), _text(now))))
