"""Checks the transition chains that core records, which no API read can answer.

Three tables record a state change as a row naming the state before it and the state after it. A
write that goes through the transition rule always records what it moved from, so a row whose
"before" does not match the previous row's "after" is a write that went round the rule. The API
never shows that: it answers with the status the entity holds now, and a lawful move and an
unlawful one both leave a status behind.

Each table also carries a second question. The newest row in the chain must agree with the status
the entity itself holds, because a write that changed the status and recorded no row breaks that
comparison while leaving the chain whole.

These read whole tables, so the run calls them on a timer rather than after each trial.
"""

from __future__ import annotations

import os
import subprocess

CORE_DSN = os.environ.get("SIM_CORE_DSN", "postgresql://core:password@localhost:5432/core")

# How many broken rows one query reports. A chain that breaks across a whole cohort would put
# thousands of rows on the page, and the first few carry the same evidence as all of them.
LIMIT = 20

# `sid` orders the rows, not the timestamp. Two rows written in one transaction share a timestamp
# to the microsecond, and the identity column is the only ordering that cannot tie.
CHECKS = (
    (
        "the customer status chain has no break",
        "platform_customer_status_history",
        """
        SELECT c.uid, h.sid, h.from_state, h.prior, h.transitioned_at
        FROM (
          SELECT sid, platform_customer_sid, from_state, transitioned_at,
                 LAG(to_state) OVER (PARTITION BY platform_customer_sid ORDER BY sid) AS prior
          FROM platform_customer_status_history) h
        JOIN platform_customer c ON c.sid = h.platform_customer_sid
        WHERE h.prior IS NOT NULL AND h.from_state::text <> h.prior::text
        ORDER BY h.sid DESC LIMIT {limit}
        """,
        "customer {0} row {1} moves from {2}, and the row before it arrived at {3} ({4})",
    ),
    (
        "the account state chain has no break",
        "customer_product_account_state",
        """
        SELECT a.uid, s.sid, s.previous_state, s.prior, s.current_state
        FROM (
          SELECT sid, customer_product_account_sid, previous_state, current_state,
                 LAG(current_state) OVER (
                   PARTITION BY customer_product_account_sid ORDER BY sid) AS prior
          FROM customer_product_account_state) s
        JOIN customer_product_account a ON a.sid = s.customer_product_account_sid
        WHERE s.prior IS NOT NULL AND s.previous_state IS DISTINCT FROM s.prior
        ORDER BY s.sid DESC LIMIT {limit}
        """,
        "account {0} row {1} moves from {2}, and the row before it arrived at {3} (now {4})",
    ),
    (
        "the order state chain has no break",
        "customer_product_order_state",
        """
        SELECT o.uid, s.sid, s.previous_state, s.prior, s.current_state
        FROM (
          SELECT sid, customer_product_order_sid, previous_state, current_state,
                 LAG(current_state) OVER (
                   PARTITION BY customer_product_order_sid ORDER BY sid) AS prior
          FROM customer_product_order_state) s
        JOIN customer_product_order o ON o.sid = s.customer_product_order_sid
        WHERE s.prior IS NOT NULL AND s.previous_state IS DISTINCT FROM s.prior
        ORDER BY s.sid DESC LIMIT {limit}
        """,
        "order {0} row {1} moves from {2}, and the row before it arrived at {3} (now {4})",
    ),
    (
        "the customer status equals its newest history row",
        "platform_customer",
        """
        SELECT c.uid, c.verification_status, h.to_state, h.sid, h.transitioned_at
        FROM platform_customer c
        JOIN LATERAL (
          SELECT sid, to_state, transitioned_at FROM platform_customer_status_history
          WHERE platform_customer_sid = c.sid ORDER BY sid DESC LIMIT 1) h ON TRUE
        WHERE c.verification_status::text <> h.to_state::text
        ORDER BY h.sid DESC LIMIT {limit}
        """,
        "customer {0} reads {1}, and its newest history row arrived at {2} (row {3}, {4})",
    ),
    (
        "the live account state row is the newest one",
        "customer_product_account_state",
        """
        SELECT a.uid, live.sid, live.current_state, newest.sid, newest.current_state
        FROM customer_product_account a
        JOIN LATERAL (
          SELECT sid, current_state FROM customer_product_account_state
          WHERE customer_product_account_sid = a.sid AND live
          ORDER BY sid DESC LIMIT 1) live ON TRUE
        JOIN LATERAL (
          SELECT sid, current_state FROM customer_product_account_state
          WHERE customer_product_account_sid = a.sid
          ORDER BY sid DESC LIMIT 1) newest ON TRUE
        WHERE live.sid <> newest.sid
        ORDER BY newest.sid DESC LIMIT {limit}
        """,
        "account {0} carries row {1} as live ({2}), and its newest row is {3} ({4})",
    ),
    (
        "one account state row is live",
        "customer_product_account_state",
        """
        SELECT a.uid, count(*) FILTER (WHERE s.live), count(*), min(s.sid), max(s.sid)
        FROM customer_product_account a
        JOIN customer_product_account_state s ON s.customer_product_account_sid = a.sid
        GROUP BY a.uid
        HAVING count(*) FILTER (WHERE s.live) <> 1
        ORDER BY max(s.sid) DESC LIMIT {limit}
        """,
        "account {0} has {1} live rows out of {2}, which are rows {3} to {4}",
    ),
    (
        # A closure moves the money out before it moves the account to CLOSED, so a CLOSED account
        # that still reports a balance means the money left the account in core and never reached
        # the customer. No check in DbIntegrityCheckService covers this, because
        # PRODUCT_ACCOUNT_BALANCE_CHECK and CASH_TRANSACTIONS_BALANCE_CHECK both look for negative
        # balances and this balance is positive.
        "a closed account holds no money",
        "customer_product_account",
        """
        SELECT a.uid, s.current_state, a.product_account_balance,
               a.platform_fee_account_balance, s.sid
        FROM customer_product_account a
        JOIN customer_product_account_state s
          ON s.customer_product_account_sid = a.sid AND s.live
        WHERE s.current_state = 'CLOSED'
          AND a.product_account_balance <> 0
        ORDER BY s.sid DESC LIMIT {limit}
        """,
        "account {0} reads {1} and still holds {2} (platform fee {3}, row {4})",
    ),
    (
        "the order status equals its newest state row",
        "customer_product_order",
        """
        SELECT o.uid, o.order_status, s.current_state, s.sid, o.order_type
        FROM customer_product_order o
        JOIN LATERAL (
          SELECT sid, current_state FROM customer_product_order_state
          WHERE customer_product_order_sid = o.sid ORDER BY sid DESC LIMIT 1) s ON TRUE
        WHERE o.order_status::text <> s.current_state::text
        ORDER BY s.sid DESC LIMIT {limit}
        """,
        "order {0} reads {1}, and its newest state row is {2} (row {3}, a {4})",
    ),
)


