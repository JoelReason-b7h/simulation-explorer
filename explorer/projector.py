"""Turns reads into the per-entity state keys the driver counts visits against.

The key is per entity rather than per journey, because a journey-level key puts "two customers with
one shared payment reference" in the same bucket as "one customer", and that difference is the one
worth exploring.

Field names differ per entity: the customer read returns `customerStatus` and the account read
returns `status`, so each entity declares its own.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def _amount(value):
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _text(value):
    """Any field taken straight from a read, made safe to sort.

    A key holding None beside a key holding a string cannot be sorted, and the frontier sorts
    every key on every step, so one absent field stops the whole run.
    """
    return value if value else "unknown"


def _bucket_zero_positive(value):
    amount = _amount(value)
    if amount is None:
        return "unknown"
    return "positive" if amount > 0 else "zero"


def customer_key(customer):
    """(status, nominated account present, unallocated cash) for one customer."""
    nominated = customer.get("nominatedAccounts") or []
    return (
        "customer",
        _text(customer.get("customerStatus")),
        "present" if nominated else "none",
    )


def _bucket_count(n):
    if not n:
        return "none"
    return "one" if n == 1 else "many"


def account_key(account, customer_status=None, pending=None):
    """The account tuple, with its customer's status and its instructions still in flight.

    Without the in-flight count a withdrawal moved nothing: the balance is bucketed to zero or
    positive, so 9.00 falling to 6.00 read as the same state and PlaceWithdrawal recorded no edge
    however often it was accepted.
    """
    product = account.get("product") or {}
    return (
        "account",
        _text(account.get("status")),
        _text(product.get("productType") or account.get("productType")),
        _bucket_zero_positive(account.get("balance")),
        "present" if account.get("depositInfo") else "none",
        _text(customer_status),
        _bucket_count(pending),
    )


def batch_key(batch):
    """Batch status, how much is outstanding, how many customers it spans, how many lines went.

    A batch that touches one customer and a batch that touches three behave differently, and the
    run only finds that out if the two are separate states.
    """
    total = _amount(batch.get("totalPaymentRequired"))
    # The service returns the batch lines under `content`; the published spec calls the field
    # `allocations`, so read both and prefer what the service sends.
    allocations = batch.get("content") or batch.get("allocations") or []
    live = [a for a in allocations if a.get("status") not in ("CANCELLED", "REJECTED")]
    if total is None:
        outstanding = "unknown"
    elif total == 0:
        outstanding = "zero"
    elif len(live) == len(allocations):
        outstanding = "full"
    else:
        outstanding = "partial"
    customers = {a.get("customerId") for a in allocations if a.get("customerId")}
    span = "one" if len(customers) <= 1 else ("two" if len(customers) == 2 else "many")
    cancelled = len([a for a in allocations if a.get("status") == "CANCELLED"])
    if not allocations:
        withdrawn = "none"
    elif cancelled == 0:
        withdrawn = "none"
    elif cancelled == len(allocations):
        withdrawn = "all"
    else:
        withdrawn = "some"
    return ("batch", _text(batch.get("status")), outstanding, span, withdrawn,
            batch.get("paidState") or "unpaid")


def diff(before, after):
    """Every field that changed, including fields the published spec does not declare.

    The spec lags the service, so a field it omits is exactly where a regression hides. Comparing
    only declared fields would step over the drift rather than report it.
    """
    changes = {}
    for field in set(before or {}) | set(after or {}):
        old = (before or {}).get(field)
        new = (after or {}).get(field)
        if old != new:
            changes[field] = (old, new)
    return changes
