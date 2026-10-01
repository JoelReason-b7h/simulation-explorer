"""Journeys over flows the explorer never reached before: the daily platform fee withdrawal, an
operator's review of a payee that failed Confirmation of Payee, and the Direct API's read models
(the customer-level transaction list, its filters and paging, the customer balances and the product
by id) held against each other.

Each follows the rules in journeys.py: its own customer where it moves money, each broken
expectation through Journey.expect, 412 when a precondition could not be reached.

None of them re-triggers a defect already in FINDINGS.md: no payee change while a withdrawal is in
flight (24, 25), no closure, and no top-up of a funded TERM.
"""

from __future__ import annotations

import time
from decimal import Decimal

from explorer import clock, fleet, interest_oracle, world
from explorer.journeys import (POLL_WINDOW_NOTE, SETTLE_PAUSE, ZERO, Journey, _money, _nominate,
                               _payouts_since, _rows, settle_rounds)

FEE_WITHDRAWALS = "FEE_WITHDRAWALS"
FEE_SAMPLE = 200
FEE_API_SAMPLE = 5
# A realisation that commits between the withdrawal's read of the fee balance and its update is
# left for the next run, so a shortfall made only of rows this close to the FEES row is not judged.
FEE_RACE_SECONDS = 5


# -- the platform fee withdrawal --------------------------------------------------------------


def _sql_uuids(values):
    return ",".join("'{}'".format(v) for v in values if v)


def fee_rows(platform_uid=None, account_uids=None):
    """One snapshot of each account's two fee pots and every transaction that moved a fee pot.

    One statement, so the balances and the rows agree with each other whatever commits meanwhile.
    Rows: (account uid, platform fee balance, bondsmith fee balance, transaction sid, transaction
    uid, created_at, type, customer amount, platform fee amount, fee amount); the transaction
    columns are empty for an account with no such row.
    """
    if account_uids:
        where = "cpa.uid IN ({})".format(_sql_uuids(account_uids))
    elif platform_uid:
        where = ("cpa.platform_product_sid IN (SELECT plp.sid FROM platform_product plp "
                 "JOIN partner_platform pp ON pp.sid = plp.platform_sid WHERE pp.uid = '{}')"
                 .format(platform_uid))
    else:
        return None, "neither a platform nor accounts"
    return interest_oracle._psql("""
      SELECT cpa.uid, cpa.platform_fee_account_balance, cpa.bondsmith_fee_account_balance,
             t.sid, t.uid, t.created_at, t.transaction_type, coalesce(t.customer_amount, 0),
             coalesce(t.platform_fee_amount, 0), coalesce(t.fee_amount, 0)
      FROM customer_product_account cpa
      LEFT JOIN account_transaction t ON t.customer_product_account_sid = cpa.sid
        AND (coalesce(t.platform_fee_amount, 0) <> 0 OR coalesce(t.fee_amount, 0) <> 0
             OR t.transaction_type = 'FEES')
      WHERE {}
      ORDER BY cpa.uid, t.sid""".format(where), timeout=300)


