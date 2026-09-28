"""An ops soft closure of a platform product that has holders, and what they can do afterwards.

A soft closure cannot be undone, so the journey gives each run an INSTANT product of its own: a
new bank product on the cohort bank, granted to this platform under an alias that the run's own
product list leaves out. The platform's main products stay open for the rest of the run.

The product lives across several calls of the journey:

- While ACTIVE, each call opens and funds more customers on it, and one holder withdraws, so the
  soft closure lands on a product with a population and a withdrawal history.
- The journey soft-closes it only once it is old enough, holds enough accounts and holds enough
  money (SIM_SOFTCLOSE_MIN_AGE_MINUTES, _MIN_ACCOUNTS, _MIN_BALANCE; the age is fake time).
- Once SOFT_CLOSED, each call puts more holders through a withdrawal: part of the balance, all of
  it, or a top-up and then a withdrawal. Each must be accepted, pay out once, and leave the
  holder's money conserved.
- ClosedProductValidator refuses a new customer's first deposit only once the grace period ends:
  at the earlier of the platform's and the bank's end of day, on the working day on or after the
  closure date plus the bank's grace days. A first deposit is judged accepted before that and
  refused after it, each only when the whole funding sequence falls on one side of the end.

When every holder has withdrawn and the first deposit after the grace period is judged, the run
makes a new product on its next call.
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from decimal import Decimal

from explorer import clock, interest_oracle
from explorer.journeys import (SETTLE_PAUSE, ZERO, Journey, _money, _payouts_since, _rows,
                               settle_rounds, working_day_on_or_after)

# The run's product list leaves out every product whose Direct `name` (the fee alias) starts
# with this, so no other action opens an account on a product this journey closes.
ALIAS_PREFIX = "Harness soft close"
MIN_AGE = timedelta(minutes=float(os.environ.get("SIM_SOFTCLOSE_MIN_AGE_MINUTES", "20")))
MIN_ACCOUNTS = int(os.environ.get("SIM_SOFTCLOSE_MIN_ACCOUNTS", "6"))
MIN_BALANCE = Decimal(os.environ.get("SIM_SOFTCLOSE_MIN_BALANCE", "1000"))
# Customers a call opens on an ACTIVE product, and holders a call withdraws for once it is closed.
# Each new customer waits up to 30 s for Confirmation of Payee, so a call keeps to a few of each.
OPEN_PER_CALL = 3
WITHDRAW_PER_CALL = 3
AMOUNTS = [Decimal(a) for a in ("150.00", "275.00", "400.00", "90.00", "520.00", "60.00")]
PARTIAL = Decimal("25.00")
TOP_UP = Decimal("25.00")
FIRST_DEPOSIT = Decimal("50.00")
# The batch is placed before the grace end but the order is confirmed when the credit arrives,
# one to two real minutes later, so a first deposit this close to the end is not judged.
GRACE_MARGIN = timedelta(hours=1)
# A product whose first deposit after the grace period cannot come within this many calls
# after the closure is left behind, so one late grace end does not stop every later closure.
CALLS_AFTER_CLOSE = 4


def is_soft_close_product(row):
    return str(row.get("name") or "").startswith(ALIAS_PREFIX)


def soft_close(run):
    j = Journey(run, "JourneySoftClose")
    if not run.platform_uid or not run.ops or not run.bank_uid:
        return j.refuse("no platform, ops client or bank")
    if not j.statements_reachable():
        return j.refuse("the statement poll window is shut, so no deposit or payout can settle")
    product = getattr(run, "soft_close", None)
    if product is None:
        product = _new_product(j)
        if product is None:
            return j.refuse("could not make a product to close")
        run.soft_close = product
    if product["closedOn"] is None:
        return _while_active(j, product)
    return _while_closed(j, product)


# -- the product while it is ACTIVE ----------------------------------------------------------


def _while_active(j, product):
    _open_holders(j, product)
    if not product["withdrewWhileActive"] and product["holders"]:
        holder = product["holders"][0]
        product["withdrewWhileActive"] = _withdraw(j, holder, PARTIAL, "part of the balance",
                                                   "an ACTIVE product")
    age = clock.now() - product["madeAt"]
    held = _held(j, product)
    ready = (age >= MIN_AGE and len(product["holders"]) >= MIN_ACCOUNTS
             and held >= MIN_BALANCE)
    j.step("ready to soft-close", "{}: {:.0f} of {:.0f} minutes, {} of {} accounts, {} of {} held"
           .format("yes" if ready else "not yet", age.total_seconds() / 60,
                   MIN_AGE.total_seconds() / 60, len(product["holders"]), MIN_ACCOUNTS, held,
                   MIN_BALANCE))
    if not ready:
        return j.done(note="the product fills before its soft closure")
    if not _close(j, product):
        j.run.soft_close = None
        return j.done(note="the soft closure was not accepted, so the next call makes a new "
                           "product")
    _withdraw_round(j, product)
    _judge_first_deposit(j, product)
    _judge_interest(j, product, "after the soft closure")
    return j.done()


def _open_holders(j, product):
    wanted = min(OPEN_PER_CALL, MIN_ACCOUNTS - len(product["holders"]))
    opened = []
    for _ in range(max(wanted, 0)):
        if not j.new_customer():
            continue
        account = j.open(product["productId"], "INSTANT")
        if account:
            account["customerId"] = j.customer
            amount = AMOUNTS[(len(product["holders"]) + len(opened)) % len(AMOUNTS)]
            opened.append((account, amount))
    if not opened:
        return
    j.fund(opened)
    landed = j.settle_until(lambda: all(_balance(j, a) >= amount for a, amount in opened))
    for account, amount in opened:
        balance = _balance(j, account)
        if balance >= amount:
            product["holders"].append({"customer": account["customerId"], "account": account,
                                       "funded": amount, "paidOut": ZERO, "withdrawn": False})
    j.step("open and fund holders", "{} of {} landed{}".format(
        sum(1 for a, amount in opened if _balance(j, a) >= amount), len(opened),
        "" if landed else " after every settle round"))


def _close(j, product):
    run = j.run
    closed_on = clock.today()
    if not _propose_and_accept(j, product, closed_on):
        return False
    product["closedOn"] = closed_on
    state = _state(product["productId"])
    j.step("read the platform product state", state)
    j.expect(state == "SOFT_CLOSED",
             "a soft closure accepted for today soft-closes the platform product at once",
             "platform product {} is {} after its soft closure for {} was accepted".format(
                 product["productId"], state, closed_on), "SOFT_CLOSED", state)
    again = run.ops.call("POST", _close_path(run, product["productId"]),
                         json_body={"closureDate": closed_on.isoformat()})
    j.step("propose the soft closure a second time", again.status)
    j.expect(400 <= again.status < 500,
             "a second soft closure of a soft-closed product is refused",
             "the second proposal for platform product {} answered {}".format(
                 product["productId"], again.status), "4xx", again.status, body=again.body)
    product["graceEnd"] = _grace_end(product["productId"], closed_on)
    j.step("the grace period for new deposits ends", product["graceEnd"].isoformat()
           if product["graceEnd"] else "unreadable")
    return True


# -- the product once it is SOFT_CLOSED --------------------------------------------------------


def _while_closed(j, product):
    product["callsAfterClose"] += 1
    _withdraw_round(j, product)
    _judge_first_deposit(j, product)
    _judge_interest(j, product, "{} calls after the soft closure".format(
        product["callsAfterClose"]))
    finished = all(h["withdrawn"] for h in product["holders"]) and product["judgedAfterGrace"]
    if finished or product["callsAfterClose"] >= CALLS_AFTER_CLOSE:
        j.run.soft_close = None
        j.step("leave the product", "every holder withdrew and the grace end is judged"
               if finished else "{} calls after the closure".format(CALLS_AFTER_CLOSE))
    return j.done()


def _withdraw_round(j, product):
    """The next holders' withdrawals, each of one kind in turn: part of the balance, all of it,
    or a top-up and then part of the balance."""
    waiting = [h for h in product["holders"] if not h["withdrawn"]]
    for holder in waiting[:WITHDRAW_PER_CALL]:
        kind = product["holders"].index(holder) % 3
        if kind == 1:
            amount = _balance(j, holder["account"])
            label = "the whole balance"
        else:
            if kind == 2:
                _top_up(j, holder)
            amount = PARTIAL
            label = "part of the balance"
        holder["withdrawn"] = True
        _withdraw(j, holder, amount, label, "a SOFT_CLOSED product")


def _withdraw(j, holder, amount, label, product_state):
    """One withdrawal: accepted, paid out once, and the holder's money conserved after it."""
    j.customer = holder["customer"]
    started = clock.time() - clock.system_seconds(2)
    call = j.instruct(holder["account"], "WITHDRAW", amount)
    when = "of {} from {}".format(label, product_state)
    j.step("withdraw {} {}".format(amount, when), call.status)
    j.expect(call.ok, "a holder can withdraw from {}".format(product_state),
             "a {} withdrawal {} answered {}".format(amount, when, call.status), "2xx",
             call.status, body=call.body)
    if not call.ok:
        return False
    paid = _payouts_settled(j, holder["account"]["accountId"])
    dues = [d for d in _payouts_since(holder["customer"], started) if _money(d[1]) == amount]
    live = [d for d in dues if d[2] not in ("CANCELLED", "FAILED")]
    j.step("the {} withdrawal".format(amount), "{}, {} live payout dues".format(
        "completed" if paid else "still pending", len(live)))
    j.expect(len(live) == 1, "a withdrawal from {} pays out once".format(product_state),
             "the {} withdrawal {} has {} live payout dues".format(amount, when, len(live)), 1,
             len(live), body=dues)
    if paid:
        holder["paidOut"] += amount
        j.conserved(holder["funded"], holder["paidOut"], "the withdrawal {}".format(when))
    return True


