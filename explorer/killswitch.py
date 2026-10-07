"""Clearing's payment kill switch, turned on while withdrawals and closure payouts are live.

The contract, from the exchange source the box runs (2fb856f49e):
- ops-api `PUT /operations/own/payment/kill-switch/{enable}` (OpsPortalPaymentKillSwitchController)
  forwards to clearing `PaymentKillSwitchController.flipKillSwitch`, which updates the one row of
  `payment_kill_switch` (PaymentKillSwitchRepository). `GET` on the same path answers the flag.
- The flag is read in one place: `BatchedFileSender.sendFiles` (BatchedFileSender.java:49-52)
  returns before it fetches any file. Raising payment dues, grouping them, approving a group and
  creating the payment file (`PaymentLifecycleScheduler.createPaymentFiles`) all carry on, so a
  payment is left as a `payment_initiation` row with `sent_at` NULL inside an unsent
  `payment_initiation_file`. Core is not told about the switch: withdrawals and closures are
  accepted as usual and their instructions stay PENDING.
- Turning it off needs no other step. The next `sendFiles` (the ops `payment/groups/process`
  call, or the SendPaymentFiles cron) fetches `fully_unsent_files` oldest first, at most
  `maxRequests` of them, and sends each one.
- `hsbc/statement/transactions/maintenance/window` is left out: it shares `sendFiles`' early return
  but also stops statement polling and reconciliation for every platform, and is keyed to a London
  wall-clock range rather than a flag, so it is not the same shape of fault.

The action runs one of two variants, alternating between runs: it places withdrawals on an INSTANT
account, or it closes the account so that the closure pays the balance out. Both are judged by the
same rules, in plain English:
- while the switch is on, no new payment reaches the bank (hsb `payment_transaction_status`) and
  none is marked sent in clearing;
- while it is on, no instruction of the customer is COMPLETED;
- the API answers a withdrawal or a close with a success or a clear 4xx, never a 5xx;
- after it is off, each payment of the account is sent once: one bank row per payment, the bank's
  amount equal to clearing's, and no more paid than was owed;
- each withdrawal debits the customer once, and the account ends at the balance the withdrawals
  leave, or closed and empty after a closure;
- no payment due of the account is left EXPECTED, and no instruction is left PENDING, once the
  wait the code gives (the next send runs) has passed.
"""

from __future__ import annotations

import os
import time
from decimal import Decimal

import httpx

from explorer import actions, fleet, ledger, world
from explorer.client import Call

PATH = "/operations/own/payment/kill-switch/{}"
LABEL = "kill-switch-during-payouts"
ACTION = "KillSwitchDuringPayouts"

# The hold lasts as long as the steps inside it, which are bounded by the sweeps. Kept short
# because every other platform's payments wait too, and clear_nameless_groups fails a payment that
# has waited two minutes with no creditor name.
HOLD_PAYMENT_WAIT_SECONDS = 30
BANK_QUIET_SECONDS = 20
BANK_QUIET_POLL_SECONDS = 3
RECOVERY_SECONDS = 240
RECOVERY_POLL_SECONDS = 5
SHOWN = 600
TERMINAL = ("ACSC", "RJCT")

VARIANT_COUNTER = ".kill-switch-variant-index"

# What the switch is doing now, so the cleanup path can put it back without the run's own state:
# the signal handler arrives wherever the run is, and a killed run never reaches its own finally.
_ON = []
_OPS = []


def register(ops):
    """Remember the ops client, so `restore` can reach the switch from a signal handler."""
    _OPS[:] = [ops]


def _url_and_headers(ops):
    token = getattr(ops, "token", None)
    headers = {"Authorization": "Bearer {}".format(token)} if token else {}
    return ops.base_url, headers


def _put_directly(enable, ops):
    """Flip the switch with a fresh HTTP client, which stays safe inside a signal handler."""
    base, headers = _url_and_headers(ops)
    with httpx.Client(timeout=10.0) as http:
        answer = http.put(base + PATH.format("true" if enable else "false"), headers=headers)
        if answer.status_code == 401 and getattr(ops, "renew", None):
            ops.token = ops.renew()
            _, headers = _url_and_headers(ops)
            answer = http.put(base + PATH.format("true" if enable else "false"), headers=headers)
    return answer.status_code < 300