def fee_withdrawal_findings(rows, since_sid=0):
    """Judge every platform fee withdrawal (a FEES transaction) after `since_sid`, and every
    account's fee pots against its transactions.

    PlatformFeeWithdrawalService.movePlatformFeesToBondsmithPot takes the account's whole
    platform_fee_account_balance, and PlatformFeeWithdrawalRepository books it as one FEES row
    with customer_amount 0, platform_fee_amount -x and fee_amount +x, moving x from the platform
    fee pot to the Bondsmith fee pot. So each FEES row must move no customer money, create no fee,
    and take exactly the platform fee the account held: the sum of every earlier row's
    platform_fee_amount. Answers (findings, stats).
    """
    findings, stats = [], {"accounts": 0, "withdrawals": 0, "withdrawn": ZERO, "raced": 0}

    def find(rule, subject, detail, expected, actual):
        findings.append({"rule": rule, "subject": subject, "detail": detail,
                         "expected": str(expected), "actual": str(actual)})

    by_account = {}
    for row in rows or []:
        by_account.setdefault(row[0], {"pots": (row[1], row[2]), "rows": []})
        if row[3]:
            by_account[row[0]]["rows"].append(row)
    for account, entry in by_account.items():
        stats["accounts"] += 1
        platform_pot, bondsmith_pot = (_money(v) for v in entry["pots"])
        running = ZERO
        seen = []
        for row in entry["rows"]:
            sid, uid, created, kind = int(row[3]), row[4], interest_oracle._when(row[5]), row[6]
            customer, platform, fee = _money(row[7]), _money(row[8]), _money(row[9])
            if kind == "FEES" and sid > int(since_sid):
                stats["withdrawals"] += 1
                stats["withdrawn"] += -platform
                if customer.compare(ZERO) != 0:
                    find("a platform fee withdrawal moves no customer money", account,
                         "FEES transaction {} moved {} of the customer's money".format(
                             uid, customer), "0", customer)
                if (fee + platform).compare(ZERO) != 0:
                    find("a platform fee withdrawal moves the fee between pots and makes none",
                         account, "FEES transaction {} took {} from the platform fee pot and "
                         "put {} in the Bondsmith fee pot".format(uid, -platform, fee),
                         -platform, fee)
                if (-platform).compare(running) != 0:
                    near = sum((p for when, p in seen if created and when
                                and abs((created - when).total_seconds()) <= FEE_RACE_SECONDS),
                               ZERO)
                    if (running + platform).compare(near) == 0:
                        stats["raced"] += 1
                    else:
                        find("a platform fee withdrawal takes the whole platform fee the "
                             "account holds", account,
                             "FEES transaction {} at {} withdrew {} and the platform fee "
                             "realised and not yet withdrawn was {}".format(
                                 uid, row[5], -platform, running), running, -platform)
            running += platform
            if kind != "FEES":
                seen.append((created, platform))
        fee_total = sum((_money(r[9]) for r in entry["rows"]), ZERO)
        if platform_pot.compare(running) != 0:
            find("an account's platform fee pot is its transactions", account,
                 "platform_fee_account_balance is {} and the transactions' platform fee sums "
                 "to {}".format(platform_pot, running), running, platform_pot)
        if bondsmith_pot.compare(fee_total) != 0:
            find("an account's Bondsmith fee pot is its transactions", account,
                 "bondsmith_fee_account_balance is {} and the transactions' fee sums to {}"
                 .format(bondsmith_pot, fee_total), fee_total, bondsmith_pot)
    stats["withdrawn"] = str(stats["withdrawn"])
    return findings, stats


def _fee_accounts(platform_uid):
    """[(account uid, customer uid, platform fee balance)] for this platform's accounts that hold
    a realised platform fee, largest first."""
    rows, _ = interest_oracle._psql("""
      SELECT cpa.uid, pc.uid, cpa.platform_fee_account_balance
      FROM customer_product_account cpa
      JOIN platform_product plp ON plp.sid = cpa.platform_product_sid
      JOIN partner_platform pp ON pp.sid = plp.platform_sid
      JOIN customer_account ca ON ca.sid = cpa.customer_account_sid
      JOIN platform_customer pc ON pc.sid = ca.platform_customer_sid
      WHERE pp.uid = '{}' AND cpa.platform_fee_account_balance > 0
      ORDER BY cpa.platform_fee_account_balance DESC LIMIT {}""".format(
        platform_uid, FEE_SAMPLE), timeout=120)
    return [(r[0], r[1], _money(r[2])) for r in rows or []]


def _newest_transaction_sid():
    rows, _ = interest_oracle._psql("SELECT coalesce(max(sid), 0) FROM account_transaction",
                                    timeout=60)
    return int(rows[0][0]) if rows else None


def _customer_money_since(account_uids, since_sid):
    """{account uid: sum of customer_amount booked after `since_sid`}, over every transaction."""
    rows, _ = interest_oracle._psql(
        "SELECT cpa.uid, coalesce(sum(t.customer_amount), 0) FROM account_transaction t "
        "JOIN customer_product_account cpa ON cpa.sid = t.customer_product_account_sid "
        "WHERE cpa.uid IN ({}) AND t.sid > {} GROUP BY cpa.uid".format(
            _sql_uuids(account_uids), int(since_sid)), timeout=60)
    return {r[0]: _money(r[1]) for r in rows or []}


