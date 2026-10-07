"""A one-month TERM account taken to maturity by each route DirectMaturityService has, and judged
once it has matured.

A TERM matures on the wall clock (TermProductMaturityRepository.fetchTermProductAccountsAtMaturity
compares cpam.due_date with the system's date), and the bank's date moves do not bring that nearer.
On the fake clock a one-month term is about three real days, so no call of the journey can wait
for it. Each call therefore does two things:

- judges every account it staged earlier whose maturity date has passed, reading core only;
- stages the next variant on a customer of its own and saves it in a file beside the saved cohort,
  so it is judged by a later call, in this run or another.

Variants, in rotation:

1. no destination: DirectMaturityService.matureToNominatedAccount (:131) raises a MATURITY
   instruction and an INTERNAL payment due, and the money is paid out to the nominated account.
2. an eligible destination (an OPEN NOTICE account of the customer): matureIntoDestination (:80)
   books a MATURITY, a PRODUCT_TRANSFER_OUT and a PRODUCT_TRANSFER_IN, and no payout.
3. a destination closed after it was set: DirectAccountClosureOperations.
   clearTermProductMaturityDestinations (:201) removes it, so the maturity is paid out.
4. a destination set and the customer then REJECTed (DEACTIVATED): divertReason (:96) asks
   DirectTransferDepositCheck.maturityRejection, which refuses a DEACTIVATED customer
   (CustomerActionPolicy.frozenStaysOnPlatform :228), so the maturity is paid out.
5. an ops term maturity break on the funded TERM, driven at once: DepositBreakingService
   .requestDepositBreak runs the Trust break and has no Direct guard on any layer.

A maturity that is paid out is judged by the money it moves, not by the payee screen: the account
ends at zero, one MATURITY instruction carries the balance plus the interest realised up to it,
and clearing holds one payout of that amount to the journey's payee.
"""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal

from explorer import clock, interest_oracle, longlived, world
from explorer.journeys import POLL_WINDOW_NOTE, ZERO, Journey, _money, _payouts_since

FUNDED = Decimal("50.00")
NOTICE_FUNDED = Decimal("5.00")
PAYEE_NUMBER = "46238510"
VARIANTS = ("no destination", "eligible destination", "destination closed", "customer rejected",
            "ops break")
NEEDS_NOTICE = {"eligible destination", "destination closed", "customer rejected"}
MAX_PENDING_PER_VARIANT = 2
# Whole wall days after the due date. TERM_PRODUCT_DISTRIBUTIONS runs each day, and the payout
# follows the INTERNAL transfer and the bank's statement.
MATURE_WITHIN_DAYS = 2
COMPLETE_WITHIN_DAYS = 4
DEACTIVATE_WAIT_SECONDS = 30


def _path(run):
    return longlived.saved_cohort_path().with_name("term-maturity.{}.json".format(run.platform_uid))


def _load(run):
    return longlived._read(_path(run)) or {"next": 0, "pending": [], "judged": 0}


def _save(run, state):
    longlived._write(_path(run), state)


def _rows(sql):
    rows, _ = interest_oracle._psql(sql)
    return rows or []


def _account(uid):
    rows = _rows(
        "SELECT cpas.current_state, cpas.deposit_state, cpa.product_account_balance, dca.status "
        "FROM customer_product_account cpa "
        "JOIN customer_product_account_state cpas "
        "ON cpas.customer_product_account_sid = cpa.sid AND cpas.live "
        "JOIN direct_customer_account dca ON dca.customer_product_account_sid = cpa.sid "
        "WHERE cpa.uid = '{}'".format(uid))
    if not rows:
        return None
    state, deposits, balance, status = rows[0]
    return {"state": state, "deposits": deposits, "balance": _money(balance), "status": status}


def _instructions(uid):
    """The account's MATURITY and transfer instructions, with how many INTERNAL payment dues each
    has. The platform payout's own due comes later, once the internal transfer settles."""
    return [{"type": r[0], "amount": _money(r[1]), "status": r[2], "dues": int(r[3]),
             "at": float(r[4])} for r in _rows(
        "SELECT i.instruction_type, i.amount, i.status, "
        "(SELECT count(*) FROM payment_due_direct_customer_instruction p "
        "WHERE p.direct_instruction_sid = i.sid AND p.payment_due_type = 'INTERNAL'), "
        "extract(epoch FROM i.created_at) "
        "FROM direct_customer_instruction i "
        "JOIN direct_customer_account d ON d.sid = i.direct_customer_account_sid "
        "JOIN customer_product_account cpa ON cpa.sid = d.customer_product_account_sid "
        "WHERE cpa.uid = '{}' AND i.instruction_type IN "
        "('MATURITY', 'PRODUCT_TRANSFER_OUT', 'PRODUCT_TRANSFER_IN', 'ROLLOVER') "
        "ORDER BY i.sid".format(uid))]


