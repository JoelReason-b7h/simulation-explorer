"""Multi-step journeys the driver runs end to end, each on a customer of its own.

A learned route strings together actions the run has already walked, and a written scenario in
explore.py is a list of action names with no expectation between them. A journey here is one
driven action that walks a whole life on a fresh customer and asserts what each step must have
left before it takes the next one, so an interaction nobody has tested is judged where it happens.

Every journey:
- makes its own customer, so the explorer's pool and its bookkeeping are untouched;
- records each step and its outcome, and each broken expectation through Run.note_violation;
- takes a stack-wide step (a bank rate change, a business date advance) only on the conductor,
  or on the run that keeps the long-lived bank's clock, and otherwise waits for one;
- answers 412 when a precondition it needs could not be reached, and 200 once it ran to the end,
  whatever it found.

None of them re-triggers a defect already in FINDINGS.md: no closure while a withdrawal is in
flight, no close after a reversal, no deposit straddling a date flip before a feed, no duplicate
delivery, and no operator decision on a held payment group.
"""

from __future__ import annotations

import contextlib
import time
from datetime import date, timedelta
from decimal import Decimal

from explorer import clock, fleet, interest_oracle, longlived, race, world
from explorer.client import Call

ZERO = Decimal("0")
SETTLE_ROUNDS = 6
SETTLE_PAUSE = 3
DAY_WAIT_SECONDS = 90
# On a fake clock a journey waits for the 20:00 London accrual only when it is this close.
ACCRUAL_WAIT_CAP_SECONDS = 300
ACCRUAL_AT_LONDON = (20, 0)


# clearing's HsbcAccountStatementPollingScheduler.pollForTransactions, cron "*/5 6-23 * * *" in
# Europe/London: the first poll of a day is at 06:00 and the last at 23:55. A deposit or a payout
# settles only when a poll reads the bank's statement, so between 23:55 and 06:00 nothing lands.
POLL_FIRST, POLL_LAST, POLL_EVERY_SECONDS = (6, 0), (23, 55), 300
# A journey that settles runs for up to this long, and waits at most POLL_WAIT_CAP_SECONDS for the
# window to open. Both are real seconds.
JOURNEY_REAL_SECONDS = 240
POLL_WAIT_CAP_SECONDS = 300
POLL_WINDOW_NOTE = "not waited: outside the statement poll window"


def poll_window_wait(real_budget=JOURNEY_REAL_SECONDS):
    """On a fake clock, the real seconds to wait before a journey that needs statement polls can
    run: 0 when the window stays open for `real_budget`, the wait until a minute after 06:00 when
    that is under POLL_WAIT_CAP_SECONDS, or None. Always 0 on the real clock, where the harness
    polls the statement itself."""
    if not clock.schedulers_run():
        return 0
    now = clock.london_now()
    first = now.replace(hour=POLL_FIRST[0], minute=POLL_FIRST[1], second=0, microsecond=0)
    last = now.replace(hour=POLL_LAST[0], minute=POLL_LAST[1], second=0, microsecond=0)
    ends = now + timedelta(seconds=clock.system_seconds(real_budget))
    if first <= now and ends <= last:
        return 0
    opens = first if now < first else first + timedelta(days=1)
    wait = clock.real_seconds((opens - now).total_seconds() + 60)
    return wait if wait <= POLL_WAIT_CAP_SECONDS else None


def settle_rounds(rounds):
    """Settle rounds of SETTLE_PAUSE real seconds. On a fake clock enough of them for a statement
    poll and the minute crons after it: the scheduler polls every five fake minutes, where on the
    real clock the harness polls on each round."""
    if not clock.schedulers_run():
        return rounds
    needed = clock.real_seconds(POLL_EVERY_SECONDS + 120) / SETTLE_PAUSE
    return max(rounds, int(needed) + 1)


def accrual_wait_seconds():
    """Real seconds until a minute after the next 20:00 London on the fake clock, or None when
    that is further off than ACCRUAL_WAIT_CAP_SECONDS."""
    now = clock.london_now()
    due = now.replace(hour=ACCRUAL_AT_LONDON[0], minute=ACCRUAL_AT_LONDON[1], second=0,
                      microsecond=0)
    if due <= now:
        due += timedelta(days=1)
    wait = clock.real_seconds((due - now).total_seconds() + 60) + DAY_WAIT_SECONDS
    return wait if wait <= ACCRUAL_WAIT_CAP_SECONDS + DAY_WAIT_SECONDS else None

# England bank holidays the maturity date rolls over (BondsmithBankCustomMaturityDateFormula
# snaps onto NationalHolidayService.getWorkingDayOnOrAfter). Weekends are handled apart.
ENGLAND_HOLIDAYS = {
    date(2026, 12, 25), date(2026, 12, 28), date(2027, 1, 1), date(2027, 3, 26),
    date(2027, 3, 29), date(2027, 5, 3), date(2027, 5, 31), date(2027, 8, 30),
    date(2027, 12, 27), date(2027, 12, 28), date(2028, 1, 3), date(2028, 4, 14),
    date(2028, 4, 17), date(2028, 5, 1), date(2028, 5, 29), date(2028, 8, 28),
    date(2028, 12, 25), date(2028, 12, 26),
}


def _rows(body):
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        return body.get("content") or body.get("allocations") or []
    return []


def _money(value):
    try:
        return Decimal(str(value if value is not None else "0"))
    except (ArithmeticError, ValueError):
        return ZERO


def _first_failure(steps):
    for step in steps:
        for call in (step if isinstance(step, (list, tuple)) else [step]):
            if call is not None and not getattr(call, "ok", True):
                return call
    return None


def add_months(day, months):
    month = day.month - 1 + months
    year = day.year + month // 12
    month = month % 12 + 1
    last = [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28, 31, 30, 31, 30,
            31, 31, 30, 31, 30, 31][month - 1]
    return date(year, month, min(day.day, last))


def working_day_on_or_after(day):
    while day.weekday() >= 5 or day in ENGLAND_HOLIDAYS:
        day += timedelta(days=1)
    return day