def platform_fee_withdrawal(run):
    """The platform's daily FEE_WITHDRAWALS (ScheduleEventType: platform, DAILY at 04:30), which
    the harness never ran, so every Direct account's realised platform fee had only ever grown and
    no account had ever held a Bondsmith fee balance. Once per business day: run it, judge every
    FEES row it wrote, check it moved none of the customers' money and that the customer's
    transaction list does not show it, run it again straight away the way an operator re-runs a
    job they think failed, and hold a day of interest on the new fee pots against the oracle."""
    j = Journey(run, "JourneyPlatformFeeWithdrawal")
    if not run.platform_uid or not run.ops or not run.bank_uid:
        return j.refuse("no platform, ops client or bank")
    if clock.schedulers_run():
        # Judges rows the scheduled job wrote, so it waits on no statement poll.
        return _scheduled_fee_withdrawals(run, j)
    today = interest_oracle.business_date(run.bank_uid)
    if getattr(run, "fee_withdrawal_day", None) == today:
        if not j.next_day():
            return j.refuse("the daily fee withdrawal already ran on business date {}".format(
                today))
        today = interest_oracle.business_date(run.bank_uid)
    accounts = _fee_accounts(run.platform_uid)
    if not accounts:
        instant = j.product("INSTANT")
        if not instant or not j.new_customer():
            return j.refuse("no platform fee realised, and no customer to earn one")
        account = j.open(instant, "INSTANT")
        if not account:
            return j.refuse("the INSTANT account did not open")
        j.fund([(account, Decimal("250.00"))])
        j.settle_until(lambda: _money(j.account(account["accountId"]).get("balance")) >= 250)
        j.next_day()
        j.next_day()
        accounts = _fee_accounts(run.platform_uid)
        if not accounts:
            return j.refuse("two days on 250.00 realised no platform fee")
    held = sum((a[2] for a in accounts), ZERO)
    j.step("accounts holding a realised platform fee", "{} holding {}".format(len(accounts),
                                                                             held))
    sample = accounts[:FEE_API_SAMPLE]
    since = _newest_transaction_sid()
    if since is None:
        return j.refuse("could not read account_transaction")
    before = {}
    for account_id, customer_id, _ in sample:
        read = j.account(account_id, customer=customer_id)
        if read:
            before[account_id] = (customer_id, _money(read.get("balance")))
    call = run.ops.call("POST", world.PLATFORM_SCHEDULED_TASK.format(run.platform_uid,
                                                                     FEE_WITHDRAWALS))
    j.step("run the platform's daily FEE_WITHDRAWALS", call.status)
    j.expect(call.ok, "the daily platform fee withdrawal runs",
             "POST FEE_WITHDRAWALS/sync for platform {} answered {}".format(
                 run.platform_uid, call.status), "2xx", call.status, body=call.body)
    if not call.ok:
        return j.done(note="the fee withdrawal did not run")
    run.fee_withdrawal_day = today
    uids = [a[0] for a in accounts]
    rows, error = fee_rows(account_uids=uids)
    if rows is None:
        return j.done(note="could not read the fee rows: {}".format(error))
    found, stats = fee_withdrawal_findings(rows, since_sid=since)
    for finding in found:
        j.expect(False, finding["rule"], finding["detail"], finding["expected"],
                 finding["actual"])
    j.step("judge the FEES rows", stats)
    withdrawn = {r[0] for r in rows if r[6] == "FEES" and r[3] and int(r[3]) > since}
    missed = [a for a in uids if a not in withdrawn]
    j.expect(not missed, "the daily fee withdrawal takes every account's realised platform fee",
             "{} of {} accounts that held a platform fee before the run got no FEES row: {}"
             .format(len(missed), len(uids), [m[:8] for m in missed[:10]]), 0, len(missed))
    fees_uids = {r[4] for r in rows if r[6] == "FEES" and r[3] and int(r[3]) > since}
    for account_id, (customer_id, balance) in before.items():
        # The clock and the population keep booking on these accounts, so the customer money
        # booked since the first read is taken on both sides of the second read.
        moved_before = _customer_money_since([account_id], since).get(account_id, ZERO)
        read = j.account(account_id, customer=customer_id)
        after_rows = j.transactions(account_id, customer=customer_id) or []
        moved_after = _customer_money_since([account_id], since).get(account_id, ZERO)
        shown_balance = _money(read.get("balance"))
        j.expect(not read or shown_balance.compare(balance + moved_before) == 0
                 or shown_balance.compare(balance + moved_after) == 0,
                 "a platform fee withdrawal moves no customer money",
                 "account {} read {} before the fee withdrawal and {} after it, with {} of "
                 "customer money booked meanwhile".format(account_id, balance,
                                                          read.get("balance"), moved_after),
                 balance + moved_after, read.get("balance"))
        shown = {r.get("transactionId") for r in after_rows} & fees_uids
        j.expect(not shown, "a platform fee withdrawal adds nothing to the customer's "
                 "transaction list", "account {} lists FEES transactions {}".format(
                     account_id, sorted(shown)), "none", sorted(shown))

    since_second = _newest_transaction_sid()
    again = run.ops.call("POST", world.PLATFORM_SCHEDULED_TASK.format(run.platform_uid,
                                                                      FEE_WITHDRAWALS))
    rows, _ = fee_rows(account_uids=uids)
    found, stats = fee_withdrawal_findings(rows, since_sid=since_second or since)
    for finding in found:
        j.expect(False, finding["rule"], finding["detail"], finding["expected"],
                 finding["actual"])
    j.step("run FEE_WITHDRAWALS again straight away", "{}; {}".format(again.status, stats))
    j.expect(again.ok, "a second fee withdrawal run on the same day is harmless",
             "the second POST FEE_WITHDRAWALS/sync answered {}".format(again.status), "2xx",
             again.status, body=again.body)

    j.next_day()
    j.oracle([a for a, _, _ in sample], "a day after the fee withdrawal")
    return j.done()


