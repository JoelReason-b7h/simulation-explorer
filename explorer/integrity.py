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

from explorer import faults

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
          -- An account reaches CLOSED a few seconds before its payout settles back into core,
          -- so a sweep inside that gap read two paid-out accounts as holding 3.00.
          AND s.updated_at < now() - interval '5 minutes'
        ORDER BY s.sid DESC LIMIT {limit}
        """,
        "account {0} reads {1} and still holds {2} (platform fee {3}, row {4})",
    ),
    (
        # SAV-11278 caps a Direct withdrawal by the available balance, the ledger balance less the
        # other outbound instructions still waiting. Overlapping withdrawals that together ask for
        # more than the account holds show the cap was checked against the ledger alone.
        "open withdrawals never ask for more than the account holds",
        "direct_customer_instruction",
        """
        SELECT a.uid, sum(coalesce(i.amount, 0)), a.product_account_balance, count(*),
               max(i.sid)
        FROM direct_customer_instruction i
        JOIN direct_customer_account d ON d.sid = i.direct_customer_account_sid
        JOIN customer_product_account a ON a.sid = d.customer_product_account_sid
        WHERE i.status IN ('PENDING', 'HELD')
          AND i.instruction_type IN ('WITHDRAWAL', 'PRODUCT_TRANSFER_OUT')
        GROUP BY a.uid, a.product_account_balance
        HAVING sum(coalesce(i.amount, 0)) > a.product_account_balance + 0.005
        ORDER BY max(i.sid) DESC LIMIT {limit}
        """,
        "account {0} has {3} open outbound instructions for {1} and holds {2} (newest {4})",
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


def operator_queues(limit=LIMIT):
    """What an operator would see waiting, read from the tables their screens read.

    Not a check. It shows that the queues exist and what sits in them, so a run can say whether a
    fault it caused reached anyone. The flagged payments screen selects `payment_state` in
    REJECTED, RETURNED and FLAGGED from `partner_payment`, as in
    PartnerPaymentPaginatedRepository. A payment group at PENDING_APPROVAL waits for an APPROVE or
    REJECT decision. Open ops tasks are the only thing that tells an operator to look.
    """
    queues = {}
    rows, error = _psql_on(CLEARING_DSN,
        "SELECT pp.payment_state, pp.debit_credit_mark, pp.value_amount, "
        "left(coalesce(pp.rejection_reason, ''), 120), pp.created_at FROM partner_payment pp "
        "WHERE pp.payment_state IN ('REJECTED', 'RETURNED', 'FLAGGED') "
        "ORDER BY pp.sid DESC LIMIT {}".format(int(limit)))
    queues["flagged payments"] = {"rows": rows, "error": error}
    rows, error = _psql_on(CLEARING_DSN,
        "SELECT pg.uid, coalesce(pg.message, ''), pg.amount, pi.end_to_end_id, pi.status, "
        "left(coalesce(pi.status_desc, ''), 120), pg.updated_at FROM payment_group pg "
        "LEFT JOIN payment_initiation pi ON pi.payment_group_sid = pg.sid AND pi.retry_uid IS NULL "
        "WHERE pg.status = 'PENDING_APPROVAL' ORDER BY pg.sid DESC LIMIT {}".format(int(limit)))
    queues["payment groups waiting for approval"] = {"rows": rows, "error": error}
    rows, error = _psql(
        "SELECT task_type, task_status, task_key, "
        "left(regexp_replace(task_description, '\\s+', ' ', 'g'), 120), created_at "
        "FROM operations_tasks WHERE task_status <> 'RESOLVED' "
        "ORDER BY sid DESC LIMIT {}".format(int(limit)))
    queues["open ops tasks"] = {"rows": rows, "error": error}
    return queues


def payments_clearing_and_the_bank_disagree_on(limit=LIMIT):
    """Payments clearing records as rejected that the bank records as paid, and the reverse.

    ClearinghouseHsbcPaymentService.mapPaymentStatusReport turns a response with no status code
    into RJCT, so a call cut after the bank accepted the payment is recorded as refused. The
    payments enquiry skips RJCT, so clearing never learns the money left. The two databases share
    only the end-to-end id, so the comparison is made here.
    """
    rows, error = _psql_on(CLEARING_DSN,
        "SELECT end_to_end_id, status, left(coalesce(status_desc, ''), 120) FROM payment_initiation "
        "WHERE end_to_end_id IS NOT NULL AND status IN ('RJCT', 'ACSC') "
        "ORDER BY sid DESC LIMIT 500")
    if error:
        return [], ["clearing payments: {}".format(error)]
    clearing = {r[0]: r for r in rows if r}
    if not clearing:
        return [], []
    found_rows, error = _psql_on(HSB_DSN,
        "SELECT end_to_end_id, status FROM payment_transaction_status WHERE end_to_end_id IN ({})"
        .format(", ".join("'{}'".format(i) for i in clearing)))
    if error:
        return [], ["the bank's payments: {}".format(error)]
    findings = []
    for end_to_end_id, bank_status in ((r[0], r[1]) for r in found_rows if len(r) > 1):
        ours = clearing[end_to_end_id][1]
        if ours == bank_status or {ours, bank_status} - {"RJCT", "ACSC"}:
            continue
        findings.append({
            "rule": "clearing and the bank agree on how a payment ended",
            "table": "payment_initiation",
            "subject": end_to_end_id,
            "detail": "payment {} reads {} in clearing and {} at the bank ({})".format(
                end_to_end_id, ours, bank_status, clearing[end_to_end_id][2]),
            "expected": "{} at the bank".format(ours),
            "actual": bank_status,
            "row": list(clearing[end_to_end_id]) + [bank_status],
        })
    return findings[:limit], []


def accounts_core_and_clearing_disagree_on_closing(limit=LIMIT):
    """Accounts core holds open that clearing has soft-closed.

    `cancelDirectCustomerAccount` calls clearing's softClose inside its own transaction, before it
    moves the account to CANCELLED. When the answer is lost, clearing has committed the soft close
    and core rolls back, so core keeps the account and later opens it, and clearing rejects every
    external credit to it. The two databases share only the clearing account uid, so the
    comparison is made here. Two minutes of quiet on the core state keeps a close in progress,
    which soft-closes first and commits CLOSING a moment later, out of the finding.
    """
    rows, error = _psql_on(CORE_DSN,
        "SELECT eia.account_uid, cpa.uid, dca.status, s.updated_at FROM direct_customer_account dca "
        "JOIN customer_product_account cpa ON cpa.sid = dca.customer_product_account_sid "
        "JOIN entity_internal_account eia ON eia.sid = dca.entity_internal_account_sid "
        "JOIN customer_product_account_state s ON s.customer_product_account_sid = cpa.sid AND s.live "
        "WHERE dca.status IN ('REQUESTED', 'OPEN') AND s.updated_at < now() - interval '2 minutes' "
        "ORDER BY dca.sid DESC LIMIT 1000")
    if error:
        return [], ["core accounts: {}".format(error)]
    core = {r[0]: r for r in rows if r}
    if not core:
        return [], []
    closed, error = _psql_on(CLEARING_DSN,
        "SELECT account_uid FROM internal_account WHERE soft_closed AND account_uid IN ({})"
        .format(", ".join("'{}'".format(u) for u in core)))
    if error:
        return [], ["clearing accounts: {}".format(error)]
    findings = []
    for (account_uid,) in (r[:1] for r in closed if r):
        _, product_account, status, since = core[account_uid]
        findings.append({
            "rule": "core and clearing agree on whether an account is closed",
            "table": "direct_customer_account",
            "subject": product_account,
            "detail": "account {} reads {} in core, and clearing has soft-closed {}".format(
                product_account, status, account_uid),
            "expected": "soft_closed false in clearing",
            "actual": "soft_closed true",
            "row": list(core[account_uid]),
        })
    return findings[:limit], []


def cancelled_withdrawals_that_pay_out(limit=LIMIT):
    """CANCELLED withdrawals whose money still left savings or still went to the nominated account.

    A cancelled withdrawal is right to carry dues only when the bank refused the payout and an
    operator sent REJECT_FAIL, which leaves its PLATFORM due FAILED. Otherwise:

    - an INTERNAL due that is PRODUCED moved the money from savings to the cash account, and
    - a PLATFORM due that is EXPECTED, SENT or PRODUCED pays the nominated account.

    Closing a NOTICE account does both: the notice processor raises the partial withdrawal, the
    close cancels it on status alone, and the close then drains the cash account. Clearing never
    stores core's PLATFORM due in that case, so the INTERNAL due is the one that shows it. The two
    databases share only the due uid.
    """
    rows, error = _psql_on(CORE_DSN,
        "SELECT p.payment_due_uid, p.payment_due_type, i.uid, i.amount, i.full_balance_withdrawal "
        "FROM direct_customer_instruction i "
        "JOIN payment_due_direct_customer_instruction p ON p.direct_instruction_sid = i.sid "
        "WHERE i.status = 'CANCELLED' AND i.instruction_type = 'WITHDRAWAL' "
        "ORDER BY i.sid DESC LIMIT 1000")
    if error:
        return [], ["cancelled withdrawals: {}".format(error)]
    dues = {r[0]: r for r in rows if len(r) > 4}
    if not dues:
        return [], []
    statuses, error = _psql_on(CLEARING_DSN,
        "SELECT uid, payment_status FROM partner_payment_due WHERE uid IN ({})"
        .format(", ".join("'{}'".format(u) for u in dues)))
    if error:
        return [], ["the dues of cancelled withdrawals: {}".format(error)]
    status_of = {r[0]: r[1] for r in statuses if len(r) > 1}
    by_instruction = {}
    for due_uid, due_type, instruction, amount, full in dues.values():
        held = by_instruction.setdefault(instruction, {"amount": amount, "full": full, "dues": []})
        held["dues"].append((due_type, due_uid, status_of.get(due_uid, "absent")))
    findings = []
    for instruction, held in by_instruction.items():
        platform_failed = any(t == "PLATFORM" and s == "FAILED" for t, _, s in held["dues"])
        moved = [(t, u, s) for t, u, s in held["dues"]
                 if (t == "INTERNAL" and s == "PRODUCED")
                 or (t == "PLATFORM" and s in ("EXPECTED", "SENT", "PRODUCED"))]
        if platform_failed or not moved:
            continue
        findings.append({
            "rule": "a cancelled withdrawal pays nothing out",
            "table": "direct_customer_instruction",
            "subject": instruction,
            "detail": "withdrawal {} of {} (full balance {}) is CANCELLED, and clearing holds {}"
                      .format(instruction, held["amount"], held["full"], ", ".join(
                          "its {} due {} {}".format(t, u, s) for t, u, s in held["dues"])),
            "expected": "no INTERNAL due PRODUCED and no live PLATFORM due",
            "actual": ", ".join("{} {}".format(t, s) for t, _, s in moved),
            "row": [instruction] + [u for _, u, _ in held["dues"]],
        })
    return findings[:limit], []


def dead_letters(limit=LIMIT):
    """Messages that failed on every delivery and went to the dead letter queue.

    Every queue on the local stack sends to `e2e-dlq` after 8 receives, so a consumer that throws
    for good leaves its message here and nothing else in the stack reports it. The message id is
    the subject, so the run's baseline keeps a message from an earlier run out of the findings.
    """
    found, error = faults.dead_letter_messages(limit)
    if error:
        return [], ["the dead letter queue: {}".format(error)]
    return [{
        "rule": "no message goes to the dead letter queue",
        "table": faults.DEAD_LETTER_QUEUE,
        "subject": message["id"],
        "detail": "a message from {} failed {} deliveries: {}".format(
            message["source"], message["receives"], message["body"]),
        "row": [message["id"], message["source"], message["sentAt"]],
    } for message in found], []


# Every rule the sweep applies, so the live page can name the checks that found nothing.
SWEEP_RULES = [rule for rule, _, _, _ in CHECKS] + [
    "clearing and the bank agree on how a payment ended",
    "core and clearing agree on whether an account is closed",
    "a cancelled withdrawal pays nothing out",
    "no message goes to the dead letter queue",
]


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
    paid, unread = payments_clearing_and_the_bank_disagree_on(limit)
    findings.extend(paid)
    errors.extend(unread)
    split, unread = accounts_core_and_clearing_disagree_on_closing(limit)
    findings.extend(split)
    errors.extend(unread)
    paid_out, unread = cancelled_withdrawals_that_pay_out(limit)
    findings.extend(paid_out)
    errors.extend(unread)
    dead, unread = dead_letters(limit)
    findings.extend(dead)
    errors.extend(unread)
    return findings, errors


def closed_account_evidence(account_uid):
    """Everything that says how an account reached CLOSED while holding money, read at once.

    A wipe removes these rows, and trials do not record account ids, so a closed account found by
    the sweep and not made by the run's own injection could not be explained afterwards. The
    evidence is read when the finding is made and saved with it.
    """
    evidence = {}
    rows, error = _psql(
        "SELECT s.sid, s.previous_state, s.current_state, s.updated_at "
        "FROM customer_product_account_state s "
        "JOIN customer_product_account a ON a.sid = s.customer_product_account_sid "
        "WHERE a.uid = '{}' ORDER BY s.sid".format(account_uid))
    evidence["core state chain"] = rows or [error]
    rows, error = _psql(
        "SELECT pc.uid FROM customer_product_account a "
        "JOIN customer_account ca ON ca.sid = a.customer_account_sid "
        "JOIN platform_customer pc ON pc.sid = ca.platform_customer_sid "
        "WHERE a.uid = '{}'".format(account_uid))
    if not rows:
        evidence["customer"] = [error or "no customer found"]
        return evidence
    customer = rows[0][0]
    evidence["customer"] = [[customer]]
    owned = ("JOIN internal_account ia ON ia.sid = {t}.account_sid "
             "JOIN account_owner ao ON ao.sid = ia.account_owner_sid "
             "WHERE ao.uid = '{c}'")
    rows, error = _psql_on(CLEARING_DSN,
        "SELECT cab.value_amount, cab.running_balance, cab.created_at FROM cash_account_balance cab "
        + owned.format(t="cab", c=customer) + " ORDER BY cab.sid")
    evidence["clearing cash lines"] = cash = rows or [error or "none"]
    rows, error = _psql_on(CLEARING_DSN,
        "SELECT ppd.payment_direction, ppd.value_amount, ppd.payment_status, ppd.created_at "
        "FROM partner_payment_due ppd " + owned.format(t="ppd", c=customer) + " ORDER BY ppd.sid")
    evidence["clearing payment dues"] = rows or [error or "none"]
    # A pooled payout leaves from the platform's account, so no column joins a payment to the
    # customer. The payments that follow the customer's last debit by the same amount are the
    # candidates, and the bank's own row for each says whether the money actually left.
    debits = [r for r in cash if isinstance(r, list) and len(r) > 2 and r[0].startswith("-")]
    if debits:
        amount, _, at = debits[-1][0].lstrip("-"), debits[-1][1], debits[-1][2]
        rows, error = _psql_on(CLEARING_DSN,
            "SELECT pi.end_to_end_id, pi.status, pi.amount, left(coalesce(pi.status_desc, ''), 120), "
            "pi.created_at FROM payment_initiation pi WHERE pi.amount = {a} AND pi.created_at "
            "BETWEEN '{t}'::timestamptz AND '{t}'::timestamptz + interval '3 minutes' "
            "ORDER BY pi.sid".format(a=amount, t=at))
        evidence["clearing payments after the last debit"] = rows or [error or "none"]
        ids = [r[0] for r in rows if r and r[0]]
        if ids:
            rows, error = _psql_on(HSB_DSN,
                "SELECT end_to_end_id, status, reason, amount, created_at "
                "FROM payment_transaction_status WHERE end_to_end_id IN ({}) ORDER BY sid".format(
                    ", ".join("'{}'".format(i) for i in ids)))
            evidence["the bank's record of those payments"] = rows or [error or "none"]
    return evidence


# The service's own integrity checks. The harness has always read the chains it wrote itself and
# never asked the system what its own checks say, which is the question a person asks first when
# money is missing.
CLEARING_DSN = os.environ.get(
    "SIM_CLEARING_DSN", "postgresql://clearing:password@localhost:5440/clearing")
HSB_DSN = os.environ.get("SIM_HSB_DSN", "postgresql://hsb:password@localhost:5444/hsb")

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