class Journey:
    """One walk: the run it belongs to, its steps so far, and the assertions it made."""

    def __init__(self, run, name):
        self.run = run
        self.name = name
        self.steps = []
        self.found = 0
        self.customer = None
        self.started = time.time()
        # A transfer books a SAVINGS_WITHDRAWAL on the source and a SAVINGS_DEPOSIT on the
        # destination with no payment reference, so the Direct API lists its legs as WITHDRAWAL
        # and DEPOSIT. The journey counts what it moved to tell them from the money it funded.
        self.moved = ZERO

    # -- bookkeeping ------------------------------------------------------------------------

    def step(self, what, outcome):
        self.steps.append({"step": what, "outcome": str(outcome)[:300],
                           "at": round(time.time() - self.started, 1)})
        print("    · {} {}: {}".format(self.name, what, str(outcome)[:160]))

    def expect(self, holds, rule, detail, expected, actual, body=None):
        if holds:
            return True
        self.found += 1
        self.run.note_violation(self.name, rule, self.customer or "?", detail, expected, actual,
                                body=body)
        return False

    def done(self, status=200, note=None):
        record = {"journey": self.name, "trial": self.run.steps, "customerId": self.customer,
                  "status": status, "violations": self.found, "steps": self.steps,
                  "seconds": round(time.time() - self.started, 1), "note": note}
        journeys = getattr(self.run, "journeys", None)
        if journeys is None:
            journeys = self.run.journeys = []
        journeys.append(record)
        del journeys[:-40]
        message = "{}: {} steps, {} broken expectations{}".format(
            self.name, len(self.steps), self.found, "; " + note if note else "")
        return Call("POST", self.name, status, {"message": message, "steps": self.steps}, 0)

    def refuse(self, note):
        return self.done(412, note)

    # -- the calls a journey is made of -------------------------------------------------------

    @property
    def client(self):
        return self.run.client

    def new_customer(self):
        from explorer import actions
        body = actions.person_body(self.run.mint, lambda reference: "Pass")
        # A payee of the journey's own. The explorer's customers all nominate 100000 41610008,
        # and its AddUnverifiableNominatedAccount renames that account to a CoP-failing name, so
        # a journey customer on the same pair came out AWAITING_REVIEW about one time in ten.
        body["nominatedAccounts"][0]["accountName"] = "Journey payee"
        body["nominatedAccounts"][0]["ukAccountDetails"] = {"sortCode": "871427",
                                                            "accountNumber": "46238510"}
        call = self.client.call("POST", "/direct/v1/customers", json_body=body)
        if not call.ok or not isinstance(call.body, dict):
            self.step("create a customer", call.status)
            return None
        self.customer = call.body.get("customerId")
        activated = self.run.activate_for_deposit(self.customer)
        self.step("create a customer and wait for ACTIVATED", "ACTIVATED" if activated else
                  "not ACTIVATED")
        if activated:
            self.step("wait for the payee to pass Confirmation of Payee",
                      "usable" if self.payee_usable() else "not usable yet")
        return self.customer if activated else None

    def payee_usable(self, seconds=30):
        """Wait for the active nominated account to be usable for a withdrawal. Confirmation of
        Payee answers through clearing and the bank simulator after the customer is made, and
        NominatedAccountUsableOrderValidator refuses a withdrawal until it has."""
        deadline = time.monotonic() + seconds
        while True:
            rows, _ = interest_oracle._psql(
                "SELECT l.verification_state FROM cash_account_nominated_account_link l "
                "JOIN platform_customer pc ON pc.sid = l.customer_sid "
                "WHERE pc.uid = '{}' AND l.is_active".format(self.customer), timeout=30)
            if rows and rows[0][0] in ("VERIFIED", "NOT_REQUIRED"):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(2)

    def product(self, label):
        """The platform product id for a label from Run.products, e.g. 'TERM 1 month'."""
        found = self.run.products.get(label)
        return found[1] if found else None

    def open(self, product_id, product_type, amount=None):
        body = {"productId": product_id, "accountReference": self.run.mint("acct", 18),
                "termsAndConditionsAcceptedAt": _past_instant()}
        if product_type == "TERM":
            body["amount"] = "{:.2f}".format(amount or Decimal("50.00"))
        call = self.client.call("POST", "/direct/v1/customers/{}/accounts".format(self.customer),
                                json_body=body)
        self.step("open a {} account".format(product_type), call.status)
        if not call.ok or not isinstance(call.body, dict):
            return None
        return {"accountId": call.body.get("accountId"),
                "accountReference": call.body.get("accountReference") or body["accountReference"],
                "productId": product_id, "productType": product_type}

    def fund(self, lines, customer=None):
        """One batch across the given (account, amount) lines, one credit, one settle sequence."""
        run = self.run
        total = sum((amount for _, amount in lines), ZERO)
        batch = self.client.call("POST", "/direct/v1/batches", json_body={
            "batchReference": run.mint("batch", 36),
            "paymentReference": run.mint("p", 16),
            "totalPaymentRequired": "{:.2f}".format(total),
            "allocations": [{
                "customerId": account.get("customerId") or customer or self.customer,
                "accountReference": account["accountReference"],
                "instructionReference": run.mint("i", 36),
                "instructionType": "DEPOSIT",
                "productId": account["productId"],
                "amount": "{:.2f}".format(amount),
            } for account, amount in lines],
        })
        # Whether the batch was accepted, apart from the settle steps after it: ops-api answers
        # 500 when clearing takes over 30 s, and the deposit still lands on a later sweep.
        self.batch_accepted = batch.ok
        self.batch_answer = (batch.status, batch.body)
        if not batch.ok:
            return batch
        steps = [
            world.raise_platform_dues(run.ops, run.platform_uid),
            run.credit_and_count("{:.2f}".format(total), batch.body["paymentReference"]),
            run.poll_if_new_money(),
            world.drain_transactions(run.ops),
            world.settle_payments(run.ops),
            world.drain_transactions(run.ops),
        ]
        run.sweeps += 1
        return _first_failure(steps) or batch

    def accounts(self, customer=None):
        call = self.client.call("GET", "/direct/v1/customers/{}/accounts".format(
            customer or self.customer))
        return {a.get("accountId"): a for a in _rows(call.body)} if call.ok else None

    def account(self, account_id, customer=None):
        call = self.client.call("GET", "/direct/v1/customers/{}/accounts/{}".format(
            customer or self.customer, account_id))
        return call.body if call.ok and isinstance(call.body, dict) else {}

    def transactions(self, account_id, customer=None):
        return self.run.account_transactions(customer or self.customer, account_id)

    def booked(self, account_id, kind="SAVINGS_DEPOSIT"):
        """The sum of the account's transactions of one type in core, read-only. The Direct API's
        transaction listing answers "Customer not found" for some customers it still serves."""
        rows, _ = interest_oracle._psql(
            "SELECT coalesce(sum(t.customer_amount), 0) FROM account_transaction t "
            "JOIN customer_product_account cpa ON cpa.sid = t.customer_product_account_sid "
            "WHERE cpa.uid = '{}' AND t.transaction_type = '{}'".format(account_id, kind))
        return _money(rows[0][0]) if rows else None

    def settle_payouts(self):
        """A settle that also learns what the bank did with each payment sent, which is what
        completes a withdrawal: the bank's debit comes back through the statement."""
        run = self.run
        world.enquire_payment_status(run.ops)
        world.poll_bank_transactions(run.ops)
        world.drain_transactions(run.ops)
        run.settle_world()

    def instructions(self, customer=None):
        call = self.client.call("GET", "/direct/v1/customers/{}/instructions".format(
            customer or self.customer))
        return _rows(call.body) if call.ok else None

    def pending(self, account_id=None):
        rows = self.instructions() or []
        return [r for r in rows if r.get("status") == "PENDING"
                and (account_id is None or r.get("accountId") == account_id)]

    def statements_reachable(self):
        """Whether this journey's deposits and payouts can settle: on a fake clock, the statement
        poll window is open or opens soon enough to wait for. The step says which."""
        wait = poll_window_wait()
        if wait is None:
            self.step("wait for the statement poll window", "{} (06:00 to 23:55 London); it is "
                      "{} London".format(POLL_WINDOW_NOTE, clock.london_now().strftime("%H:%M")))
            return False
        if wait:
            self.step("wait for the statement poll window",
                      "{:.0f} real seconds until 06:00 London".format(wait))
            time.sleep(wait)
        return True

    def settle_until(self, done, rounds=SETTLE_ROUNDS):
        """Settle and re-read until `done()` answers true, and answer its last value."""
        value = done()
        for _ in range(settle_rounds(rounds)):
            if value:
                return value
            time.sleep(SETTLE_PAUSE)
            self.run.settle_world()
            value = done()
        return value

    def instruct(self, account, kind, amount, destination=None):
        body = {"instructionRequestType": kind, "productId": destination or account["productId"],
                "amount": "{:.2f}".format(amount),
                "instructionReference": self.run.mint("w" if kind == "WITHDRAW" else "t", 36)}
        if kind == "TRANSFER":
            body["termsAndConditionsAcceptedAt"] = _past_instant()
        call = self.client.call(
            "POST", "/direct/v1/customers/{}/accounts/{}/instruction".format(
                self.customer, account["accountId"]), json_body=body)
        if kind == "TRANSFER" and call.ok:
            self.moved += amount
        return call

    def money(self):
        """(sum of balances, {type: sum of amounts}) over every account the customer holds."""
        accounts = self.accounts()
        if accounts is None:
            return None
        total, by_type = ZERO, {}
        for account_id, account in accounts.items():
            total += _money(account.get("balance"))
            rows = self.transactions(account_id)
            if rows is None:
                return None
            for row in rows:
                by_type[row.get("type")] = by_type.get(row.get("type"), ZERO) + _money(
                    row.get("amount"))
        return total, by_type

    def conserved(self, funded, paid_out, label):
        """Across the customer's accounts: balances == funded - paid out + interest, and every
        transfer's two legs cancel."""
        read = self.money()
        if read is None:
            self.step("read the money at " + label, "unreadable")
            return
        total, by_type = read
        interest = by_type.get("INTEREST", ZERO)
        expected = funded - paid_out + interest
        self.expect(total.compare(expected) == 0,
                    "a customer's money is conserved across its accounts",
                    "at {}: the accounts hold {} against {} funded, {} paid out and {} interest"
                    .format(label, total, funded, paid_out, interest), expected, total,
                    body={k: str(v) for k, v in by_type.items()})
        self.expect(by_type.get("DEPOSIT", ZERO).compare(funded + self.moved) == 0,
                    "the deposits booked are the deposits funded and the transfers in",
                    "at {}: DEPOSIT rows sum to {} against {} funded and {} transferred".format(
                        label, by_type.get("DEPOSIT", ZERO), funded, self.moved),
                    funded + self.moved, by_type.get("DEPOSIT", ZERO))
        self.step("money conserved at " + label, "{} = {} - {} + {}".format(
            total, funded, paid_out, interest))

    def next_day(self):
        """A business date advance: made here on the clock keeper, waited for elsewhere."""
        run = self.run
        before = interest_oracle.business_date(run.bank_uid) if run.bank_uid else None
        if longlived.keeps_clock(run):
            moved = longlived.clock_for(run).tick(force=True)
            after = interest_oracle.business_date(run.bank_uid)
            self.step("advance the business date", "{} -> {}".format(before, after))
            return bool(moved) or after != before
        if not fleet.is_member() and not clock.schedulers_run():
            call = run.advance_business_day()
            after = interest_oracle.business_date(run.bank_uid)
            self.step("advance the business date", "{} {} -> {}".format(
                getattr(call, "status", None), before, after))
            return getattr(call, "ok", False)
        wait, who = DAY_WAIT_SECONDS, "the conductor"
        if clock.schedulers_run():
            # Only the bank's ACCRUALS_AND_REALISATIONS schedule moves the date, at 20:00 London.
            who = "the accrual job"
            wait = accrual_wait_seconds()
            if wait is None:
                self.step("wait for the accrual job to advance the business date",
                          "not waited: the next 20:00 London is more than {}s of real time away"
                          .format(ACCRUAL_WAIT_CAP_SECONDS))
                return False
        deadline = time.monotonic() + wait
        after = before
        while time.monotonic() < deadline:
            time.sleep(3)
            after = interest_oracle.business_date(run.bank_uid)
            if after != before:
                break
        self.step("wait for {} to advance the business date".format(who),
                  "{} -> {}".format(before, after))
        return after != before

    def oracle(self, account_ids, label):
        """The interest oracle over these accounts; each finding is this journey's finding."""
        found, stats, _ = interest_oracle.check(account_uids=[a for a in account_ids if a])
        for finding in found:
            # The journey's customer is whichever it made last, so the account goes in the detail.
            self.expect(False, finding["rule"], "account {}: {}".format(
                finding["subject"], finding["detail"]), finding["expected"], finding["actual"])
        self.step("interest oracle at " + label, "{} accruals, {} realisations, {} findings"
                  .format(stats.get("accrualsJudged"), stats.get("realisationsJudged"),
                          len(found)))
        return stats

    @contextlib.contextmanager
    def standing_on(self, subject):
        """Point the run at a synthetic subject for one of its own driven actions."""
        run = self.run
        saved = run.subjects[run.current]
        run.subjects[run.current] = subject
        try:
            yield
        finally:
            run.subjects[run.current] = saved