def _scheduled_fee_withdrawals(run, j):
    """On a fake clock PlatformScheduleManager runs FEE_WITHDRAWALS itself at 04:30 London, so the
    journey judges the FEES rows written since it last looked instead of running the job."""
    since = getattr(run, "fee_judged_sid", 0)
    newest = _newest_transaction_sid()
    if newest is None:
        return j.refuse("could not read account_transaction")
    rows, error = fee_rows(platform_uid=run.platform_uid)
    if rows is None:
        return j.done(note="could not read the fee rows: {}".format(error))
    found, stats = fee_withdrawal_findings(rows, since_sid=since)
    for finding in found:
        j.expect(False, finding["rule"], finding["detail"], finding["expected"],
                 finding["actual"])
    j.step("judge the FEES rows the scheduled withdrawal wrote after row {}".format(since), stats)
    run.fee_judged_sid = newest
    return j.done()


# -- an operator's review of a payee ------------------------------------------------------------

# Modulus-valid pairs from the Vocalink specification's examples, none used elsewhere in the
# harness, so each payee is its own external account in clearing.
REVIEW_PAYEES = {"A": ("820000", "73688637"), "B": ("827101", "28748352"),
                 "C": ("134020", "63849203")}
VERIFICATION = "/operations/own/customer/platform/customer/{}/nominatedAccounts/{}/verification/GBP/{}"


def _cop_required(platform_uid):
    rows, _ = interest_oracle._psql(
        "SELECT c.cop_verification_required FROM custom_platform_config c JOIN partner_platform "
        "pp ON pp.sid = c.platform_sid WHERE pp.uid = '{}'".format(platform_uid), timeout=30)
    return bool(rows) and rows[0][0] == "t"


def _link(customer, pair):
    """(payee_account uid, verification state) of the customer's active link to this pair."""
    rows, _ = interest_oracle._psql(
        "SELECT pa.uid, l.verification_state FROM cash_account_nominated_account_link l "
        "JOIN payee_account pa ON pa.sid = l.payee_account_sid "
        "JOIN platform_customer pc ON pc.sid = l.customer_sid "
        "WHERE pc.uid = '{}' AND l.is_active AND pa.account_identifier->>'value' = '{}' "
        "ORDER BY l.sid DESC LIMIT 1".format(customer, pair[0] + pair[1]), timeout=30)
    return (rows[0][0], rows[0][1]) if rows else (None, None)


def _wait_for_state(customer, pair, settled=("AWAITING_REVIEW", "VERIFIED", "REJECTED"),
                    seconds=45):
    """Confirmation of Payee answers asynchronously through clearing and the bank simulator."""
    deadline = time.monotonic() + seconds
    while True:
        uid, state = _link(customer, pair)
        if state in settled or time.monotonic() >= deadline:
            return uid, state
        time.sleep(2)