def _payouts_settled(j, account_id):
    def settled():
        return not [r for r in j.pending(account_id) if r.get("type") == "WITHDRAWAL"]
    for _ in range(settle_rounds(10)):
        if settled():
            return True
        time.sleep(SETTLE_PAUSE)
        j.settle_payouts()
    return settled()


def _top_up(j, holder):
    """An existing customer's deposit into a soft-closed product lands, within grace or after."""
    account = holder["account"]
    j.customer = holder["customer"]
    before = _balance(j, account)
    j.fund([(account, TOP_UP)])
    after = j.settle_until(lambda: _balance(j, account) >= before + TOP_UP and
                           _balance(j, account)) or _balance(j, account)
    j.step("top up a holder's account", "{} -> {}".format(before, after))
    if j.expect(after >= before + TOP_UP, "an existing customer can top up a soft-closed product",
                "account {} held {} and holds {} after a {} top-up".format(
                    account["accountId"], before, after, TOP_UP), before + TOP_UP, after):
        holder["funded"] += TOP_UP


def _judge_first_deposit(j, product):
    """A new customer's first deposit: accepted within the grace period, refused after it."""
    if product["judgedAfterGrace"] or not product["graceEnd"]:
        return
    now, end = clock.now(), product["graceEnd"]
    if now + GRACE_MARGIN < end and not product["judgedWithinGrace"]:
        _first_deposit(j, product["productId"], refused=False)
        product["judgedWithinGrace"] = True
    elif now >= end + GRACE_MARGIN:
        _first_deposit(j, product["productId"], refused=True)
        product["judgedAfterGrace"] = True
    else:
        j.step("a new customer's first deposit", "not judged: {} London, grace ends {}".format(
            clock.london_now().strftime("%Y-%m-%d %H:%M"), end.isoformat()))


