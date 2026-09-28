"""An ops soft closure of a platform product, and what it lets a customer do afterwards.

A soft closure cannot be undone, so each closure gets an INSTANT product of its own: a new bank
product on the cohort bank, granted to this platform under an alias that the run's own product
list leaves out. The platform's main products stay open for the rest of the run.

ClosedProductValidator lets an existing customer top up a SOFT_CLOSED product at any time, and
refuses a new customer's first deposit only once the grace period has ended: at the platform's
end of day on the working day on or after the closure date plus the bank's grace days. So the
journey judges a first deposit in two places. The call that closes the product judges it as
accepted when the grace period has time left. A later call judges it as refused once the grace
period has ended.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from explorer import clock, interest_oracle, world
from explorer.journeys import Journey, _money, _rows, working_day_on_or_after

# The run's product list leaves out every product whose Direct `name` (the fee alias) starts
# with this, so no other action opens an account on a product this journey closes.
ALIAS_PREFIX = "Harness soft close"
# A deposit is judged against the grace period only when the whole funding sequence falls on one
# side of the grace end: the batch is placed before it, but the order is confirmed when the credit
# arrives, which is one to two real minutes later.
GRACE_MARGIN = timedelta(hours=1)
# Products closed and waiting for their grace period to end. Each one costs a bank product, so
# the journey closes no more until one of these is judged.
MAX_WAITING = 2
FUNDED = Decimal("100.00")
TOP_UP = Decimal("25.00")
FIRST_DEPOSIT = Decimal("50.00")


def is_soft_close_product(row):
    return str(row.get("name") or "").startswith(ALIAS_PREFIX)


def soft_close(run):
    j = Journey(run, "JourneySoftClose")
    if not run.platform_uid or not run.ops or not run.bank_uid:
        return j.refuse("no platform, ops client or bank")
    if not j.statements_reachable():
        return j.refuse("the statement poll window is shut, so no deposit can settle")
    waiting = run.__dict__.setdefault("soft_closed", [])
    due = [entry for entry in waiting if clock.now() >= entry["grace_end"] + GRACE_MARGIN]
    if due:
        return _after_grace(j, due[0])
    if len(waiting) >= MAX_WAITING:
        return j.refuse("{} closed products wait for their grace period to end, the first at {}"
                        .format(len(waiting), waiting[0]["grace_end"].isoformat()))
    return _close(j)


def _close(j):
    run = j.run
    made = _new_product(j)
    if not made:
        return j.refuse("could not make a product to close")
    product_id, alias = made
    if not j.new_customer():
        return j.refuse("no customer to hold an account before the closure")
    holder = j.customer
    account = j.open(product_id, "INSTANT")
    if not account:
        return j.refuse("the INSTANT account on the new product did not open")
    j.fund([(account, FUNDED)])
    balance = j.settle_until(lambda: _balance(j, account, holder) >= FUNDED and
                             _balance(j, account, holder))
    if not balance:
        return j.refuse("the {} deposit before the closure did not land".format(FUNDED))

    closed_on = clock.today()
    accepted = _propose_and_accept(j, product_id, alias, closed_on)
    if not accepted:
        return j.done(note="the soft closure was not accepted")
    state = _state(product_id)
    j.step("read the platform product state", state)
    j.expect(state == "SOFT_CLOSED",
             "a soft closure accepted for today soft-closes the platform product at once",
             "platform product {} is {} after its soft closure for {} was accepted".format(
                 product_id, state, closed_on), "SOFT_CLOSED", state)

    again = run.ops.call("POST", _close_path(run, product_id),
                         json_body={"closureDate": closed_on.isoformat()})
    j.step("propose the soft closure a second time", again.status)
    j.expect(400 <= again.status < 500,
             "a second soft closure of a soft-closed product is refused",
             "the second proposal for platform product {} answered {}".format(
                 product_id, again.status), "4xx", again.status, body=again.body)

    entry = {"productId": product_id, "holder": holder, "account": account,
             "held": FUNDED, "grace_end": _grace_end(product_id, closed_on)}
    j.step("the grace period for new deposits ends", entry["grace_end"].isoformat()
           if entry["grace_end"] else "unreadable")
    if not entry["grace_end"]:
        return j.done(note="could not read the grace period")
    _top_up(j, entry)
    if clock.now() + GRACE_MARGIN < entry["grace_end"]:
        _first_deposit(j, product_id, refused=False)
        run.soft_closed.append(entry)
    elif clock.now() >= entry["grace_end"] + GRACE_MARGIN:
        _first_deposit(j, product_id, refused=True)
    else:
        j.step("a new customer's first deposit", "not judged: within {} of the grace end"
               .format(GRACE_MARGIN))
        run.soft_closed.append(entry)
    j.customer = holder
    j.oracle([account["accountId"]], "after the soft closure")
    return j.done()


def _after_grace(j, entry):
    j.run.soft_closed.remove(entry)
    j.step("judge a product whose grace period ended", "{} ended {}".format(
        entry["productId"], entry["grace_end"].isoformat()))
    _top_up(j, entry)
    _first_deposit(j, entry["productId"], refused=True)
    j.customer = entry["holder"]
    j.oracle([entry["account"]["accountId"]], "after the grace period")
    return j.done()


def _top_up(j, entry):
    """An existing customer's deposit into a soft-closed product lands, before or after grace."""
    account, holder = entry["account"], entry["holder"]
    j.customer = holder
    before = _balance(j, account, holder)
    j.fund([(account, TOP_UP)], customer=holder)
    after = j.settle_until(lambda: _balance(j, account, holder) >= before + TOP_UP and
                           _balance(j, account, holder)) or _balance(j, account, holder)
    j.step("top up the existing account", "{} -> {}".format(before, after))
    j.expect(after >= before + TOP_UP,
             "an existing customer can top up a soft-closed product",
             "account {} held {} and holds {} after a {} top-up".format(
                 account["accountId"], before, after, TOP_UP), before + TOP_UP, after)


