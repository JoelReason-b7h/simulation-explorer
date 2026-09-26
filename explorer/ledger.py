"""Facts about a payment that only clearing's own tables carry.

The Direct API answers about accounts and batches. It says nothing about the payment clearing sent
to the bank, so a run that wants to send that payment back has no reference to send it on, and a
run that wants to know whether the bank refused it has nothing to read.

Everything here is a read. The harness changes clearing through its ops endpoints, never by writing
to this database.
"""

from __future__ import annotations

import os
import subprocess

CLEARING_DSN = os.environ.get(
    "SIM_CLEARING_DSN", "postgresql://clearing:password@localhost:5440/clearing")
CORE_DSN = os.environ.get("SIM_CORE_DSN", "postgresql://core:password@localhost:5432/core")

PAYMENT_COLUMNS = ("sid", "end_to_end_id", "creditor_reference", "amount", "status",
                   "payment_type", "status_desc", "from_account_identifier")


def _psql(sql, timeout=60, dsn=CLEARING_DSN):
    try:
        done = subprocess.run(
            ["psql", dsn, "-tA", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return []
    if done.returncode != 0:
        return []
    return [line.split("|") for line in done.stdout.splitlines() if line.strip()]


def newest_payment_sid():
    """The highest payment initiation row clearing holds, or 0 when it holds none."""
    rows = _psql("SELECT COALESCE(MAX(sid), 0) FROM payment_initiation")
    try:
        return int(rows[0][0])
    except (IndexError, ValueError):
        return 0


def payment_after(sid):
    """The first payment clearing raised after the given row, as a dict, or None.

    The run closes an account and settles in one action, so the payment that follows that row is
    the closure payment. Nothing else in the harness sends money out in that window.
    """
    rows = _psql(
        "SELECT {} FROM payment_initiation WHERE sid > {} AND is_return IS NOT TRUE "
        "ORDER BY sid LIMIT 1".format(", ".join(PAYMENT_COLUMNS), int(sid)))
    if not rows:
        return None
    return dict(zip(PAYMENT_COLUMNS, rows[0]))


def payment_by_sid(sid):
    """Read one payment again, which is how the run learns what the bank answered."""
    rows = _psql("SELECT {} FROM payment_initiation WHERE sid = {}".format(
        ", ".join(PAYMENT_COLUMNS), int(sid)))
    if not rows:
        return None
    return dict(zip(PAYMENT_COLUMNS, rows[0]))


def clearing_account_of(product_account_uid):
    """The clearing internal account uid behind a core product account, or None."""
    rows = _psql(
        "SELECT eia.account_uid FROM direct_customer_account dca "
        "JOIN customer_product_account cpa ON cpa.sid = dca.customer_product_account_sid "
        "JOIN entity_internal_account eia ON eia.sid = dca.entity_internal_account_sid "
        "WHERE cpa.uid = '{}'".format(str(product_account_uid).replace("'", "")), dsn=CORE_DSN)
    return rows[0][0] if rows and rows[0] else None


def core_balance(product_account_uid):
    """The product account balance core holds now, as text, or None."""
    rows = _psql(
        "SELECT product_account_balance FROM customer_product_account WHERE uid = '{}'".format(
            str(product_account_uid).replace("'", "")), dsn=CORE_DSN)
    return rows[0][0] if rows and rows[0] else None


def payment_for_account_after(sid, clearing_account_uid):
    """The first payment after the given row that pays out this account's dues, or None.

    One closure sweep closes every account waiting for it, so the first payment after the injection
    can belong to another account. In cycle 29 the run decided another account's group that way and
    read the injected account, which it had never touched.
    """
    rows = _psql(
        "SELECT {} FROM payment_initiation pi "
        "JOIN payment_group pg ON pg.sid = pi.payment_group_sid "
        "WHERE pi.sid > {} AND pi.is_return IS NOT TRUE AND EXISTS ("
        "  SELECT 1 FROM partner_payment_due ppd "
        "  JOIN internal_account ia ON ia.sid = ppd.account_sid "
        "  WHERE ppd.aggregate_uid = pg.uid AND ia.account_uid = '{}') "
        "ORDER BY pi.sid LIMIT 1".format(
            ", ".join("pi." + c for c in PAYMENT_COLUMNS), int(sid),
            str(clearing_account_uid).replace("'", "")))
    if not rows:
        return None
    return dict(zip(PAYMENT_COLUMNS, rows[0]))


def group_of_payment(sid):
    """The payment group a payment belongs to, as {"uid", "status"}, or None."""
    rows = _psql(
        "SELECT pg.uid, pg.status FROM payment_initiation pi "
        "JOIN payment_group pg ON pg.sid = pi.payment_group_sid WHERE pi.sid = {}".format(int(sid)))
    if not rows:
        return None
    return {"uid": rows[0][0], "status": rows[0][1]}


def retry_of_payment(sid):
    """The payment an APPROVE made in place of this one, or None.

    `replacePaymentInitiation` writes the new payment's uid into the old payment's `retry_uid`.
    """
    rows = _psql(
        "SELECT {} FROM payment_initiation WHERE uid = "
        "(SELECT retry_uid FROM payment_initiation WHERE sid = {})".format(
            ", ".join(PAYMENT_COLUMNS), int(sid)))
    if not rows:
        return None
    return dict(zip(PAYMENT_COLUMNS, rows[0]))


HSB_DSN = os.environ.get("SIM_HSB_DSN", "postgresql://hsb:password@localhost:5444/hsb")


def bank_paid(end_to_end_id):
    """Whether the bank simulator recorded this payment as settled (ACSC)."""
    if not end_to_end_id:
        return False
    import subprocess
    done = subprocess.run(
        ["psql", HSB_DSN, "-tA", "-c",
         "SELECT 1 FROM payment_transaction_status WHERE end_to_end_id = '{}' AND status = 'ACSC' "
         "LIMIT 1".format(str(end_to_end_id).replace("'", ""))],
        capture_output=True, text=True, timeout=30)
    return done.returncode == 0 and done.stdout.strip() == "1"