def _first_deposit(j, product_id, refused):
    if not j.new_customer():
        j.step("a new customer's first deposit", "not judged: no customer")
        return
    when = "after" if refused else "within"
    account = j.open(product_id, "INSTANT")
    if not account:
        # An opening refused outright refuses the first deposit too, but never with a 5xx.
        opened = j.steps[-1]["outcome"]
        j.expect(refused and opened.startswith("4"),
                 "a new customer can open a soft-closed product only within its grace period",
                 "opening an account {} the grace period answered {}".format(when, opened),
                 "4xx" if refused else "2xx", opened)
        return
    j.fund([(account, FIRST_DEPOSIT)])
    batch_status = j.batch_answer[0]
    j.expect(batch_status < 500, "a first deposit into a soft-closed product never answers 5xx",
             "the batch {} the grace period answered {}".format(when, batch_status), "<500",
             batch_status, body=j.batch_answer[1])
    if refused:
        j.settle_until(lambda: False)
        held = _balance(j, account)
        j.step("the first deposit after the grace period", "batch {}, balance {}".format(
            batch_status, held))
        j.expect(held == 0, "a soft-closed product takes no first deposit after its grace period",
                 "account {} holds {} after a first deposit placed after the grace period"
                 .format(account["accountId"], held), 0, held)
    else:
        held = j.settle_until(lambda: _balance(j, account) >= FIRST_DEPOSIT and
                              _balance(j, account)) or _balance(j, account)
        j.step("the first deposit within the grace period", "batch {}, balance {}".format(
            batch_status, held))
        j.expect(held >= FIRST_DEPOSIT,
                 "a soft-closed product takes a first deposit within its grace period",
                 "account {} holds {} after a {} first deposit within the grace period".format(
                     account["accountId"], held, FIRST_DEPOSIT), FIRST_DEPOSIT, held)


def _judge_interest(j, product, label):
    j.oracle([h["account"]["accountId"] for h in product["holders"]], label)


# -- ops calls and reads -----------------------------------------------------------------------