def _first_deposit(j, product_id, refused):
    """A new customer's first deposit: accepted within the grace period, refused after it."""
    if not j.new_customer():
        j.step("a new customer's first deposit", "not judged: no customer")
        return
    account = j.open(product_id, "INSTANT")
    when = "after" if refused else "within"
    if not account:
        # An opening refused outright is a refusal of the first deposit too, but never a 5xx.
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
        held = _balance(j, account, j.customer)
        j.step("the first deposit after the grace period", "batch {}, balance {}".format(
            batch_status, held))
        j.expect(held == 0, "a soft-closed product takes no first deposit after its grace period",
                 "account {} holds {} after a first deposit placed after the grace period"
                 .format(account["accountId"], held), 0, held)
    else:
        held = j.settle_until(lambda: _balance(j, account, j.customer) >= FIRST_DEPOSIT and
                              _balance(j, account, j.customer)) or _balance(j, account, j.customer)
        j.step("the first deposit within the grace period", "batch {}, balance {}".format(
            batch_status, held))
        j.expect(held >= FIRST_DEPOSIT,
                 "a soft-closed product takes a first deposit within its grace period",
                 "account {} holds {} after a {} first deposit within the grace period".format(
                     account["accountId"], held, FIRST_DEPOSIT), FIRST_DEPOSIT, held)


def _new_product(j):
    """A new INSTANT bank product, granted to this platform. Answers (platform product, alias)."""
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
    return (found, alias) if found else None


def _close_path(run, product_id):
    return "/operations/proposals/platforms/{}/platform-products/{}/close/soft".format(
        run.platform_uid, product_id)


def _propose_and_accept(j, product_id, alias, closed_on):
    run = j.run
    proposal = run.ops.call("POST", _close_path(run, product_id),
                            json_body={"closureDate": closed_on.isoformat()})
    j.step("propose the soft closure for {}".format(closed_on), proposal.status)
    j.expect(proposal.ok, "ops can propose a soft closure of an active platform product",
             "the proposal for platform product {} answered {}".format(product_id,
                                                                       proposal.status),
             "2xx", proposal.status, body=proposal.body)
    if not proposal.ok:
        return False
    # The proposal answers with no body. The approval carries the platform product's alias as
    # its entity name, and each alias is this journey's own.
    listed = run.ops.call("GET", "/operations/approvals?approvalType=PLATFORM_PRODUCT_SOFT_CLOSE"
                          "&paginatedProperty=CREATED_AT&orderAscDesc=DESC&take=50")
    rows = _rows(listed.body) if listed.ok else []
    approval = next((row.get("approvalUid") for row in rows if row.get("entityName") == alias),
                    None)
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
        run.note_in_flight("closure", "a soft closure of platform product {}".format(product_id))
    return accepted.ok


def _balance(j, account, customer):
    return _money(j.account(account["accountId"], customer=customer).get("balance"))


def _state(product_id):
    rows, _ = interest_oracle._psql(
        "SELECT current_state FROM platform_product WHERE uid = '{}'".format(product_id))
    return rows[0][0] if rows else None


def _grace_end(product_id, closed_on):
    """The instant ClosedProductValidator's grace period ends for this product, in London time:
    the earlier of the platform's and the bank's end of day, on the working day on or after the
    closure date plus the grace days. The grace buffers default to 0 and the stack sets neither."""
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