def restore(why="the run ended"):
    """Turn the switch off if this process turned it on. Safe to call more than once."""
    if not _ON or not _OPS:
        return []
    try:
        done = _put_directly(False, _OPS[0])
    except (httpx.HTTPError, OSError):
        done = False
    if not done:
        print("  -- the payment kill switch could NOT be turned off because {}; turn it off with "
              "PUT {}".format(why, PATH.format("false")))
        return []
    del _ON[:]
    fleet.note("fault", "the payment kill switch was turned off")
    print("  -- restoring the stack because {}: the payment kill switch is off".format(why))
    return ["the payment kill switch is off"]


def is_on(ops):
    """True or False as the switch reads, or None when it cannot be read."""
    call = ops.call("GET", PATH.format("").rstrip("/"))
    if not call.ok:
        return None
    body = call.body
    if isinstance(body, bool):
        return body
    return str(body).strip().lower() == "true"


def ensure_off_at_start(ops):
    """A run that was killed hard leaves the switch on, which would stall every later cycle."""
    register(ops)
    state = is_on(ops)
    if state:
        call = ops.call("PUT", PATH.format("false"))
        print("  -- the payment kill switch was left on by an earlier run; turned it off ({})"
              .format(call.status))
        fleet.note("fault", "the payment kill switch was turned off")
    return state


def _flip(ops, enable):
    if enable:
        # Recorded before the call, so a call whose answer is lost is still put back.
        _ON.append(time.time())
    call = ops.call("PUT", PATH.format("true" if enable else "false"))
    if not enable and call.ok:
        del _ON[:]
    return call


def _skip(why):
    return Call("PUT", LABEL, 412, {"message": why}, 0)


def _text(value):
    return str(value)[:SHOWN]


def _rows(sql, dsn=ledger.CLEARING_DSN):
    return ledger._psql(sql, dsn=dsn)


def _number(sql, dsn=ledger.CLEARING_DSN):
    rows = _rows(sql, dsn)
    try:
        return int(rows[0][0])
    except (IndexError, ValueError):
        return None


def sent_by_clearing():
    return _number("SELECT count(*) FROM payment_initiation WHERE sent_at IS NOT NULL")


def received_by_bank():
    return _number("SELECT count(*) FROM payment_transaction_status", ledger.HSB_DSN)


def newest_due_sid():
    return _number("SELECT COALESCE(MAX(sid), 0) FROM partner_payment_due") or 0


def _uuid(value):
    return str(value).replace("'", "")


def payments_of(account, after_sid):
    """The account's payments clearing raised after `after_sid`, with whether each was sent."""
    rows = _rows(
        "SELECT pi.sid, pi.end_to_end_id, pi.amount, coalesce(pi.status::text, ''), "
        "pi.sent_at IS NOT NULL, pg.status::text, coalesce(pg.message, '') "
        "FROM payment_initiation pi JOIN payment_group pg ON pg.sid = pi.payment_group_sid "
        "WHERE pi.sid > {} AND pi.is_return IS NOT TRUE AND EXISTS ("
        "  SELECT 1 FROM partner_payment_due ppd "
        "  JOIN internal_account ia ON ia.sid = ppd.account_sid "
        "  WHERE ppd.aggregate_uid = pg.uid AND ia.account_uid = '{}') "
        "ORDER BY pi.sid".format(int(after_sid), _uuid(account)))
    return [{"sid": r[0], "endToEndId": r[1], "amount": r[2], "status": r[3],
             "sent": r[4] == "t", "group": r[5], "groupMessage": r[6]} for r in rows if len(r) >= 7]