def payee_review(run):
    """A payee that fails Confirmation of Payee waits in AWAITING_REVIEW, and an operator decides
    it through NominatedAccountVerificationOpsService: accepted, it becomes VERIFIED and is paid
    once; rejected, it becomes REJECTED and no withdrawal reaches it; a repeated or late decision
    is refused without a server error; and a fresh payee that passes brings withdrawals back.
    The harness left 111 payees in AWAITING_REVIEW and never decided one.

    Every payee change is made with nothing pending, so FINDINGS.md 24 and 25 are not repeated."""
    j = Journey(run, "JourneyPayeeReview")
    if not j.statements_reachable():
        return j.refuse(POLL_WINDOW_NOTE)
    instant = j.product("INSTANT")
    if not instant or not run.ops:
        return j.refuse("no INSTANT product or ops client")
    if not _cop_required(run.platform_uid):
        return j.refuse("the platform does not require Confirmation of Payee")
    if not j.new_customer():
        return j.refuse("the customer did not become ACTIVATED")
    account = j.open(instant, "INSTANT")
    if not account:
        return j.refuse("the INSTANT account did not open")
    funded = Decimal("10.00")
    j.fund([(account, funded)])
    if not j.settle_until(lambda: _money(j.account(account["accountId"]).get("balance")) >= 10
                          and not j.pending()):
        return j.done(note="the funding did not land")
    tag = run.mint("", 8)[-6:]
    paid = ZERO

    def settled():
        return not [r for r in j.pending(account["accountId"]) if r.get("type") == "WITHDRAWAL"]

    def payouts_settled():
        for _ in range(settle_rounds(10)):
            if settled():
                return True
            time.sleep(SETTLE_PAUSE)
            j.settle_payouts()
        return settled()

    def publish():
        for _ in range(2):
            time.sleep(SETTLE_PAUSE)
            run.settle_world()

    def decide(pair, decision):
        uid, _ = _link(j.customer, pair)
        call = run.ops.call("POST", VERIFICATION.format(j.customer, uid, decision), json_body={})
        j.step("the operator's {} of {}".format(decision, pair[1]), call.status)
        return call

    def refused_cleanly(call, what):
        j.expect(not call.ok and call.status < 500,
                 "a decision on a payee that is no longer awaiting review is refused, not a "
                 "server error", "{} answered {}".format(what, call.status), "a 4xx",
                 call.status, body=call.body)

    def withdraw_to(amount, pair):
        started = clock.time() - clock.system_seconds(2)
        call = j.instruct(account, "WITHDRAW", amount)
        j.step("withdraw {}".format(amount), call.status)
        if not call.ok:
            return call, False
        done = payouts_settled()
        dues = [d for d in _payouts_since(j.customer, started) if _money(d[1]) == amount]
        live = [d for d in dues if d[2] not in ("CANCELLED", "FAILED")]
        j.expect(bool(live) and all(pair[1] in d[3] for d in live),
                 "a payout goes to the nominated account active when it is raised",
                 "the {} withdrawal's payout dues name {}; {} was accepted on review and "
                 "published before it".format(amount, sorted({d[3] for d in dues}), pair[1]),
                 pair[1], sorted({d[3] for d in dues}), body=dues)
        j.expect(len(live) <= 1, "a withdrawal pays out once",
                 "the {} withdrawal has {} live payout dues".format(amount, len(live)), 1,
                 len(live), body=dues)
        return call, done and bool(live)

    # Accept.
    if not _nominate(j, "Review A CLOSEMATCH {}".format(tag), REVIEW_PAYEES["A"]).ok:
        return j.done(note="the first payee change was refused")
    _, state = _wait_for_state(j.customer, REVIEW_PAYEES["A"])
    # NominatedAccountVerificationService.applyCopOutcome sends CLOSEMATCH to AWAITING_REVIEW.
    j.step("the CLOSEMATCH payee after Confirmation of Payee", state)
    if not j.expect(state == "AWAITING_REVIEW",
                    "a payee that fails Confirmation of Payee waits for review",
                    "a CLOSEMATCH payee reads {}".format(state), "AWAITING_REVIEW", state):
        return j.done(note="the payee never reached review")
    accepted = decide(REVIEW_PAYEES["A"], "accept")
    _, state = _link(j.customer, REVIEW_PAYEES["A"])
    j.expect(accepted.ok and state == "VERIFIED", "an operator's accept verifies the payee",
             "the accept answered {} and the payee reads {}".format(accepted.status, state),
             "2xx and VERIFIED", "{} and {}".format(accepted.status, state),
             body=accepted.body)
    refused_cleanly(decide(REVIEW_PAYEES["A"], "accept"), "a second accept of a VERIFIED payee")
    if state != "VERIFIED":
        return j.done(note="the accept did not verify the payee")
    publish()
    call, done = withdraw_to(Decimal("1.11"), REVIEW_PAYEES["A"])
    j.expect(call.ok, "a payee accepted on review can be paid",
             "a 1.11 withdrawal after the accept answered {}".format(call.status), "2xx",
             call.status, body=call.body)
    if call.ok and done:
        paid += Decimal("1.11")
    if not settled():
        return j.done(note="the 1.11 withdrawal is still pending, so the payee is not changed")

    # Reject.
    if not _nominate(j, "Review B NOMATCH {}".format(tag), REVIEW_PAYEES["B"]).ok:
        return j.done(note="the second payee change was refused")
    _, state = _wait_for_state(j.customer, REVIEW_PAYEES["B"])
    j.step("the NOMATCH payee after Confirmation of Payee", state)
    if state == "AWAITING_REVIEW":
        rejected = decide(REVIEW_PAYEES["B"], "reject")
        _, state = _link(j.customer, REVIEW_PAYEES["B"])
        j.expect(rejected.ok and state == "REJECTED", "an operator's reject rejects the payee",
                 "the reject answered {} and the payee reads {}".format(rejected.status, state),
                 "2xx and REJECTED", "{} and {}".format(rejected.status, state),
                 body=rejected.body)
        refused_cleanly(decide(REVIEW_PAYEES["B"], "accept"), "an accept after a reject")
        refused_cleanly(decide(REVIEW_PAYEES["B"], "re-check"), "a re-check after a reject")
        publish()
        started = clock.time() - clock.system_seconds(2)
        call = j.instruct(account, "WITHDRAW", Decimal("0.50"))
        j.step("withdraw 0.50 with the payee rejected", call.status)
        j.expect(not call.ok, "a payee rejected on review cannot be paid",
                 "a 0.50 withdrawal with the payee REJECTED answered {}".format(call.status),
                 "a 4xx", call.status, body=call.body)
        if call.ok:
            payouts_settled()
        to_b = [d for d in _payouts_since(j.customer, started)
                if REVIEW_PAYEES["B"][1] in d[3] and d[2] not in ("CANCELLED", "FAILED")]
        j.expect(not to_b, "no payout goes to a payee rejected on review",
                 "payout dues to the rejected payee: {}".format(to_b), "none", len(to_b),
                 body=to_b)
        if not settled():
            return j.done(note="a withdrawal is pending with the payee rejected")
    else:
        j.expect(False, "a payee that fails Confirmation of Payee waits for review",
                 "a NOMATCH payee reads {}".format(state), "AWAITING_REVIEW", state)

    # Recover with a payee that passes.
    third = _nominate(j, "Review C {}".format(tag), REVIEW_PAYEES["C"])
    if third.status >= 500 and (j.run.live_fault() or fleet.peer_fault()):
        # A 5xx while a restart or cut is in force is the fault working; no_server_error files it
        # under its own rule, so this expectation does not judge it.
        return j.refuse("the payee change answered {} while {} was in force".format(
            third.status, j.run.live_fault() or fleet.peer_fault()))
    if not j.expect(third.ok, "a customer whose payee was rejected can nominate another",
                    "the payee change after the rejection answered {}".format(third.status),
                    "2xx", third.status, body=third.body):
        return j.done(note="the payee change after the rejection was refused")
    _, state = _wait_for_state(j.customer, REVIEW_PAYEES["C"])
    j.step("the MATCH payee after Confirmation of Payee", state)
    if state == "VERIFIED":
        publish()
        call, done = withdraw_to(Decimal("0.75"), REVIEW_PAYEES["C"])
        j.expect(call.ok, "a new payee that passes Confirmation of Payee brings withdrawals back",
                 "a 0.75 withdrawal to the new payee after a rejected one answered {}".format(
                     call.status), "2xx", call.status, body=call.body)
        if call.ok and done:
            paid += Decimal("0.75")
    if settled():
        j.conserved(funded, paid, "the payee review")
    return j.done()