def _new_product(j):
    """A new INSTANT bank product, granted to this platform under an alias of its own."""
    import standup_cohort
    run = j.run
    tag = run.mint("sc", 10)
    alias = "{} {}".format(ALIAS_PREFIX, tag)
    body = standup_cohort.product_body(run.bank_uid)
    body.update({"externalId": "harness-softclose-{}".format(tag),
                 "name": "Harness Soft Close {}".format(tag)})
    proposal = run.ops.call("POST", "/operations/proposals/banks/{}/products".format(
        run.bank_uid), json_body=body)
    approval = standup_cohort.uid_from(proposal.body) if proposal.ok else None
    if not approval:
        j.step("propose a product to close", proposal.status)
        return None
    accepted = run.ops.call("POST", "/operations/approvals/{}/accept".format(approval),
                            json_body={})
    bank_product = standup_cohort.uid_from(accepted.body) if accepted.ok else None
    j.step("make the bank product to close", "{} {}".format(accepted.status, bank_product))
    if not bank_product:
        return None
    access = standup_cohort.access_body(run.bank_uid, run.platform_uid, bank_product)
    access["feeDetailRequest"]["alias"] = alias
    granted = run.ops.call("POST", "/operations/own/access", json_body=access)
    j.step("grant this platform the product", granted.status)
    if not granted.ok:
        return None
    run.ops.call("POST", "/operations/processor/partners/refresh")
    listed = run.client.call("GET", "/direct/v1/products")
    found = next((row.get("productId") for row in (_rows(listed.body) if listed.ok else [])
                  if row.get("name") == alias), None)
    j.step("find the platform product", found)
    if not found:
        return None
    return {"productId": found, "alias": alias, "madeAt": clock.now(), "holders": [],
            "withdrewWhileActive": False, "closedOn": None, "graceEnd": None,
            "judgedWithinGrace": False, "judgedAfterGrace": False, "callsAfterClose": 0}


def _close_path(run, product_id):
    return "/operations/proposals/platforms/{}/platform-products/{}/close/soft".format(
        run.platform_uid, product_id)


def _propose_and_accept(j, product, closed_on):
    run = j.run
    proposal = run.ops.call("POST", _close_path(run, product["productId"]),
                            json_body={"closureDate": closed_on.isoformat()})
    j.step("propose the soft closure for {}".format(closed_on), proposal.status)
    j.expect(proposal.ok, "ops can propose a soft closure of an active platform product",
             "the proposal for platform product {} answered {}".format(product["productId"],
                                                                       proposal.status),
             "2xx", proposal.status, body=proposal.body)
    if not proposal.ok:
        return False
    # The proposal answers with no body. The approval carries the platform product's alias as
    # its entity name, and each alias is this journey's own.
    listed = run.ops.call("GET", "/operations/approvals?approvalType=PLATFORM_PRODUCT_SOFT_CLOSE"
                          "&paginatedProperty=CREATED_AT&orderAscDesc=DESC&take=50")
    rows = _rows(listed.body) if listed.ok else []
    approval = next((row.get("approvalUid") for row in rows
                     if row.get("entityName") == product["alias"]), None)
    if not approval:
        j.step("find the soft closure approval", "not found in {} rows".format(len(rows)))
        return False
    accepted = run.ops.call("POST", "/operations/approvals/{}/accept".format(approval),
                            json_body={})
    j.step("accept the soft closure", accepted.status)
    j.expect(accepted.ok, "ops can accept a soft closure",
             "accepting approval {} answered {}".format(approval, accepted.status), "2xx",
             accepted.status, body=accepted.body)
    if accepted.ok:
        run.note_in_flight("closure", "a soft closure of platform product {}".format(
            product["productId"]))
    return accepted.ok


def _balance(j, account):
    return _money(j.account(account["accountId"], customer=account.get("customerId")
                            or j.customer).get("balance"))


def _held(j, product):
    return sum((_balance(j, h["account"]) for h in product["holders"]), ZERO)


def _state(product_id):
    rows, _ = interest_oracle._psql(
        "SELECT current_state FROM platform_product WHERE uid = '{}'".format(product_id))
    return rows[0][0] if rows else None


def _grace_end(product_id, closed_on):
    """The instant ClosedProductValidator's grace period ends for this product, in London time.
    The grace buffers default to 0 and the stack sets neither."""
    rows, _ = interest_oracle._psql(
        "SELECT COALESCE(bp.grace_period_days, pb.grace_period, 0), plat.end_of_day, "
        "pb.end_of_day FROM platform_product pp "
        "JOIN bank_product bp ON bp.sid = pp.product_sid "
        "JOIN partner_bank pb ON pb.sid = bp.bank_sid "
        "JOIN partner_platform plat ON plat.sid = pp.platform_sid "
        "WHERE pp.uid = '{}'".format(product_id))
    if not rows:
        return None
    days, platform_eod, bank_eod = rows[0]
    end_day = working_day_on_or_after(closed_on + timedelta(days=int(days)))
    end_time = min(datetime.strptime(platform_eod, "%H:%M:%S").time(),
                   datetime.strptime(bank_eod, "%H:%M:%S").time())
    return datetime.combine(end_day, end_time, tzinfo=clock.LONDON)


ALL = {"JourneySoftClose": soft_close}