def bank_rows_of(end_to_end_id):
    """(rows the bank holds for the payment, their amounts), or None when unreadable."""
    rows = _rows("SELECT amount FROM payment_transaction_status WHERE end_to_end_id = '{}'".format(
        _uuid(end_to_end_id)), ledger.HSB_DSN)
    try:
        return len(rows), [Decimal(r[0]) for r in rows]
    except (ArithmeticError, IndexError):
        return None


def dues_of(account, after_sid):
    """{payment_status: count} of the account's payment dues raised after `after_sid`."""
    rows = _rows(
        "SELECT ppd.payment_status::text, count(*) FROM partner_payment_due ppd "
        "JOIN internal_account ia ON ia.sid = ppd.account_sid "
        "WHERE ppd.sid > {} AND ia.account_uid = '{}' GROUP BY 1".format(
            int(after_sid), _uuid(account)))
    return {r[0]: int(r[1]) for r in rows if len(r) >= 2}


def debits_of(instruction_ids):
    """{instruction uid: (status, SAVINGS_WITHDRAWAL transactions)} from core."""
    if not instruction_ids:
        return {}
    listed = ", ".join("'{}'".format(_uuid(i)) for i in instruction_ids)
    rows = ledger._psql(
        "SELECT i.uid, i.status, count(at.sid) FROM direct_customer_instruction i "
        "LEFT JOIN direct_instruction_subject dis ON dis.instruction_sid = i.sid "
        "LEFT JOIN account_transaction at ON at.balance_change_subject_id = dis.sid "
        "AND at.transaction_type = 'SAVINGS_WITHDRAWAL' "
        "WHERE i.uid IN ({}) GROUP BY i.uid, i.status".format(listed), dsn=ledger.CORE_DSN)
    return {r[0]: (r[1], int(r[2])) for r in rows if len(r) >= 3}


def _decimal(value):
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _next_variant():
    try:
        with open(VARIANT_COUNTER) as f:
            index = int(f.read().strip() or 0)
    except (OSError, ValueError):
        index = 0
    try:
        with open(VARIANT_COUNTER, "w") as f:
            f.write(str(index + 1))
    except OSError:
        pass
    return ("withdrawals", "closure")[index % 2]


def _withdrawal_amounts(balance):
    """Up to two small amounts that leave something in the account and differ from each other,
    because a repeat to the same payee is held by the duplicate gate and would look like a hold the
    switch caused."""
    amounts, left = [], balance
    for amount in ("1.00", "0.50", "0.01"):
        if len(amounts) == 2:
            break
        if Decimal(amount) + Decimal("0.01") <= left:
            amounts.append(amount)
            left -= Decimal(amount)
    return amounts


def _instructions(x, customer):
    call = x.client.call("GET", "/direct/v1/customers/{}/instructions".format(customer))
    if not call.ok:
        return None
    rows = call.body if isinstance(call.body, list) else (call.body or {}).get("content") or []
    return {r.get("instructionId"): r for r in rows if isinstance(r, dict)}


def _balance(x):
    account = x.read("account") or {}
    return x.balance_of(account), account.get("status")


class _Judge:
    def __init__(self, x, held, variant):
        self.x, self.held, self.variant = x, held, variant
        self.evidence = {"variant": variant, "customerId": held.get("customerId"),
                         "accountId": held.get("accountId")}
        self.broken = []

    def flag(self, rule, expected, actual):
        self.broken.append((rule, expected, actual))
        self.x.note_violation(
            ACTION, rule, self.held.get("accountId"),
            "{} variant: {}".format(self.variant, _text(actual)), expected, _text(actual),
            body=dict(self.evidence))