def _bookings(uid):
    """The account's booked transactions, each with how many SAVINGS_TRANSACTION webhooks it has."""
    return [{"type": r[0], "amount": _money(r[1]), "webhooks": int(r[2]), "at": float(r[3])}
            for r in _rows(
        "SELECT t.transaction_type, t.customer_amount, "
        "(SELECT count(*) FROM platform_webhook_event e "
        "WHERE e.event_entity_uid = t.uid AND e.event_type = 'SAVINGS_TRANSACTION'), "
        "extract(epoch FROM t.created_at) FROM account_transaction t "
        "JOIN customer_product_account cpa ON cpa.sid = t.customer_product_account_sid "
        "WHERE cpa.uid = '{}' ORDER BY t.sid".format(uid))]


def _webhooks_once(bookings):
    """Never two webhooks for a transaction, and one once the transaction is old enough for the
    outbox to have written it."""
    settled = clock.time() - clock.system_seconds(300)
    return all(b["webhooks"] <= 1 and (b["webhooks"] == 1 or b["at"] > settled)
               for b in bookings)


def _route(entry):
    """Where the maturity must go: into the destination only while it is set and eligible."""
    if entry.get("destination") and not entry.get("destinationClosed") \
            and not entry.get("rejected"):
        return "destination"
    return "nominated"


def _why_paid_out(entry):
    if entry.get("destinationClosed"):
        return "the destination was closed, which removes it (DirectAccountClosureOperations:201)"
    if entry.get("rejected"):
        return ("the customer is DEACTIVATED, which divertReason refuses "
                "(CustomerActionPolicy.frozenStaysOnPlatform:228)")
    return "no destination was set (DirectMaturityService.matureToNominatedAccount)"


# -- staging ---------------------------------------------------------------------------------


def _setup(j, variant):
    """A customer with a funded, OPEN TERM account, and for the destination variants a funded,
    OPEN NOTICE account. Answers the entry to save, or the reason it could not be made."""
    term, notice = j.product("TERM 1 month"), j.product("NOTICE")
    if not term or not notice:
        return "the platform lacks the one-month TERM or the NOTICE product"
    if not j.new_customer():
        return "the customer did not become ACTIVATED"
    a_term = j.open(term, "TERM", FUNDED)
    a_notice = j.open(notice, "NOTICE") if variant in NEEDS_NOTICE else None
    if not a_term or (variant in NEEDS_NOTICE and not a_notice):
        return "an account did not open"
    staged_at = clock.time()
    lines = [(a_term, FUNDED)] + ([(a_notice, NOTICE_FUNDED)] if a_notice else [])
    j.fund(lines)
    ids = [a["accountId"] for a, _ in lines]
    landed = j.settle_until(lambda: all(j.account(i).get("status") == "OPEN" for i in ids))
    j.step("fund the accounts and wait for OPEN", "OPEN" if landed else "not OPEN")
    if not landed:
        return "the funding did not land"
    read = j.account(a_term["accountId"])
    return {"variant": variant, "customer": j.customer, "account": a_term["accountId"],
            "due": str(read.get("maturityDate"))[:10], "funded": str(FUNDED),
            "stagedAt": staged_at, "notice": a_notice, "destination": None}


def _set_destination(j, entry):
    notice = entry["notice"]
    call = j.client.call(
        "POST", "/direct/v1/customers/{}/accounts/{}/maturityDestination".format(
            j.customer, entry["account"]), json_body={"productId": notice["productId"]})
    j.step("set the maturity destination to the NOTICE account", call.status)
    j.expect(call.ok, "a funded TERM accepts an OPEN NOTICE account as its maturity destination",
             "setting the destination answered {}".format(call.status), "2xx", call.status,
             body=call.body)
    if not call.ok:
        return
    read = j.account(entry["account"]).get("maturityDestination")
    j.expect(read == notice["accountId"], "an accepted maturity destination reads back",
             "the TERM account names {} as its destination".format(read), notice["accountId"],
             read)
    entry["destination"] = notice["accountId"]