# -- the Direct API's read models ---------------------------------------------------------------

LIST = "/direct/v1/customers/{}/accounts/transactions?paginatedProperty=CREATED_AT&orderAscDesc=ASC"
ACCOUNT_LIST = ("/direct/v1/customers/{}/accounts/{}/transactions?paginatedProperty=CREATED_AT"
                "&orderAscDesc=ASC")
PAGE = 7
READ_MODEL_CUSTOMERS = 4


def _key(row):
    return (row.get("transactionId"), row.get("accountId"), str(_money(row.get("amount"))),
            row.get("type"), str(row.get("valueDate")))


def union_findings(customer_rows, account_rows_before, account_rows_after):
    """The customer-level list against the per-account lists read on both sides of it. Only rows
    present in both per-account reads are held to account, so a transaction booked during the
    reads is neither missing nor extra."""
    stable = {_key(r) for r in account_rows_before} & {_key(r) for r in account_rows_after}
    either = {_key(r) for r in account_rows_before} | {_key(r) for r in account_rows_after}
    listed = [_key(r) for r in customer_rows]
    missing = sorted(stable - set(listed))
    extra = sorted({k for k in listed if k not in either})
    twice = sorted({k for k in listed if listed.count(k) > 1})
    return missing, extra, twice


def paging_findings(full, pages):
    """Pages walked with skip/take under one ordering must be the full list, each row once and in
    the same order. Judged only when nothing was booked during the walk; the caller checks."""
    walked = [r.get("transactionId") for page in pages for r in page]
    wanted = [r.get("transactionId") for r in full]
    twice = sorted({t for t in walked if walked.count(t) > 1})
    missing = [t for t in wanted if t not in walked]
    return walked == wanted, twice, missing


def filter_findings(full_before, full_after, filtered, predicate):
    """A filtered list against the full list filtered here. A row outside the filter is wrong
    whenever it was read; a row inside it is owed only when both full reads hold it."""
    outside = [r.get("transactionId") for r in filtered if not predicate(r)]
    stable = ({r.get("transactionId") for r in full_before if predicate(r)}
              & {r.get("transactionId") for r in full_after if predicate(r)})
    missing = sorted(stable - {r.get("transactionId") for r in filtered})
    return outside, missing