def kill_switch_during_payouts(x):
    """Turn the kill switch on, put withdrawals or a closure payout through, turn it off, judge."""
    if fleet.is_member():
        return _skip("only the conductor flips a switch the whole stack shares")
    live = x.live_fault()
    if live:
        return _skip("{} is already injected".format(live))
    register(x.ops)
    if is_on(x.ops):
        return _skip("the payment kill switch is already on")
    held, refused = x.closable_subject()
    if not held:
        return _skip(refused)
    balance, status = _balance(x)
    subject = x.subjects[x.current]
    variant = _next_variant()
    if variant == "withdrawals" and not (
            (subject.get("productType") or "INSTANT") == "INSTANT" and status == "OPEN"
            and _withdrawal_amounts(balance)):
        variant = "closure"
    judge = _Judge(x, held, variant)
    account = ledger.clearing_account_of(held.get("accountId"))
    if not account:
        return _skip("the account has no clearing internal account yet")

    before_payment = ledger.newest_payment_sid()
    before_due = newest_due_sid()
    judge.evidence.update({"balanceBefore": str(balance), "clearingAccount": account})

    switched = _flip(x.ops, True)
    if not switched.ok:
        _flip(x.ops, False)
        return Call("PUT", LABEL, switched.status, {
            "message": "the switch could not be turned on: {}".format(_text(switched.body))}, 0)
    fleet.note("fault", "the payment kill switch is on")
    x.note_in_flight("fault", "the payment kill switch is on")
    x.faulted_boundary = "the payment kill switch is on"
    x.faulted_at = x.steps
    placed = []
    try:
        # The baseline is read before any work is placed, so a payment that leaks during the
        # sweeps below counts against the switch instead of into the baseline.
        baseline = _quiet_counts()
        judge.evidence["bankBaseline"], judge.evidence["sentBaseline"] = baseline
        placed = _hold_phase(x, judge, held, variant, balance, account, before_payment, baseline)
    finally:
        # Every way out of the hold turns the switch off, and the cleanup in faults.restore_stack
        # does it again for a signal that arrives before this line.
        off = _flip(x.ops, False)
        if not off.ok:
            restore("the call that turns it off answered {}".format(off.status))
        fleet.note("fault", "the payment kill switch was turned off")
        x.note_in_flight("fault", "the payment kill switch was turned off")
        x.faulted_boundary = None
    if is_on(x.ops):
        judge.flag("the payment kill switch can be turned off again", "the switch reads false",
                   "it still reads true after PUT false answered {}".format(off.status))
        restore("it still read true after being turned off")
        return Call("PUT", LABEL, 500, {"message": "the switch would not turn off"}, 0)

    if placed is not None:
        _recovery_phase(x, judge, held, variant, balance, account, before_payment, before_due,
                        placed)
    clean = not judge.broken
    return Call("PUT", LABEL, 200, {"message": "{} {} variant: {}".format(
        "clean" if clean else "BROKEN", variant, _text(judge.evidence.get("summary")))}, 0)