def _past_instant():
    return (clock.utcnow_naive() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _conductor_or_keeper(run):
    return not fleet.is_member() or longlived.keeps_clock(run)


# -- the journeys -----------------------------------------------------------------------------


def multi_product(run):
    """INSTANT, NOTICE and TERM funded from one batch; a transfer; a partial notice withdrawal; a
    bank rate change and the notice adjustment it owes; a day of interest; the notice withdrawal
    cancelled and the INSTANT account closed once nothing is in flight. Money is conserved across
    the customer's accounts at every settled point."""
    j = Journey(run, "JourneyMultiProduct")
    if not j.statements_reachable():
        return j.refuse(POLL_WINDOW_NOTE)
    instant, notice, term = (j.product("INSTANT"), j.product("NOTICE"), j.product("TERM 1 month"))
    if not (instant and notice and term):
        return j.refuse("the platform lacks one of INSTANT, NOTICE and the one-month TERM")
    if not j.new_customer():
        return j.refuse("the customer did not become ACTIVATED")
    a_instant = j.open(instant, "INSTANT")
    a_notice = j.open(notice, "NOTICE")
    a_term = j.open(term, "TERM", Decimal("50.00"))
    if not (a_instant and a_notice and a_term):
        return j.refuse("an account did not open")
    amounts = {a_instant["accountId"]: Decimal("20.00"), a_notice["accountId"]: Decimal("5.00"),
               a_term["accountId"]: Decimal("50.00")}
    funded = sum(amounts.values(), ZERO)
    call = j.fund([(a_instant, amounts[a_instant["accountId"]]),
                   (a_notice, amounts[a_notice["accountId"]]),
                   (a_term, amounts[a_term["accountId"]])])
    j.step("fund three accounts from one batch of {}".format(funded), call.status)

    def landed():
        read = j.accounts() or {}
        return all(_money(read.get(k, {}).get("balance")) >= v for k, v in amounts.items()) \
            and not j.pending()
    if not j.settle_until(landed):
        read = j.accounts() or {}
        j.expect(False, "a batch across three of a customer's products lands on each",
                 "after {} settle rounds the balances read {}".format(
                     SETTLE_ROUNDS, {k[:8]: v.get("balance") for k, v in read.items()}),
                 {k[:8]: str(v) for k, v in amounts.items()},
                 {k[:8]: v.get("balance") for k, v in read.items()})
        return j.done(note="the funding did not land, so the journey stops")
    term_read = j.account(a_term["accountId"])
    j.expect(term_read.get("status") == "OPEN" and term_read.get("maturityDate"),
             "a TERM account funded to its amount opens with a maturity date",
             "the TERM account reads {} with maturity {}".format(
                 term_read.get("status"), term_read.get("maturityDate")), "OPEN with a date",
             term_read.get("status"))
    j.conserved(funded, ZERO, "funding")

    before = j.accounts() or {}
    call = j.instruct(a_instant, "TRANSFER", Decimal("3.00"), destination=notice)
    j.step("transfer 3.00 from INSTANT to NOTICE", call.status)
    if call.ok:
        after = j.accounts() or {}
        moved_out = _money(before[a_instant["accountId"]].get("balance")) - _money(
            after.get(a_instant["accountId"], {}).get("balance"))
        moved_in = _money(after.get(a_notice["accountId"], {}).get("balance")) - _money(
            before[a_notice["accountId"]].get("balance"))
        # DirectTransferRequestProcessor.createInstructionsAndTransactions books both legs at once.
        j.expect(moved_out.compare(Decimal("3.00")) == 0 and moved_in.compare(Decimal("3.00")) == 0,
                 "a transfer books both legs at once",
                 "straight after the transfer INSTANT moved by {} and NOTICE by {}".format(
                     -moved_out, moved_in), "-3.00 and +3.00",
                 "{} and {}".format(-moved_out, moved_in))
    j.conserved(funded, ZERO, "the transfer")

    call = j.instruct(a_notice, "WITHDRAW", Decimal("1.00"))
    j.step("place a partial NOTICE withdrawal of 1.00", call.status)
    partial = call.body.get("instructionId") if call.ok and isinstance(call.body, dict) else None
    if partial is None:
        waiting = [r for r in j.pending(a_notice["accountId"]) if r.get("type") == "WITHDRAWAL"]
        partial = waiting[0].get("instructionId") if waiting else None

    if _conductor_or_keeper(run) and not fleet.is_member():
        with j.standing_on({"customerId": j.customer, "accountId": a_notice["accountId"],
                            "productId": notice, "productType": "NOTICE"}):
            rate = run.change_bank_rate()
        j.step("change the NOTICE bank rate", "{} {}".format(
            rate.status, (rate.body or {}).get("message") if isinstance(rate.body, dict) else ""))
        adjusted = run.ops.call("POST", run.ADJUST_PATH)
        time.sleep(3)
        j.step("run the notice withdrawal adjustment", adjusted.status)
        waiting = [r for r in j.pending(a_notice["accountId"]) if r.get("type") == "WITHDRAWAL"]
        balance = _money(j.account(a_notice["accountId"]).get("balance"))
        for row in waiting:
            j.expect(_money(row.get("amount")) <= balance,
                     "no pending notice withdrawal asks for more than its account holds",
                     "after the rate change the pending withdrawal asks {} of {}".format(
                         row.get("amount"), balance), "<= {}".format(balance), row.get("amount"))
    else:
        j.step("change the NOTICE bank rate", "skipped: a bank rate change is conductor-only")

    j.next_day()
    j.oracle([a_instant["accountId"], a_notice["accountId"], a_term["accountId"]], "a day on")
    j.conserved(funded, ZERO, "a day on")

    if partial:
        call = j.client.call("DELETE", "/direct/v1/customers/{}/accounts/{}/instruction/{}".format(
            j.customer, a_notice["accountId"], partial))
        j.step("cancel the partial NOTICE withdrawal", call.status)
        rows = {r.get("instructionId"): r for r in j.instructions() or []}
        # The instructions listing leaves cancelled instructions out, so absent counts as
        # cancelled and only a row still PENDING breaks the rule.
        j.expect(not call.ok or rows.get(partial, {}).get("status") in (None, "CANCELLED"),
                 "a cancelled notice withdrawal is no longer pending",
                 "the cancel answered {} and the instruction reads {}".format(
                     call.status, rows.get(partial, {}).get("status")), "CANCELLED or absent",
                 rows.get(partial, {}).get("status"))
    if j.pending():
        return j.done(note="an instruction is still in flight, so the account is not closed")
    instant_balance = _money(j.account(a_instant["accountId"]).get("balance"))
    call = j.client.call("POST", "/direct/v1/customers/{}/accounts/{}/close?reason=NO_LONGER_NEEDED"
                         .format(j.customer, a_instant["accountId"]))
    j.step("close the INSTANT account holding {}".format(instant_balance), call.status)
    if not call.ok:
        return j.done(note="the close was refused")
    # Closing brings the final realisation forward to the next day, and the closure sweep takes
    # the account once that realisation has closed the schedule off (DirectModelInterestProcessing).
    j.next_day()
    run.process_closures()

    finished = False
    for _ in range(SETTLE_ROUNDS):
        if j.account(a_instant["accountId"]).get("status") == "CLOSED":
            finished = True
            break
        time.sleep(SETTLE_PAUSE)
        run.settle_world()
        run.process_closures()
    j.step("the INSTANT account closes", "CLOSED" if finished else j.account(
        a_instant["accountId"]).get("status"))
    if finished:
        # closeAccount writes CLOSED before the payout's transfer settles, so the balance leaves
        # a settle or two after the status reads CLOSED.
        for _ in range(10):
            if _money(j.account(a_instant["accountId"]).get("balance")) == 0:
                break
            time.sleep(SETTLE_PAUSE)
            j.settle_payouts()
        if _money(j.account(a_instant["accountId"]).get("balance")) != 0:
            j.step("the closed INSTANT account", "still holds {} after 10 payout settles, left to "
                   "the closed-account sweep".format(j.account(a_instant["accountId"]).get(
                       "balance")))
            return j.done(note="the closure payout had not settled")
        paid = sum((-_money(r.get("amount")) for r in j.transactions(a_instant["accountId"]) or []
                    if r.get("type") == "WITHDRAWAL"), ZERO) - j.moved
        j.conserved(funded, paid, "the INSTANT closure")
        j.expect(_money(j.account(a_instant["accountId"]).get("balance")) == 0,
                 "a closed account holds no money", "the closed INSTANT account holds {}".format(
                     j.account(a_instant["accountId"]).get("balance")), "0.00",
                 j.account(a_instant["accountId"]).get("balance"))
    j.oracle([a_instant["accountId"], a_notice["accountId"], a_term["accountId"]], "the end")
    return j.done()


def term_before_maturity(run):
    """A one-month TERM funded to its amount: its maturity date follows the formula, a top-up is
    refunded, a withdrawal and a close are refused, a NOTICE maturity destination reads back, and a
    day on it accrues without realising (AT_MATURITY)."""
    j = Journey(run, "JourneyTermBeforeMaturity")
    if not j.statements_reachable():
        return j.refuse(POLL_WINDOW_NOTE)
    term, notice = j.product("TERM 1 month"), j.product("NOTICE")
    if not (term and notice):
        return j.refuse("the platform lacks the one-month TERM or the NOTICE product")
    if not j.new_customer():
        return j.refuse("the customer did not become ACTIVATED")
    a_term = j.open(term, "TERM", Decimal("50.00"))
    if not a_term:
        return j.refuse("the TERM account did not open")
    call = j.fund([(a_term, Decimal("50.00"))])
    j.step("fund the TERM account with 50.00", call.status)
    opened_on = clock.london_now().date()
    read = j.settle_until(lambda: (lambda a: a if a.get("status") == "OPEN" else None)(
        j.account(a_term["accountId"])))
    if not read:
        return j.done(note="the TERM account never opened")
    # CustomerProductAccountMaturityService.upsertMaturityDate takes the wall date and
    # BondsmithBankCustomMaturityDateFormula adds the term in months, then rolls onto a working day.
    wanted = working_day_on_or_after(add_months(opened_on, 1))
    j.expect(str(read.get("maturityDate"))[:10] == wanted.isoformat(),
             "a TERM matures on the first working day on or after opening plus its term",
             "funded on {} the one-month TERM matures on {}".format(opened_on,
                                                                   read.get("maturityDate")),
             wanted, read.get("maturityDate"))
    j.step("maturity date", read.get("maturityDate"))

    a_notice = j.open(notice, "NOTICE")
    call = j.client.call("POST", "/direct/v1/customers/{}/accounts/{}/maturityDestination".format(
        j.customer, a_term["accountId"]), json_body={"productId": notice})
    j.step("set the maturity destination to NOTICE", call.status)
    if call.ok and a_notice:
        destination = j.account(a_term["accountId"]).get("maturityDestination")
        j.expect(destination == a_notice["accountId"], "an accepted maturity destination reads back",
                 "the TERM account names {} as its destination".format(destination),
                 a_notice["accountId"], destination)

    # DirectTransactionDepositHandler refunds a deposit into a funded TERM (SAV-10844).
    call = j.fund([(a_term, Decimal("5.00"))])
    j.step("top up the funded TERM account with 5.00", call.status)
    time.sleep(SETTLE_PAUSE)
    for _ in range(3):
        run.settle_world()
    balance = _money(j.account(a_term["accountId"]).get("balance"))
    deposits = sum((_money(r.get("amount")) for r in j.transactions(a_term["accountId"]) or []
                    if r.get("type") == "DEPOSIT"), ZERO)
    j.expect(deposits.compare(Decimal("50.00")) == 0,
             "a funded TERM account takes no top-up",
             "after a 5.00 top-up the TERM account's deposits sum to {} and it holds {}".format(
                 deposits, balance), "50.00", deposits)

    call = j.instruct(a_term, "WITHDRAW", Decimal("1.00"))
    j.expect(not call.ok, "a TERM account refuses a withdrawal before maturity",
             "a 1.00 withdrawal from the TERM answered {}".format(call.status), "a 4xx",
             call.status, body=call.body)
    j.step("withdraw from the TERM account", call.status)
    call = j.client.call("POST", "/direct/v1/customers/{}/accounts/{}/close?reason=NO_LONGER_NEEDED"
                         .format(j.customer, a_term["accountId"]))
    j.expect(not call.ok, "a TERM account cannot be closed",
             "closing the TERM answered {}".format(call.status), "a 4xx", call.status,
             body=call.body)
    j.step("close the TERM account", call.status)

    j.next_day()
    interest = [r for r in j.transactions(a_term["accountId"]) or [] if r.get("type") == "INTEREST"]
    j.expect(not interest or str(read.get("maturityDate"))[:10] <= (
        interest_oracle.business_date(run.bank_uid) or date.min).isoformat(),
        "a TERM paying at maturity realises nothing before it",
        "the TERM account shows {} INTEREST rows before its maturity {}".format(
            len(interest), read.get("maturityDate")), "none", len(interest))
    j.oracle([a_term["accountId"]] + ([a_notice["accountId"]] if a_notice else []), "a day on")
    return j.done()


def _payouts_since(customer, since, name_like=None):
    """Clearing's payment dues to the customer's nominated accounts raised since `since`:
    [(due uid, amount, status, payee name, payee state)]."""
    from explorer import ledger
    rows = ledger._psql(
        "SELECT ppd.uid, abs(ppd.value_amount), ppd.payment_status, "
        "coalesce(ea.account_identifier::text, '') || ' ' || coalesce(ea.account_name, ''), "
        "ea.account_state, extract(epoch FROM ppd.created_at) "
        "FROM partner_payment_due ppd "
        "JOIN external_account ea ON ea.sid = ppd.external_counterpart_account_sid "
        "JOIN internal_account ia ON ia.sid = ppd.account_sid "
        "JOIN account_owner ao ON ao.sid = ia.account_owner_sid "
        "WHERE ao.uid = '{}' AND ppd.created_at >= to_timestamp({}) "
        "AND ppd.payment_direction = 'CASH_ACCOUNT_CUSTOMER' "
        "ORDER BY ppd.sid".format(customer, float(since)))
    return [tuple(r[:6]) for r in rows if len(r) >= 6]


def _payee_states(customer):
    """{sort code + account number: core's verification state} for the customer's payees."""
    rows, _ = interest_oracle._psql(
        "SELECT pa.account_identifier->>'value', l.verification_state "
        "FROM cash_account_nominated_account_link l "
        "JOIN payee_account pa ON pa.sid = l.payee_account_sid "
        "JOIN platform_customer pc ON pc.sid = l.customer_sid WHERE pc.uid = '{}'".format(customer))
    return {row[0]: row[1] for row in rows or []}


def _unverified_payouts(customer, dues):
    """The payouts whose payee core holds in a state other than VERIFIED or NOT_REQUIRED."""
    states = _payee_states(customer)
    bad = []
    for due in dues:
        for number, state in states.items():
            if number and number in due[3] and state not in ("VERIFIED", "NOT_REQUIRED"):
                bad.append(due + (state,))
    return bad


# Pairs that pass UK modulus checking (the Vocalink specification's own examples), so each payee
# is a distinct external account in clearing: re-nominating the same pair under another name
# leaves clearing's one external account, and its name, as they were.
PAYEES = {"A": ("089999", "66374958"), "B": ("107999", "88837491"), "C": ("202959", "63748472")}


def _nominate(j, name, pair=("100000", "41610008")):
    body = {"nominatedAccount": {
        "accountName": name, "currency": "GBP",
        "accountHolderAddress": {"addressLine1": "1 Other Road", "addressLine2": "Flat 2",
                                 "town": "London", "county": "Greater London",
                                 "postCode": "AB12 3CD", "country": "GBR"},
        "ukAccountDetails": {"sortCode": pair[0], "accountNumber": pair[1]}}}
    call = j.client.call("PATCH", "/direct/v1/customers/{}/nominated-account".format(j.customer),
                         json_body=body)
    j.step("nominate '{}'".format(name), call.status)
    return call


def payee_change(run):
    """Change the nominated account between withdrawals, each change made with nothing pending
    and published before the next withdrawal: every payout goes to the payee nominated when the
    withdrawal was placed, a payee that fails Confirmation of Payee refuses new withdrawals, and a
    verified payee brings them back, each paying out once.

    A change made while a withdrawal is pending is FINDINGS.md 24 and 25, so it is not repeated."""
    j = Journey(run, "JourneyPayeeChange")
    if not j.statements_reachable():
        return j.refuse(POLL_WINDOW_NOTE)
    instant = j.product("INSTANT")
    if not instant or not j.new_customer():
        return j.refuse("no INSTANT product, or the customer did not become ACTIVATED")
    account = j.open(instant, "INSTANT")
    if not account:
        return j.refuse("the INSTANT account did not open")
    j.fund([(account, Decimal("10.00"))])
    if not j.settle_until(lambda: _money(j.account(account["accountId"]).get("balance")) >= 10
                          and not j.pending()):
        return j.done(note="the funding did not land")
    tag = run.mint("", 8)[-6:]

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
        # settle_world drains the nominated-account outbox, which puts the new payee in clearing.
        for _ in range(2):
            time.sleep(SETTLE_PAUSE)
            run.settle_world()

    def withdraw_to(amount, payee, pair):
        started = clock.time() - clock.system_seconds(2)
        call = j.instruct(account, "WITHDRAW", amount)
        j.step("withdraw {}".format(amount), call.status)
        if not call.ok:
            return None
        paid = payouts_settled()
        dues = [d for d in _payouts_since(j.customer, started) if _money(d[1]) == amount]
        live = [d for d in dues if d[2] not in ("CANCELLED", "FAILED")]
        j.step("payout dues for {}".format(amount), dues)
        j.expect(bool(live) and all(pair[1] in d[3] for d in live),
                 "a payout goes to the nominated account active when it is raised",
                 "the {} withdrawal's payout dues name {}; '{}' ({}) was nominated and published "
                 "before the withdrawal".format(amount, sorted({d[3] for d in dues}), payee,
                                                 pair[1]),
                 pair[1], sorted({d[3] for d in dues}), body=dues)
        j.expect(len(live) <= 1, "a withdrawal pays out once",
                 "the {} withdrawal has {} live payout dues".format(amount, len(live)), 1,
                 len(live), body=dues)
        j.step("the {} withdrawal".format(amount), "completed" if paid else "still pending")
        return call

    payee_a = "Payee A {}".format(tag)
    _nominate(j, payee_a, PAYEES["A"])
    publish()
    if withdraw_to(Decimal("2.17"), payee_a, PAYEES["A"]) is None:
        return j.done(note="the first withdrawal was refused")
    if not settled():
        return j.done(note="the 2.17 withdrawal is still pending, so the payee is not changed")

    payee_b = "Payee B NOMATCH {}".format(tag)
    _nominate(j, payee_b, PAYEES["B"])
    publish()
    refused = j.instruct(account, "WITHDRAW", Decimal("0.50"))
    j.expect(not refused.ok, "a withdrawal is refused while the payee is unverified",
             "a 0.50 withdrawal placed with '{}' nominated answered {}".format(payee_b,
                                                                            refused.status),
             "a 4xx", refused.status, body=refused.body)
    j.step("withdraw 0.50 with an unverified payee", refused.status)
    if refused.ok and not payouts_settled():
        return j.done(note="the 0.50 withdrawal was accepted and is still pending")

    payee_c = "Payee C {}".format(tag)
    _nominate(j, payee_c, PAYEES["C"])
    publish()
    withdraw_to(Decimal("1.23"), payee_c, PAYEES["C"])
    return j.done()


def frozen_lifecycle(run):
    """Fund, freeze, try everything CustomerActionPolicy refuses or holds for a FROZEN customer,
    let a day accrue, unfreeze, and check the held deposit lands exactly once."""
    j = Journey(run, "JourneyFrozenLifecycle")
    if not j.statements_reachable():
        return j.refuse(POLL_WINDOW_NOTE)
    instant, notice = j.product("INSTANT"), j.product("NOTICE")
    if not instant or not run.compliance or not j.new_customer():
        return j.refuse("no INSTANT product or compliance client, or no ACTIVATED customer")
    account = j.open(instant, "INSTANT")
    if not account:
        return j.refuse("the INSTANT account did not open")
    j.fund([(account, Decimal("10.00"))])
    if not j.settle_until(lambda: _money(j.account(account["accountId"]).get("balance")) >= 10
                          and not j.pending()):
        return j.done(note="the funding did not land")
    frozen = world.override_customer_status(run.compliance, j.customer, "FREEZE",
                                            "simulation journey freeze")
    status = (j.client.call("GET", "/direct/v1/customers/{}".format(j.customer)).body or {}).get(
        "customerStatus")
    j.step("freeze the customer", "{} -> {}".format(getattr(frozen, "status", None), status))
    if status != "FROZEN":
        return j.done(note="the freeze did not land")
    frozen_day = interest_oracle.business_date(run.bank_uid)
    # CustomerActionPolicy.directVerdict: withdrawal placement, opening and transfer are refused,
    # an amendment of the nominated account is refused, and a rail deposit is held.
    refused = [
        ("withdraw 1.00", j.instruct(account, "WITHDRAW", Decimal("1.00"))),
        ("open a NOTICE account", j.client.call(
            "POST", "/direct/v1/customers/{}/accounts".format(j.customer),
            json_body={"productId": notice or instant, "accountReference": run.mint("acct", 18),
                       "termsAndConditionsAcceptedAt": _past_instant()})),
        ("transfer 1.00 to NOTICE", j.instruct(account, "TRANSFER", Decimal("1.00"),
                                               destination=notice or instant)),
        ("change the nominated account", _nominate(j, "Frozen payee {}".format(
            run.mint("", 6)[-4:]))),
    ]
    for what, call in refused:
        j.step(what + " while FROZEN", call.status)
        j.expect(not call.ok, "a FROZEN customer cannot {}".format(what.split(" ")[0]),
                 "{} while FROZEN answered {}".format(what, call.status), "a 4xx", call.status,
                 body=call.body)
    # A batch allocation is an instruction placed by the platform, so it may be refused at
    # placement; a deposit that does get through is held (CustomerActionPolicy.railDeposit).
    placed = j.fund([(account, Decimal("4.00"))])
    for _ in range(2):
        time.sleep(SETTLE_PAUSE)
        run.settle_world()
    held = [r for r in j.pending(account["accountId"]) if r.get("type") == "DEPOSIT"]
    deposits = j.booked(account["accountId"])
    accepted = getattr(j, "batch_accepted", False)
    answer = getattr(j, "batch_answer", (None, None))
    j.step("deposit 4.00 while FROZEN", "batch answered {}, {} pending deposit, deposits booked "
           "{}".format(answer[0], len(held), deposits))
    j.expect(accepted or answer[0] is None or answer[0] < 500,
             "a batch for a FROZEN customer is refused, not answered with a server error",
             "a 4.00 deposit batch for a FROZEN customer answered {}".format(answer[0]),
             "2xx or 4xx", answer[0], body=answer[1])
    j.expect(deposits is None or deposits == Decimal("10.00"),
             "a deposit for a FROZEN customer is not booked while frozen",
             "the account's deposits sum to {} while the customer is FROZEN".format(deposits),
             "10.00", deposits)
    # A batch answers 200 with each line's own verdict, so the line is read back: a line the
    # service REJECTED for the frozen customer never becomes a deposit to hold or release.
    line_status = None
    if accepted and isinstance(answer[1], dict) and answer[1].get("batchId"):
        read = j.client.call("GET", "/direct/v1/batches/{}".format(answer[1]["batchId"]))
        line_status = next((r.get("status") for r in _rows(read.body)), None) if read.ok else None
    j.step("the frozen deposit's batch line", line_status)
    expected_deposits = Decimal("14.00") if accepted and line_status not in (
        "REJECTED", "CANCELLED") else Decimal("10.00")

    moved = j.next_day()
    last = j.account(account["accountId"]).get("lastAccrualDate")
    j.expect(not moved or frozen_day is None or (last and last >= frozen_day.isoformat()),
             "a FROZEN customer's account keeps accruing",
             "frozen on business date {}, the account's last accrual is {}".format(
                 frozen_day, last), ">= {}".format(frozen_day), last)
    j.oracle([account["accountId"]], "a frozen day")

    released = world.override_customer_status(run.compliance, j.customer, "APPROVE",
                                              "simulation journey unfreeze")
    j.step("unfreeze the customer", getattr(released, "status", None))

    j.settle_until(lambda: (j.booked(account["accountId"]) or ZERO) >= expected_deposits)
    for _ in range(2):
        time.sleep(SETTLE_PAUSE)
        run.settle_world()
    deposits = j.booked(account["accountId"])
    j.expect(deposits == expected_deposits, "a held deposit is released exactly once",
             "after the unfreeze the account's deposits sum to {} (the frozen batch answered "
             "{})".format(deposits, getattr(placed, "status", None)), expected_deposits, deposits)
    j.conserved(expected_deposits, ZERO, "the unfreeze")
    call = j.instruct(account, "WITHDRAW", Decimal("1.00"))
    j.expect(call.ok or not j.payee_usable(seconds=0), "an unfrozen customer can withdraw",
             "a 1.00 withdrawal after the unfreeze answered {}".format(call.status), "2xx",
             call.status, body=call.body)
    j.step("withdraw 1.00 after the unfreeze", call.status)
    return j.done()


def fee_mid_period(run):
    """An INSTANT account earning at one platform fee, the fee changed, and each day's customer
    interest and platform fee held against the interest oracle on both sides of the change."""
    j = Journey(run, "JourneyFeeMidPeriod")
    if not j.statements_reachable():
        return j.refuse(POLL_WINDOW_NOTE)
    instant = j.product("INSTANT")
    if not instant or not j.new_customer():
        return j.refuse("no INSTANT product, or the customer did not become ACTIVATED")
    account = j.open(instant, "INSTANT")
    if not account:
        return j.refuse("the INSTANT account did not open")
    j.fund([(account, Decimal("250.00"))])
    if not j.settle_until(lambda: _money(j.account(account["accountId"]).get("balance")) >= 250):
        return j.done(note="the funding did not land")
    j.next_day()
    def newest_fee():
        rows, _ = interest_oracle._psql(
            "SELECT r.rate FROM platform_rate_detail r JOIN platform_product plp "
            "ON plp.sid = r.platform_product_sid WHERE plp.uid = '{}' AND r.live "
            "ORDER BY r.created_at DESC LIMIT 1".format(instant))
        return rows[0][0] if rows else None
    before = newest_fee()
    # ChangePlatformFee alternates a rise and a fall; a rise to the rate already set moves
    # nothing, so a second call makes the change real.
    for _ in range(2):
        with j.standing_on({"customerId": j.customer, "accountId": account["accountId"],
                            "productId": instant, "productType": "INSTANT"}):
            change = run.change_platform_fee()
        j.step("change the platform fee", "{} {}".format(change.status, (change.body or {}).get(
            "message") if isinstance(change.body, dict) else ""))
        if newest_fee() != before:
            break
    j.next_day()
    j.next_day()
    stats = j.oracle([account["accountId"]], "two days after the fee change")
    # Core is read on both sides of the API, because the clock keeper realises a day every few
    # seconds: fleet 206 read 0.12 in core and then 0.15 from the API, and both held 0.57 later.
    def core_realised():
        totals = interest_oracle.realised_totals(run.platform_uid, limit=500) or {}
        return totals.get(account["accountId"], (None, ZERO))[1]
    before = core_realised()
    api = sum((_money(r.get("amount")) for r in j.transactions(account["accountId"]) or []
               if r.get("type") == "INTEREST"), ZERO)
    realised = core_realised()
    j.expect(before <= api <= realised,
             "the INTEREST rows the API lists are the interest core realised",
             "the Direct API lists {} of INTEREST and core realised {} before that read and {} "
             "after it".format(api, before, realised), realised, api)
    j.step("realised interest", "{} via the API, {} in core; {} accruals judged".format(
        api, realised, stats.get("accrualsJudged")))
    return j.done()


def date_flip_under_load(run):
    """A deposit and a withdrawal on one account racing a business date advance (made by the
    conductor, waited for on a member), then every transaction booked after the accrual's read is
    dated after that day, the balance matches its transactions, and the accrual used the balance
    its cutoff saw. Members of a fleet run it at the same time on their own platforms."""
    j = Journey(run, "JourneyDateFlipUnderLoad")
    if not j.statements_reachable():
        return j.refuse(POLL_WINDOW_NOTE)
    instant = j.product("INSTANT")
    if not instant or not j.new_customer():
        return j.refuse("no INSTANT product, or the customer did not become ACTIVATED")
    account = j.open(instant, "INSTANT")
    if not account:
        return j.refuse("the INSTANT account did not open")
    j.fund([(account, Decimal("30.00"))])
    if not j.settle_until(lambda: _money(j.account(account["accountId"]).get("balance")) >= 30
                          and not j.pending()):
        return j.done(note="the funding did not land")
    legs = [lambda: j.fund([(account, Decimal("2.00"))]),
            lambda: j.instruct(account, "WITHDRAW", Decimal("1.50"))]
    before = interest_oracle.business_date(run.bank_uid)
    if longlived.keeps_clock(run):
        legs.append(lambda: longlived.clock_for(run).tick(force=True))
    elif not fleet.is_member() and not clock.schedulers_run():
        legs.append(run.advance_business_day)
    results, elapsed = race.fire(legs)
    j.step("race a deposit, a withdrawal{}".format(
        " and a date advance" if len(legs) == 3 else ""),
        [getattr(r, "status", r) for r in results])
    if len(legs) == 2:
        j.next_day()
    after = interest_oracle.business_date(run.bank_uid)
    j.step("business date", "{} -> {}".format(before, after))
    j.settle_until(lambda: not j.pending(account["accountId"]))
    time.sleep(61)
    read = j.account(account["accountId"])
    rows = j.transactions(account["accountId"]) or []
    total = sum((_money(r.get("amount")) for r in rows), ZERO)
    j.expect(_money(read.get("balance")) == total, "account balance == sum of its transactions",
             "after the race the account holds {} and its transactions sum to {}".format(
                 read.get("balance"), total), total, read.get("balance"))
    j.oracle([account["accountId"]], "after the date flip")
    return j.done()


ALL = {
    "JourneyMultiProduct": multi_product,
    "JourneyTermBeforeMaturity": term_before_maturity,
    "JourneyPayeeChange": payee_change,
    "JourneyFrozenLifecycle": frozen_lifecycle,
    "JourneyFeeMidPeriod": fee_mid_period,
    "JourneyDateFlipUnderLoad": date_flip_under_load,
}

# Journeys over the fee withdrawal, the payee review and the read models live beside these.
from explorer import journeys_ops  # noqa: E402

ALL.update(journeys_ops.ALL)

from explorer import journeys_softclose  # noqa: E402

ALL.update(journeys_softclose.ALL)