def _busy_customers(platform_uid, limit):
    rows, _ = interest_oracle._psql("""
      SELECT pc.uid FROM platform_customer pc
      JOIN partner_platform pp ON pp.sid = pc.platform_sid
      JOIN customer_account ca ON ca.platform_customer_sid = pc.sid
      JOIN customer_product_account cpa ON cpa.customer_account_sid = ca.sid
      JOIN account_transaction t ON t.customer_product_account_sid = cpa.sid
      WHERE pp.uid = '{}' AND pc.verification_status = 'ACTIVATED'
      GROUP BY pc.uid HAVING count(DISTINCT cpa.sid) > 1
      ORDER BY count(*) DESC LIMIT {}""".format(platform_uid, limit), timeout=120)
    return [r[0] for r in rows or []]


def read_models(run):
    """For this platform's busiest customers, every read model of the same money must agree:
    GET /customers/{id}/accounts/transactions (never called before) is the union of the
    per-account lists; paging it with skip/take walks each row once in order; each filter
    (transactionType, valueDateFrom/To, valueAmountFrom/To) returns the full list filtered;
    totalSize counts the rows; the customer balances' totalSavingsBalance is the sum of the
    account balances (customer_holdings_group_by_currency_account_tw_fn sums
    product_account_balance); and GET /products/{id} (never called before) reads as the list does.
    Reads only, so every run may take it on its own platform."""
    j = Journey(run, "JourneyReadModels")
    if not run.platform_uid:
        return j.refuse("no platform")
    customers = _busy_customers(run.platform_uid, READ_MODEL_CUSTOMERS)
    if not customers:
        return j.refuse("no ACTIVATED customer with transactions on two accounts")
    for customer in customers:
        j.customer = customer
        _judge_customer(j, customer)
    j.customer = None
    _judge_products(j)
    return j.done()


def _list(j, path):
    """The rows and totalSize at `path`; a full read (skip=0&take=1000) follows every page, since
    the long-lived bank's busiest customers hold more than 1000 rows."""
    call = j.client.call("GET", path)
    if not call.ok or not isinstance(call.body, dict):
        return None, None
    rows, total = _rows(call.body), call.body.get("totalSize")
    if "skip=0&take=1000" not in path:
        return rows, total
    while isinstance(total, int) and len(rows) < total:
        more = j.client.call("GET", path.replace("skip=0&", "skip={}&".format(len(rows)), 1))
        if not more.ok or not isinstance(more.body, dict) or not _rows(more.body):
            return None, None
        rows.extend(_rows(more.body))
    return rows, total


def _per_account(j, customer, account_ids):
    rows = []
    for account_id in account_ids:
        listed, _ = _list(j, ACCOUNT_LIST.format(customer, account_id) + "&skip=0&take=1000")
        if listed is None:
            return None
        rows.extend(listed)
    return rows