def _hold_phase(x, judge, held, variant, balance, account, before_payment, baseline):
    """Place the work while the switch is on, sweep, and judge what reached the bank."""
    ev = judge.evidence
    placed = []
    if variant == "withdrawals":
        for amount in _withdrawal_amounts(balance):
            body = actions.withdraw_body(held, x.mint)
            body["amount"] = amount
            call = x.client.call(
                "POST", "/direct/v1/customers/{customerId}/accounts/{accountId}/instruction"
                        .format(**held), json_body=body)
            ev.setdefault("placed", []).append({"amount": amount, "status": call.status})
            _judge_api(judge, "a withdrawal", call)
            if call.ok and isinstance(call.body, dict) and call.body.get("instructionId"):
                placed.append({"id": call.body["instructionId"], "amount": amount})
    else:
        call = x.client.call(
            "POST", "/direct/v1/customers/{customerId}/accounts/{accountId}/close"
                    "?reason=NO_LONGER_NEEDED".format(**held))
        ev["closeStatus"] = call.status
        _judge_api(judge, "closing the account", call)
        if not call.ok:
            ev["summary"] = "the close answered {}, so nothing was held".format(call.status)
            return None
        x.note_in_flight("closure payment", "a closure payment held by the kill switch",
                         held["accountId"])
        x.finalise_the_closure(held["accountId"])
    if not placed and variant == "withdrawals":
        ev["summary"] = "no withdrawal was accepted, so nothing was held"
        return None
    _sweep_until_payment(x, account, before_payment)

    quiet_bank, quiet_sent = baseline
    x.settle_world()
    world.enquire_payment_status(x.ops)
    held_payments = payments_of(account, before_payment)
    bank_now, sent_now = received_by_bank(), sent_by_clearing()
    ev["held"] = {"payments": held_payments, "bank": bank_now, "sentByClearing": sent_now}
    if None not in (quiet_bank, bank_now) and bank_now != quiet_bank:
        judge.flag("while the payment kill switch is on, no payment reaches the bank",
                   "the bank's payment count does not move",
                   "{} before, {} after a payment sweep with the switch on".format(
                       quiet_bank, bank_now))
    if None not in (quiet_sent, sent_now) and sent_now != quiet_sent:
        judge.flag("while the payment kill switch is on, clearing sends no payment",
                   "clearing's count of sent payments does not move",
                   "{} before, {} after a payment sweep with the switch on".format(
                       quiet_sent, sent_now))
    sent_mine = [p for p in held_payments if p["sent"]]
    if sent_mine:
        judge.flag("while the payment kill switch is on, clearing sends no payment",
                   "no payment of this account marked sent",
                   "payment {} of this account was marked sent".format(
                       sent_mine[0]["endToEndId"]))
    if placed:
        seen = _instructions(x, held["customerId"]) or {}
        done = [i["id"] for i in placed if (seen.get(i["id"]) or {}).get("status") == "COMPLETED"]
        if done:
            judge.flag("while the payment kill switch is on, a withdrawal is not completed",
                       "PENDING", "instruction {} reads COMPLETED".format(done[0]))
    return placed


def _judge_api(judge, what, call):
    if call.status >= 500:
        judge.flag("while the payment kill switch is on, the Direct API never answers 5xx",
                   "a success or a clear 4xx",
                   "{} answered {} {}".format(what, call.status, _text(call.body)))


def _quiet_counts():
    """Read the bank and clearing counts until two reads a few seconds apart agree."""
    deadline = time.monotonic() + BANK_QUIET_SECONDS
    last = (received_by_bank(), sent_by_clearing())
    while time.monotonic() < deadline:
        time.sleep(BANK_QUIET_POLL_SECONDS)
        now = (received_by_bank(), sent_by_clearing())
        if now == last:
            return now
        last = now
    return last


def _sweep_until_payment(x, account, before_payment):
    deadline = time.monotonic() + HOLD_PAYMENT_WAIT_SECONDS
    while True:
        x.settle_world()
        if payments_of(account, before_payment) or time.monotonic() >= deadline:
            return
        time.sleep(3)