def _close_destination(j, entry):
    call = j.client.call("POST", "/direct/v1/customers/{}/accounts/{}/close?reason="
                         "NO_LONGER_NEEDED".format(j.customer, entry["notice"]["accountId"]))
    j.step("close the destination", call.status)
    if not call.ok:
        return
    entry["destinationClosed"] = True
    read = j.account(entry["account"]).get("maturityDestination")
    j.expect(not read, "closing an account removes it as a maturity destination",
             "after the NOTICE account was closed the TERM names {} as its destination".format(
                 read), "no destination", read)


def _reject_customer(j, entry):
    run = j.run
    call = world.override_customer_status(run.compliance, j.customer, "REJECT",
                                          "simulation journey term maturity")
    deadline = time.monotonic() + DEACTIVATE_WAIT_SECONDS
    status = None
    while time.monotonic() < deadline:
        body = j.client.call("GET", "/direct/v1/customers/{}".format(j.customer)).body
        status = body.get("customerStatus") if isinstance(body, dict) else None
        if status == "DEACTIVATED":
            break
        time.sleep(2)
    j.step("officer REJECT", "{} -> {}".format(getattr(call, "status", None), status))
    entry["rejected"] = status == "DEACTIVATED"


def _break(j, entry):
    """The ops Trust deposit break on a Direct TERM account. It is refused by intent: the break
    pays the bank's return through the distribution flow, which Direct accounts do not use."""
    amount = "{:.2f}".format(FUNDED)
    call = j.run.ops.call(
        "POST", "/operations/own/account/{}/term/maturity/break".format(entry["account"]),
        json_body={"totalAmountReceived": amount, "customerAmounts": amount,
                   "platformFeeAmounts": "0.00", "bondsmithFeeAmounts": "0.00",
                   "depositBreakType": "OTHER", "bankPaymentReference": j.run.mint("brk", 16)})
    account = _account(entry["account"]) or {}
    j.step("ops term maturity break", "{}; the account reads {}".format(call.status,
                                                                         account.get("state")))
    j.expect(not call.ok and account.get("state") == "OPEN",
             "the ops term maturity break is not applied to a Direct TERM account",
             "the break answered {} and the Direct TERM account reads {} with {}".format(
                 call.status, account.get("state"), account.get("balance")),
             "a 4xx and the account still OPEN", "{} and {}".format(call.status,
                                                                   account.get("state")),
             body=call.body)


def _stage(j, state, variant):
    entry = _setup(j, variant)
    if not isinstance(entry, dict):
        return j.refuse(entry)
    if variant == "ops break":
        _break(j, entry)
        return j.done()
    if variant in NEEDS_NOTICE:
        _set_destination(j, entry)
    if entry["destination"] and variant == "destination closed":
        _close_destination(j, entry)
    if entry["destination"] and variant == "customer rejected":
        _reject_customer(j, entry)
    if variant in NEEDS_NOTICE and not entry["destination"]:
        return j.done(note="the destination was not set, so the account is not kept")
    state["pending"].append(entry)
    j.step("keep for judgement at maturity", "{} on {}".format(variant, entry["due"]))
    return j.done()


# -- judging ---------------------------------------------------------------------------------


def _judge(j, entry, today):
    """Judge one staged account. True once it is finished, False to look again on a later call."""
    j.customer = entry["customer"]
    due = date.fromisoformat(entry["due"])
    overdue = (today - due).days
    if overdue < 0:
        return False
    account = _account(entry["account"])
    if account is None:
        j.step("read {}".format(entry["account"][:8]), "no such account")
        return True
    if account["state"] == "OPEN":
        if overdue < MATURE_WITHIN_DAYS:
            return False
        j.expect(False, "a TERM account matures once its maturity date has passed",
                 "the {} account reads OPEN {} days after its maturity date {}".format(
                     entry["variant"], overdue, entry["due"]), "MATURED", "OPEN")
        return True
    j.expect(account["state"] == "MATURED", "a matured TERM account reads MATURED",
             "the {} account reads {} after maturity".format(entry["variant"], account["state"]),
             "MATURED", account["state"])
    j.expect(account["deposits"] == "NOT_ACCEPTING_DEPOSITS", "a matured TERM takes no deposits",
             "the matured account's deposit state is {}".format(account["deposits"]),
             "NOT_ACCEPTING_DEPOSITS", account["deposits"])
    instructions = _instructions(entry["account"])
    maturity = [i for i in instructions if i["type"] == "MATURITY"]
    j.expect(len(maturity) == 1, "a maturity books exactly one MATURITY instruction",
             "the {} account has {} MATURITY instructions".format(entry["variant"],
                                                                  len(maturity)), 1, len(maturity))
    if len(maturity) != 1:
        return True
    matured = maturity[0]
    route = _route(entry)
    bookings = _bookings(entry["account"])
    if not entry.get("checked"):
        entry["checked"] = True
        _judge_amount(j, entry, matured, bookings)
        _judge_route(j, entry, route, matured, instructions, bookings)
    if matured["status"] != "COMPLETED":
        if overdue < COMPLETE_WITHIN_DAYS:
            return False
        paid = _paid_out(entry, matured)
        j.expect(bool(paid), "a maturity raises its payout",
                 "the {} maturity of {} is {} {} days after the due date and clearing holds no "
                 "payout of it".format(entry["variant"], matured["amount"], matured["status"],
                                       overdue), "a payout", "none")
        return True
    _judge_settled(j, entry, route, matured, bookings)
    j.oracle([entry["account"]], "the matured {} account".format(entry["variant"]))
    return True