def _judge_customer(j, customer):
    accounts = j.accounts(customer) or {}
    if not accounts:
        j.step("read the accounts of {}".format(customer[:8]), "unreadable")
        return
    before = _per_account(j, customer, list(accounts))
    full, total = _list(j, LIST.format(customer) + "&skip=0&take=1000")
    after = _per_account(j, customer, list(accounts))
    if before is None or full is None or after is None:
        j.step("list the transactions of {}".format(customer[:8]), "unreadable")
        return
    missing, extra, twice = union_findings(full, before, after)
    j.expect(not (missing or extra or twice),
             "the customer transaction list is the union of its accounts' lists",
             "customer {} lists {} rows: {} missing, {} not on any account, {} twice".format(
                 customer, len(full), missing[:5], extra[:5], twice[:5]),
             "{} rows".format(len(before)), len(full),
             body={"missing": missing[:10], "extra": extra[:10], "twice": twice[:10]})
    if len(full) < 1000:
        j.expect(total == len(full), "totalSize counts the rows the list holds",
                 "customer {} listed {} rows with totalSize {}".format(customer, len(full),
                                                                       total),
                 len(full), total)

    size = max(PAGE, len(full) // 8 + 1)
    pages, skip = [], 0
    while skip <= len(full) + size:
        page, _ = _list(j, LIST.format(customer) + "&skip={}&take={}".format(skip, size))
        if page is None:
            break
        pages.append(page)
        if len(page) < size:
            break
        skip += size
    full_again, _ = _list(j, LIST.format(customer) + "&skip=0&take=1000")
    if full_again is not None and [r.get("transactionId") for r in full_again] == [
            r.get("transactionId") for r in full]:
        in_order, twice, missing = paging_findings(full, pages)
        j.expect(in_order, "paging through a customer's transactions returns each once, in order",
                 "customer {}: {} pages of {} walked {} rows, {} twice, {} missing, against {} "
                 "in one read".format(customer, len(pages), size,
                                      sum(len(p) for p in pages), twice[:5], missing[:5],
                                      len(full)),
                 len(full), sum(len(p) for p in pages))
    else:
        j.step("page the transactions of {}".format(customer[:8]),
               "not judged: a transaction was booked while paging")
        full_again = full_again or full

    filters = []
    for kind in sorted({r.get("type") for r in full if r.get("type")}):
        filters.append(("transactionType={}".format(kind),
                        lambda r, kind=kind: r.get("type") == kind))
    dates = sorted({str(r.get("valueDate")) for r in full if r.get("valueDate")})
    if dates:
        middle = dates[len(dates) // 2]
        filters.append(("valueDateFrom={}".format(middle),
                        lambda r, d=middle: str(r.get("valueDate")) >= d))
        filters.append(("valueDateTo={}".format(middle),
                        lambda r, d=middle: str(r.get("valueDate")) <= d))
        filters.append(("valueDateFrom={0}&valueDateTo={0}".format(middle),
                        lambda r, d=middle: str(r.get("valueDate")) == d))
    # DirectCustomerTransactionService compares the signed customer_amount.
    # MoneyString refuses a negative value, so no amount filter can name a debit alone.
    filters.append(("valueAmountFrom=0.01", lambda r: _money(r.get("amount")) >= Decimal("0.01")))
    filters.append(("valueAmountTo=1.00", lambda r: _money(r.get("amount")) <= Decimal("1.00")))
    for query, predicate in filters:
        listed, _ = _list(j, LIST.format(customer) + "&skip=0&take=1000&" + query)
        if listed is None:
            j.expect(False, "a valid transaction filter is answered",
                     "customer {} with {} did not answer a list".format(customer, query),
                     "a list", "none")
            continue
        outside, missing = filter_findings(full, full_again, listed, predicate)
        j.expect(not outside and not missing, "a filtered transaction list is the full list "
                 "filtered", "customer {} with {}: {} rows outside the filter, {} rows missing"
                 .format(customer, query, outside[:5], missing[:5]), "none",
                 "{} outside, {} missing".format(len(outside), len(missing)))
    j.step("the transaction views of {}".format(customer[:8]),
           "{} rows, {} pages, {} filters".format(len(full), len(pages), len(filters)))

    for _ in range(3):
        first = j.accounts(customer) or {}
        call = j.client.call("GET", "/direct/v1/customers/{}/balances".format(customer))
        second = j.accounts(customer) or {}
        if first and {k: v.get("balance") for k, v in first.items()} == {
                k: v.get("balance") for k, v in second.items()}:
            break
        time.sleep(2)
    else:
        j.step("the balances of {}".format(customer[:8]), "not judged: balances kept moving")
        return
    if not call.ok or not isinstance(call.body, dict):
        j.expect(False, "a customer's balances are readable",
                 "GET balances for {} answered {}".format(customer, call.status), "2xx",
                 call.status, body=call.body)
        return
    summed = sum((_money(a.get("balance")) for a in first.values()
                  if (a.get("currency") or "GBP") == "GBP"), ZERO)
    gbp = [b for b in call.body.get("balances") or [] if (b.get("currency") or "GBP") == "GBP"]
    shown = sum((_money(b.get("totalSavingsBalance")) for b in gbp), ZERO)
    j.expect(shown.compare(summed) == 0,
             "a customer's total savings balance is the sum of its accounts' balances",
             "customer {} shows totalSavingsBalance {} and its accounts hold {}".format(
                 customer, shown, summed), summed, shown, body=call.body)


def _judge_products(j):
    listed = j.client.call("GET", "/direct/v1/products")
    rows = _rows(listed.body) if listed.ok else []
    differ = []
    for row in rows:
        product_id = row.get("productId")
        one = j.client.call("GET", "/direct/v1/products/{}".format(product_id))
        if not one.ok:
            differ.append((product_id, one.status))
            continue
        body = one.body if isinstance(one.body, dict) else {}

        def differing(listed_row):
            # The list and the single read may carry different field sets; only the fields
            # both carry are held to agree.
            return sorted(k for k in set(listed_row or {}) & set(body)
                          if (listed_row or {}).get(k) != body.get(k))
        if differing(row):
            again = j.client.call("GET", "/direct/v1/products")
            now = next((r for r in _rows(again.body) if r.get("productId") == product_id), None)
            if differing(now):
                differ.append((product_id, differing(now)))
    j.expect(not differ, "a product reads the same by id as in the product list",
             "{} of {} products differ: {}".format(len(differ), len(rows), differ[:5]), "none",
             len(differ), body=differ[:10])
    j.step("the products by id", "{} products, {} differ".format(len(rows), len(differ)))


ALL = {
    "JourneyPlatformFeeWithdrawal": platform_fee_withdrawal,
    "JourneyPayeeReview": payee_review,
    "JourneyReadModels": read_models,
}
