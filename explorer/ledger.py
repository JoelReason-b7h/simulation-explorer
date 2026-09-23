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

PAYMENT_COLUMNS = ("sid", "end_to_end_id", "creditor_reference", "amount", "status",
                   "payment_type", "status_desc", "from_account_identifier")


def _psql(sql, timeout=60):
    try:
        done = subprocess.run(
            ["psql", CLEARING_DSN, "-tA", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
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