def _judge_amount(j, entry, matured, bookings):
    funded = Decimal(entry["funded"])
    before = sum((b["amount"] for b in bookings if b["type"] == "INTEREST"
                  and b["at"] <= matured["at"]), ZERO)
    after = [b for b in bookings if b["type"] == "INTEREST" and b["at"] > matured["at"]]
    j.expect(matured["amount"] == funded + before,
             "a maturity pays the balance plus the interest realised up to it",
             "the {} maturity is {} against {} funded and {} of interest realised before it"
             .format(entry["variant"], matured["amount"], funded, before), funded + before,
             matured["amount"])
    j.expect(not after, "no interest is realised on a TERM account after its maturity",
             "{} INTEREST rows of {} were booked after the maturity instruction".format(
                 len(after), sum((b["amount"] for b in after), ZERO)), "none", len(after))


def _judge_route(j, entry, route, matured, instructions, bookings):
    amount = matured["amount"]
    out = [i for i in instructions if i["type"] == "PRODUCT_TRANSFER_OUT"]
    into = [i for i in _instructions(entry["destination"])
            if i["type"] == "PRODUCT_TRANSFER_IN" and i["amount"] == amount] \
        if entry.get("destination") else []
    landed = [b for b in _bookings(entry["destination"])
              if b["type"] == "SAVINGS_DEPOSIT" and b["amount"] == amount] \
        if entry.get("destination") else []
    if route == "destination":
        j.expect(len(out) == 1 and out[0]["amount"] == amount and len(into) == 1,
                 "a maturity into the destination books one transfer out and one transfer in of "
                 "the maturity amount",
                 "{} PRODUCT_TRANSFER_OUT and {} PRODUCT_TRANSFER_IN for a maturity of {}".format(
                     len(out), len(into), amount), "1 and 1", "{} and {}".format(len(out),
                                                                                 len(into)))
        j.expect(matured["dues"] == 0, "a maturity into the destination raises no payment due",
                 "the MATURITY instruction has {} payment dues".format(matured["dues"]), 0,
                 matured["dues"])
        withdrawn = [b for b in bookings if b["type"] == "SAVINGS_WITHDRAWAL"
                     and b["amount"] == -amount]
        j.expect(len(withdrawn) == 1 and len(landed) == 1,
                 "a maturity into the destination books one withdrawal on the TERM and one "
                 "deposit on the destination",
                 "{} withdrawals of {} on the TERM and {} deposits on the destination".format(
                     len(withdrawn), amount, len(landed)), "1 and 1",
                 "{} and {}".format(len(withdrawn), len(landed)))
        j.expect(_webhooks_once(withdrawn + landed),
                 "each transaction of a maturity into the destination sends one webhook",
                 "webhooks per transaction: {}".format([b["webhooks"] for b in withdrawn + landed]),
                 "1 each", [b["webhooks"] for b in withdrawn + landed])
        j.expect(not _paid_out(entry, matured), "a maturity into the destination is not paid out",
                 "clearing holds a payout of {} to the customer".format(amount), "none",
                 "a payout")
        j.expect(_account(entry["account"])["balance"] == ZERO,
                 "a TERM account matured into a destination holds nothing",
                 "the account holds {}".format(_account(entry["account"])["balance"]), "0.00",
                 _account(entry["account"])["balance"])
        return
    reason = _why_paid_out(entry)
    j.expect(not out and not into and not landed,
             "a maturity that cannot go into the destination is paid out, never moved into it",
             "{}: {} PRODUCT_TRANSFER_OUT on the TERM, {} PRODUCT_TRANSFER_IN and {} deposits of "
             "{} on the destination".format(reason, len(out), len(into), len(landed), amount),
             "none", "{}, {} and {}".format(len(out), len(into), len(landed)))
    j.expect(matured["dues"] == 1, "a maturity paid out raises one payment due",
             "{}: the MATURITY instruction has {} payment dues".format(reason, matured["dues"]),
             1, matured["dues"])


