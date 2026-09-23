"""Checks the service's own reads against each other, per §8 of the design.

The driver records what an endpoint answered; it does not check that the answer was right. An
endpoint that returns 200 and moves no money reads exactly like one that worked, so every claim of
correctness so far has come from querying the database by hand.

Each check takes reads the run already has and returns a violation or None. A violation names the
two quantities that disagree, because "conservation failed" without the numbers is not actionable.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

# Terminal statuses, per entity. Leaving one is a violation whatever the route in.
TERMINAL = {
    "customer": {"CLOSED"},
    "account": {"CLOSED", "CANCELLED"},
    "batch": {"SETTLED", "REJECTED", "CANCELLED"},
}


class Violation:
    def __init__(self, rule, subject, detail, expected=None, actual=None):
        self.rule = rule
        self.subject = subject
        self.detail = detail
        self.expected = expected
        self.actual = actual

    def as_row(self):
        return {
            "rule": self.rule,
            "subject": self.subject,
            "detail": self.detail,
            "expected": None if self.expected is None else str(self.expected),
            "actual": None if self.actual is None else str(self.actual),
        }

    def __str__(self):
        if self.expected is None:
            return "{}: {}".format(self.rule, self.detail)
        return "{}: {} — expected {}, read {}".format(
            self.rule, self.detail, self.expected, self.actual)


def _amount(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def account_balance(account, transactions):
    """balance == Σ amount over the account's transactions.

    The amounts are SIGNED on the wire: DirectTransactionRepository derives DEBIT from
    `customer_amount < 0` and passes the value through unchanged, so summing CREDIT minus DEBIT
    double-negates every debit. `mark` labels the sign rather than instructing one.
    """
    balance = _amount(account.get("balance"))
    if balance is None:
        return None
    total = Decimal("0")
    for row in transactions:
        amount = _amount(row.get("amount"))
        if amount is None:
            return None
        total += amount
    if balance.compare(total) != 0:
        return Violation(
            "account balance == sum of its transactions",
            account.get("accountId") or account.get("accountReference"),
            "the balance and the transactions the service lists for it disagree",
            expected=total, actual=balance)
    return None


def batch_total(batch, lines):
    """totalPaymentRequired == Σ amount over lines that are neither CANCELLED nor REJECTED.

    Completed deposits stay in the total, because DirectBatchInstructionRepository computes it as
    `SUM(amount) FILTER (WHERE status <> 'CANCELLED' AND type = 'DEPOSIT')`, so the total does not
    fall to zero once the batch settles.
    """
    required = _amount(batch.get("totalPaymentRequired"))
    if required is None:
        return None
    total = Decimal("0")
    counted = 0
    for row in lines:
        if row.get("instructionType") != "DEPOSIT":
            continue
        if row.get("status") in ("CANCELLED", "REJECTED"):
            continue
        amount = _amount(row.get("amount"))
        if amount is None:
            continue
        total += amount
        counted += 1
    if counted == 0 and required == 0:
        return None
    if required.compare(total) != 0:
        return Violation(
            "batch total == sum of its live deposit lines",
            batch.get("batchId"),
            "the total the batch asks for and the lines it holds disagree",
            expected=total, actual=required)
    return None


def paid_batch_settles(batch_id, required, paid, status, sweeps, shared, any_rejected=False):
    """A batch paid what it asked for has to stop being outstanding once the sweep has run.

    This is the check that says whether money actually moved. Every other batch oracle compares a
    batch against itself, so a payment that the bank took and clearing never attributed leaves
    every one of them satisfied while the deposit never lands.

    `shared` suppresses it: the request record states that batches sharing a paymentReference only
    advance when one payment covers their combined total, so a batch of a shared reference sitting
    unsettled is the documented behaviour rather than a fault.
    """
    if shared or paid < required or required <= 0:
        return None
    # A batch with a rejected line is left alone. What the run has paid is measured against the
    # lines still live, while the batch measures itself against totalPaymentRequired, which still
    # counts the rejected ones: the two stop agreeing the moment a line is rejected, and "paid in
    # full" becomes arithmetic that does not mean what it says. One run reported twelve findings
    # that way, every one of them a batch holding three rejected lines and one still waiting.
    if any_rejected:
        return None
    if sweeps < SWEEPS_BEFORE_SETTLEMENT_IS_OWED:
        return None
    if status in ("SETTLED", "COMPLETED", "CANCELLED", "REJECTED"):
        return None
    return Violation(
        "a fully paid batch settles once the sweep has run",
        batch_id,
        "the batch was paid in full and {} settlement sweeps later it is still {}".format(
            sweeps, status),
        expected="a settled batch", actual=status)


# One sweep can be in flight when the payment lands, so the batch is given a second before the
# harness calls it stuck.
SWEEPS_BEFORE_SETTLEMENT_IS_OWED = 2


def terminal_not_left(entity, before_status, after_status, subject):
    """A status that is terminal for its entity must not change again.

    This is the shape of the defect on SAV-11534, where a closed customer came back as ACTIVATED,
    so the run would have found it without anybody reading the database.
    """
    if not before_status or not after_status or before_status == after_status:
        return None
    if before_status in TERMINAL.get(entity, set()):
        return Violation(
            "a terminal status is not left",
            subject,
            "{} moved from {} to {}".format(entity, before_status, after_status),
            expected=before_status, actual=after_status)
    return None


# The status the client puts on a call that never reached the service, set in _Client.call.
TRANSPORT_FAULT = 598


# A read answering more slowly than this is reported. Chosen from measurement rather than taste:
# on the local stack every Direct read answers in 20 to 200 milliseconds, so a second is well clear
# of normal and still catches a read that has become seconds long.
SLOW_READ_SECONDS = 1.0


def answers_promptly(action, call, subject):
    """A read that takes seconds is a finding, even when it answers correctly.

    Found by measurement: a TERM account read costs 1.7 to 2.7 seconds against 0.19 for an INSTANT
    one, for a response of the same size, because core computes a day-by-day interest projection to
    maturity on every read. Nothing else in the harness would have reported that, because the call
    succeeds.
    """
    elapsed = (getattr(call, "elapsed_ms", 0) or 0) / 1000.0
    if elapsed < SLOW_READ_SECONDS:
        return None
    return Violation(
        "a read answers promptly",
        subject,
        "{} took {:.2f}s".format(action, elapsed),
        expected="under {:.0f}s".format(SLOW_READ_SECONDS),
        actual="{:.2f}s".format(elapsed))


def no_server_error(action, call, subject, under_fault=None):
    """A 5xx is the service failing rather than refusing, so it is never an expected answer.

    `under_fault` names the fault the run had injected when the call was made. A 5xx while the
    wire is cut or a service is restarting is the fault working, not a defect, so it is reported
    under its own rule and kept apart from the ones found on a healthy stack. Without that split
    one injected fault turned every action in the run into a finding: 24 rules fired in a single
    run and the two real defects were lost among them.

    What stays a finding under a fault is conservation — money that went missing or was counted
    twice — which the balance and batch oracles answer whatever the wire is doing.
    """
    status = getattr(call, "status", 0) or 0
    if status >= 500 and under_fault:
        return Violation(
            "a fault is survived without a server error",
            subject,
            "{} answered {} while {} was injected".format(action, status, under_fault),
            expected="a 2xx or a 4xx", actual=status)
    if status == TRANSPORT_FAULT:
        message = call.body.get("message") if isinstance(call.body, dict) else None
        return Violation(
            "the service answers every call",
            subject,
            "{} never got an answer: {}".format(action, message or "the connection failed"),
            expected="an answer", actual="no answer")
    if status >= 500:
        return Violation(
            "no unexplained 5xx",
            subject,
            "{} answered {}".format(action, status),
            expected="a 2xx or a 4xx", actual=status)
    return None


def closure_finishes(account_id, status, sweeps, case):
    """An account whose closure payment failed still has to finish closing.

    `check_non_term_cpa_uniqueness` counts an account at CLOSING, and the application refuses a
    new account on the same product while the trigger would raise. Nothing but the closure sweep
    moves that status, so an account that never leaves CLOSING turns the refusal on SAV-11580
    from a wait into a permanent block.
    """
    if status != "CLOSING":
        return None
    return Violation(
        "a closure the bank refused still finishes",
        account_id,
        "the bank {} the closure payment, and {} closure sweeps later the account is still "
        "CLOSING".format(case, sweeps),
        expected="CLOSED", actual=status)


def closed_account_is_empty(account_id, status, balance, case):
    """An account that reached CLOSED must not still hold the customer's money.

    Observed with a rejected closure payment: the account read CLOSED and the balance stayed at
    6.01. `InstructionFailureService.handleDirectPaymentFailure` cancels the instruction group and
    moves nothing back, because `isAcceptingInstructions()` is false for a closed account, so the
    money has neither left the bank nor returned to a place the customer can reach.
    """
    if status not in ("CLOSED", "CANCELLED"):
        return None
    amount = _amount(balance)
    if amount is None or amount.compare(Decimal("0")) == 0:
        return None
    return Violation(
        "a closed account holds no money",
        account_id,
        "the bank {} the closure payment, and the account is {} holding {}".format(
            case, status, amount),
        expected="0", actual=amount)


def operator_decision_takes_effect(group_uid, decision, status_before, status_after):
    """An operator's decision on a waiting payment group must change the group.

    `approveOrRejectPaymentGroup` takes the debtor account lock with `acquireNonBlockingLock` and
    acts only `ifPresent`, so a contended lock makes the decision do nothing and the call succeed.
    """
    if status_after != status_before:
        return None
    return Violation(
        "an operator decision takes effect",
        group_uid,
        "{} on a group at {} left it at {}".format(decision, status_before, status_after),
        expected="a status other than {}".format(status_before), actual=status_after)


def decided_closure_is_empty(account_id, status, balance, decision):
    """After an operator decides a refused closure payment, the closed account holds nothing.

    APPROVE sends the payment again, so the balance leaves once the bank accepts it. REJECT_FAIL
    and CANCEL have no path that pays a CLOSED account out, so a balance left here is the finding.
    """
    if status not in ("CLOSED", "CANCELLED"):
        return None
    amount = _amount(balance)
    if amount is None or amount.compare(Decimal("0")) == 0:
        return None
    return Violation(
        "a decided closure leaves the account empty",
        account_id,
        "the operator sent {} for the refused closure payment, and the account is {} holding {}"
        .format(decision, status, amount),
        expected="0", actual=amount)