def _psql(sql, timeout=120):
    """Returns the rows as lists of text, and the error psql gave, if it gave one."""
    try:
        done = subprocess.run(
            ["psql", CORE_DSN, "-tA", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as fault:
        return [], "{}: {}".format(type(fault).__name__, fault)
    if done.returncode != 0:
        return [], done.stderr.strip()[:300]
    return [line.split("|") for line in done.stdout.splitlines() if line.strip()], None


def sweep(limit=LIMIT):
    """Run every chain check once, and return what broke and what could not be read.

    A finding names the rule, the table, and the two rows that disagree, because a chain break
    stays in the data and a person chases it by row number.
    """
    findings = []
    errors = []
    for rule, table, query, shape in CHECKS:
        rows, error = _psql(query.format(limit=limit))
        if error:
            errors.append("{}: {}".format(rule, error))
            continue
        for row in rows:
            filled = list(row) + [""] * 5
            findings.append({
                "rule": rule,
                "table": table,
                "subject": row[0],
                "detail": shape.format(*filled[:5]),
                "row": row,
            })
    return findings, errors


# The service's own integrity checks. The harness has always read the chains it wrote itself and
# never asked the system what its own checks say, which is the question a person asks first when
# money is missing.
CLEARING_DSN = os.environ.get(
    "SIM_CLEARING_DSN", "postgresql://clearing:password@localhost:5440/clearing")

# Each endpoint runs a family of checks and answers with nothing, so the run reads the rows the
# checks write afterwards.
SYSTEM_CHECK_ENDPOINTS = (
    "/operations/processor/db-checks",
    "/operations/processor/clearing-integrity-checks",
    "/operations/processor/internal-reconciliation",
    "/operations/processor/order-flow-checks",
)

# Where each service writes its results. The two tables carry the same columns under different
# names, so each query is written out rather than shared.
SYSTEM_CHECK_TABLES = (
    ("core", CORE_DSN,
     "SELECT check_name, check_details, created_at FROM db_integrity_check "
     "WHERE check_passed = false AND created_at > now() - interval '{minutes} minutes' "
     "ORDER BY created_at DESC LIMIT {limit}"),
    ("clearing", CLEARING_DSN,
     "SELECT check_type, check_details, created_at FROM db_integrity_checks "
     "WHERE check_passed = false AND created_at > now() - interval '{minutes} minutes' "
     "ORDER BY created_at DESC LIMIT {limit}"),
)


def run_system_checks(ops_client):
    """Ask the service to run its own integrity checks. Returns the endpoints that refused."""
    refused = []
    for path in SYSTEM_CHECK_ENDPOINTS:
        call = ops_client.call("POST", path)
        if not getattr(call, "ok", False):
            refused.append("{} answered {}".format(path, getattr(call, "status", "nothing")))
    return refused


def _psql_on(dsn, sql, timeout=120):
    try:
        done = subprocess.run(
            ["psql", dsn, "-tA", "-F", "|", "-v", "ON_ERROR_STOP=1", "-c", sql],
            capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as fault:
        return [], "{}: {}".format(type(fault).__name__, fault)
    if done.returncode != 0:
        return [], done.stderr.strip()[:300]
    return [line.split("|") for line in done.stdout.splitlines() if line.strip()], None


# Checks that fail because the harness moves each bank's business date forward on purpose.
# BANK_BUSINESS_DATE_CHECK wants the date within a day of the wall clock, and
# SCHEDULE_SKIPPED_OCCURRENCE_CHECK counts the daily occurrences a jump steps over, so every run
# that advances a day fails both and the finding says nothing about the system.
MOVED_BY_THE_HARNESS = {"BANK_BUSINESS_DATE_CHECK", "SCHEDULE_SKIPPED_OCCURRENCE_CHECK"}


def system_check_failures(minutes=30, limit=LIMIT):
    """Every integrity check the service itself failed recently, from core and from clearing."""
    findings = []
    errors = []
    for service, dsn, query in SYSTEM_CHECK_TABLES:
        rows, error = _psql_on(dsn, query.format(minutes=int(minutes), limit=int(limit)))
        if error:
            errors.append("{} integrity checks: {}".format(service, error))
            continue
        for row in rows:
            name = row[0]
            if name in MOVED_BY_THE_HARNESS:
                continue
            details = row[1][:400] if len(row) > 1 else ""
            # A check that threw writes check_passed = false with only an error in its details.
            # That says the check could not run, which a network fault causes, and not that the
            # data broke the rule, so it goes with the reads that failed rather than the findings.
            if details.startswith('{"error"'):
                errors.append("{} could not run {}: {}".format(service, name, details))
                continue
            findings.append({
                "rule": "the service's own integrity check passes",
                "table": "{} {}".format(service, name),
                "subject": name,
                "detail": "{} failed its own check {}".format(service, name),
                "expected": "{} passes".format(name),
                "actual": details,
                "row": row,
            })
    return findings, errors