def _judge_settled(j, entry, route, matured, bookings):
    amount = matured["amount"]
    reason = "into the destination" if route == "destination" else _why_paid_out(entry)
    withdrawn = [b for b in _bookings(entry["account"]) if b["type"] == "SAVINGS_WITHDRAWAL"
                 and b["amount"] == -amount]
    j.expect(len(withdrawn) == 1, "a completed maturity debits the TERM account once",
             "{}: {} withdrawals of {}".format(reason, len(withdrawn), amount), 1, len(withdrawn))
    account = _account(entry["account"])
    j.expect(account["balance"] == ZERO, "a completed maturity leaves the TERM account at zero",
             "{}: the account holds {}".format(reason, account["balance"]), "0.00",
             account["balance"])
    if route == "destination":
        return
    j.expect(_webhooks_once(withdrawn),
             "the withdrawal of a paid-out maturity sends one webhook",
             "webhooks per withdrawal: {}".format([b["webhooks"] for b in withdrawn]), "1 each",
             [b["webhooks"] for b in withdrawn])
    payouts = _paid_out(entry, matured)
    j.expect(len(payouts) == 1 and PAYEE_NUMBER in payouts[0][3],
             "a maturity paid out reaches the nominated account once",
             "{}: clearing holds {} payouts of {}, to {}".format(
                 reason, len(payouts), amount, [p[3] for p in payouts]),
             "one payout to {}".format(PAYEE_NUMBER), [p[3] for p in payouts])
    j.step("the {} maturity".format(entry["variant"]), "paid out {} to {}".format(
        amount, payouts[0][3] if payouts else "nobody"))


def _paid_out(entry, matured):
    return [p for p in _payouts_since(entry["customer"], entry["stagedAt"])
            if _money(p[1]) == matured["amount"]]


def _judge_due(j, state):
    today = clock.today()
    kept = []
    for entry in state["pending"]:
        try:
            finished = _judge(j, entry, today)
        except (ArithmeticError, ValueError, KeyError, IndexError) as fault:
            j.step("judge {}".format(entry.get("account", "?")[:8]), repr(fault))
            finished = False
        if finished:
            state["judged"] = state.get("judged", 0) + 1
            j.step("judged the {} account".format(entry["variant"]), entry["account"][:8])
        else:
            kept.append(entry)
    judged = len(state["pending"]) - len(kept)
    state["pending"] = kept
    return judged


def _next_variant(run, state):
    for offset in range(len(VARIANTS)):
        variant = VARIANTS[(state.get("next", 0) + offset) % len(VARIANTS)]
        waiting = sum(1 for e in state["pending"] if e["variant"] == variant)
        if waiting >= MAX_PENDING_PER_VARIANT:
            continue
        if variant == "customer rejected" and not run.compliance:
            continue
        if variant == "ops break" and not run.ops:
            continue
        state["next"] = (state.get("next", 0) + offset + 1) % len(VARIANTS)
        return variant
    return None


def term_maturity(run):
    j = Journey(run, "JourneyTermMaturity")
    if not run.platform_uid or not run.bank_uid:
        return j.refuse("no platform or bank")
    state = _load(run)
    judged = _judge_due(j, state)
    j.step("judge matured accounts", "{} judged, {} waiting for their date".format(
        judged, len(state["pending"])))
    _save(run, state)
    variant = _next_variant(run, state)
    if variant is None:
        return j.done(note="every variant has {} accounts waiting for maturity".format(
            MAX_PENDING_PER_VARIANT))
    if not j.statements_reachable():
        return j.done(note=POLL_WINDOW_NOTE) if judged else j.refuse(POLL_WINDOW_NOTE)
    j.step("stage", variant)
    call = _stage(j, state, variant)
    _save(run, state)
    return call


ALL = {"JourneyTermMaturity": term_maturity}