def _recovery_phase(x, judge, held, variant, balance_before, account, before_payment, before_due,
                    placed):
    """Sweep with the switch off until the held payments are paid, then judge each one."""
    ev = judge.evidence
    ids = [i["id"] for i in placed]
    deadline = time.monotonic() + RECOVERY_SECONDS
    payments, finished = [], False
    while True:
        x.settle_world()
        world.enquire_payment_status(x.ops)
        if variant == "closure":
            x.process_closures()
        payments = payments_of(account, before_payment)
        finished = _finished(x, held, variant, ids, payments)
        if finished or time.monotonic() >= deadline:
            break
        time.sleep(RECOVERY_POLL_SECONDS)
    x.settle_world()
    payments = payments_of(account, before_payment)
    ev["recovery"] = {"payments": payments, "finished": finished}
    note = "{} payment(s), {}".format(len(payments), "all settled" if finished else "not settled")

    paid_total = Decimal("0")
    for payment in payments:
        amount = _decimal(payment["amount"])
        bank = bank_rows_of(payment["endToEndId"])
        payment["bank"] = None if bank is None else {"rows": bank[0], "amounts": [str(a) for a in bank[1]]}
        if bank is None:
            continue
        rows, amounts = bank
        if rows > 1:
            judge.flag("a payment is sent to the bank once", "one bank row per payment",
                       "payment {} has {} bank rows, amounts {}".format(
                           payment["endToEndId"], rows, [str(a) for a in amounts]))
        elif rows == 0 and payment["status"] != "RJCT":
            judge.flag("a payment held by the kill switch is sent once it is turned off",
                       "a bank row within {}s of the switch going off".format(RECOVERY_SECONDS),
                       "payment {} has no bank row; clearing reads sent={} status={} group={} "
                       "{}".format(payment["endToEndId"], payment["sent"],
                                   payment["status"] or "NULL", payment["group"],
                                   payment["groupMessage"]))
        elif rows == 1 and amount is not None and amounts[0] != amount:
            judge.flag("a payment reaches the bank for the amount clearing raised",
                       "the bank's amount equals clearing's",
                       "payment {}: clearing {}, the bank {}".format(
                           payment["endToEndId"], amount, amounts[0]))
        if rows >= 1 and amount is not None:
            paid_total += amount * rows
    if not payments:
        judge.flag("a payout raised while the kill switch is on exists once it is turned off",
                   "a payment of this account after the sweeps",
                   "clearing raised no payment for the account's dues")

    owed = sum((Decimal(i["amount"]) for i in placed), Decimal("0")) if variant == "withdrawals" \
        else balance_before
    ev["owed"], ev["paid"] = str(owed), str(paid_total)
    if paid_total > owed:
        judge.flag("a payout is paid once", "no more is paid out than was owed",
                   "{} owed, the bank holds {} for the account's payments".format(
                       owed, paid_total))

    balance_after, status_after = _balance(x)
    ev["after"] = {"balance": str(balance_after), "status": status_after}
    if variant == "withdrawals":
        seen = _instructions(x, held["customerId"]) or {}
        left = [i for i in ids if (seen.get(i) or {}).get("status") == "PENDING"]
        if left and finished is False:
            judge.flag("a withdrawal held by the kill switch completes once it is turned off",
                       "COMPLETED within {}s".format(RECOVERY_SECONDS),
                       "instruction {} still reads PENDING".format(left[0]))
        debits = debits_of(ids)
        ev["debits"] = {k: list(v) for k, v in debits.items()}
        for instruction, (state, count) in debits.items():
            if count > 1:
                judge.flag("a withdrawal debits the customer once", "one SAVINGS_WITHDRAWAL",
                           "instruction {} has {} debits".format(instruction, count))
            elif count == 0 and state == "COMPLETED":
                judge.flag("a completed withdrawal has debited the customer",
                           "one SAVINGS_WITHDRAWAL", "instruction {} is COMPLETED with none".format(
                               instruction))
        expected = balance_before - owed if finished else None
        if expected is not None and balance_after != expected:
            judge.flag("a withdrawal debits the customer once",
                       "the balance falls by exactly the sum withdrawn",
                       "{} withdrawn from {}, expected {}, the account reads {}".format(
                           owed, balance_before, expected, balance_after))
    elif finished is False:
        judge.flag("a closure payout held by the kill switch is paid once it is turned off",
                   "the account CLOSED holding nothing within {}s".format(RECOVERY_SECONDS),
                   "the account reads {} holding {}".format(status_after, balance_after))

    dues = dues_of(account, before_due)
    ev["dues"] = dues
    if dues.get("EXPECTED") and finished:
        judge.flag("no payment due of the account is left EXPECTED after its payout is paid",
                   "no EXPECTED due", "{} dues read EXPECTED".format(dues["EXPECTED"]))
    ev["summary"] = note
    print("  -- kill switch during {}: {}; bank paid {} of {} owed".format(
        variant, note, paid_total, owed))


def _finished(x, held, variant, ids, payments):
    """Whether the payouts the switch held have been paid and the work they belong to is over."""
    if not payments or not all(p["sent"] and p["status"] in TERMINAL for p in payments):
        return False
    if variant == "withdrawals":
        seen = _instructions(x, held["customerId"])
        return seen is not None and all((seen.get(i) or {}).get("status") != "PENDING"
                                        for i in ids)
    balance, status = _balance(x)
    return status == "CLOSED" and balance == 0
