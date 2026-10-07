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

from explorer import clock, faults

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
          AND s.updated_at < now() - interval '{m5} minutes'
        ORDER BY s.sid DESC LIMIT {limit}
        """,
        "account {0} reads {1} and still holds {2} (platform fee {3}, row {4})",
    ),
    (
        # SAV-11278 caps a Direct withdrawal by the available balance, the ledger balance less the
        # other outbound instructions still waiting. Overlapping withdrawals that together ask for
        # more than the account holds show the cap was checked against the ledger alone.
        # SAV-11198 debits savings when the internal transfer settles and leaves the instruction
        # PENDING until the payout settles, so an instruction already debited is out of the
        # balance and is left out here, as DirectCustomerInstructionRepository
        # .sumPendingOutboundAmount leaves it out of the available balance.
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
          AND NOT EXISTS (SELECT 1 FROM direct_instruction_subject s
                          WHERE s.instruction_sid = i.sid
                            AND s.transaction_type = 'SAVINGS_WITHDRAWAL')
        GROUP BY a.uid, a.product_account_balance
        HAVING sum(coalesce(i.amount, 0)) > a.product_account_balance + 0.005
        ORDER BY max(i.sid) DESC LIMIT {limit}
        """,
        "account {0} has {3} open outbound instructions for {1} and holds {2} (newest {4})",
    ),
    (
        # CustomerActionPolicy holds a payout for a FROZEN customer (WITHDRAWAL_PAYOUT_EXECUTION),
        # and the hold is decided once, when the payout's PLATFORM payment due is raised. The
        # savings debit books later, when the bank statement shows the money left, so judging the
        # status at the booking flagged fleet 185's withdrawal paid 1.7 s before the freeze.
        "no withdrawal books while the customer is frozen",
        "account_transaction",
        """
        SELECT at.sid, pc.uid, at.customer_amount, pd.created_at
        FROM account_transaction at
        JOIN customer_product_account cpa ON cpa.sid = at.customer_product_account_sid
        JOIN customer_account ca ON ca.sid = cpa.customer_account_sid
        JOIN platform_customer pc ON pc.sid = ca.platform_customer_sid
        JOIN direct_instruction_subject dis ON dis.sid = at.balance_change_subject_id
        JOIN payment_due_direct_customer_instruction pd
          ON pd.direct_instruction_sid = dis.instruction_sid AND pd.payment_due_type = 'PLATFORM'
        WHERE at.transaction_type = 'SAVINGS_WITHDRAWAL'
          AND (SELECT h.to_state FROM platform_customer_status_history h
            WHERE h.platform_customer_sid = pc.sid AND h.transitioned_at <= pd.created_at
            ORDER BY h.transitioned_at DESC, h.sid DESC LIMIT 1) = 'FROZEN'
          AND (SELECT h.to_state FROM platform_customer_status_history h
            WHERE h.platform_customer_sid = pc.sid AND h.transitioned_at <= pd.created_at - interval '{s2} seconds'
            ORDER BY h.transitioned_at DESC, h.sid DESC LIMIT 1) = 'FROZEN'
        ORDER BY at.sid DESC LIMIT {limit}
        """,
        "transaction {0} paid {2} out of customer {1}'s account, its payout raised at {3} while the "
        "customer was FROZEN",
    ),
    (
        # A deposit is held for a FROZEN customer and refused for PENDING, DEACTIVATED, CANCELLED
        # and CLOSED (RECEIVE_RAIL_DEPOSIT), so none of them should book one. A batch deposit
        # settles under SETTLE_INSTRUCTION_GROUP instead, which holds only FROZEN: fleet 191 settled
        # batch u6fw4up68 into two customers deactivated after the batch was accepted.
        "no deposit books for a customer who may not receive one",
        "account_transaction",
        """
        SELECT at.sid, pc.uid, at.customer_amount, at.created_at, (SELECT h.to_state FROM platform_customer_status_history h
            WHERE h.platform_customer_sid = pc.sid AND h.transitioned_at <= at.created_at
            ORDER BY h.transitioned_at DESC, h.sid DESC LIMIT 1) AS status
        FROM account_transaction at
        JOIN customer_product_account cpa ON cpa.sid = at.customer_product_account_sid
        JOIN customer_account ca ON ca.sid = cpa.customer_account_sid
        JOIN platform_customer pc ON pc.sid = ca.platform_customer_sid
        WHERE at.transaction_type = 'SAVINGS_DEPOSIT'
          AND (SELECT h.to_state FROM platform_customer_status_history h
            WHERE h.platform_customer_sid = pc.sid AND h.transitioned_at <= at.created_at
            ORDER BY h.transitioned_at DESC, h.sid DESC LIMIT 1)
              IN ('FROZEN', 'PENDING', 'DEACTIVATED', 'CANCELLED', 'CLOSED')
          AND (SELECT h.to_state FROM platform_customer_status_history h
            WHERE h.platform_customer_sid = pc.sid AND h.transitioned_at <= at.created_at - interval '{s2} seconds'
            ORDER BY h.transitioned_at DESC, h.sid DESC LIMIT 1)
              IN ('FROZEN', 'PENDING', 'DEACTIVATED', 'CANCELLED', 'CLOSED')
          AND NOT (
            (SELECT h.to_state FROM platform_customer_status_history h
              WHERE h.platform_customer_sid = pc.sid AND h.transitioned_at <= at.created_at
              ORDER BY h.transitioned_at DESC, h.sid DESC LIMIT 1) <> 'FROZEN'
            AND EXISTS (SELECT 1 FROM direct_batch_instruction dbi
              WHERE dbi.payment_reference = at.payment_reference AND dbi.platform_sid = pc.platform_sid))
        ORDER BY at.sid DESC LIMIT {limit}
        """,
        "transaction {0} paid {2} into customer {1}'s account at {3} while the customer was {4}",
    ),
    (
        # core's own reconciliation of the Direct accounts against clearing. It writes to
        # internal_reconciliation, not db_integrity_check, so the service-check reader missed it.
        # The subject is the row, not the check type: the baseline drops every subject already
        # failing before a run, so a check that failed once was never reported again.
        "core's Direct reconciliation with clearing passes",
        "internal_reconciliation",
        """
        SELECT sid, check_type, bondsmith_account_balance, bank_account_balance,
               left(check_details::text, 600)
        FROM internal_reconciliation
        WHERE NOT check_passed AND check_type LIKE '%DIRECT%'
          AND created_at > now() - interval '{m30} minutes'
        ORDER BY sid DESC LIMIT {limit}
        """,
        "{1} failed in row {0}: {4}",
    ),
    (
        # The other types core reconciles: the HSBC and HSBC_INTL hub, e-money and platform cash
        # accounts. A row whose details are {"error": ...} is a check that could not run, because
        # clearing dropped the call, so it is not a mismatch.
        "core's internal reconciliation with clearing passes",
        "internal_reconciliation",
        """
        SELECT sid, check_type, bondsmith_account_balance, bank_account_balance,
               left(check_details::text, 600)
        FROM internal_reconciliation
        WHERE NOT check_passed AND check_type NOT LIKE '%DIRECT%'
          AND check_details::text NOT LIKE '{{"error"%'
          AND created_at > now() - interval '{m30} minutes'
        ORDER BY sid DESC LIMIT {limit}
        """,
        "{1} failed in row {0}: {4}",
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


def payments_left_unsent(limit=LIMIT, minutes=10):
    """Payment initiations clearing created but never sent, older than `minutes`.

    One payment with no creditor name made BatchedFileSender throw on every run and stranded
    every later file (finding 24); nothing in the run noticed for more than forty minutes.
    """
    rows, error = _psql_on(CLEARING_DSN,
        "SELECT pi.end_to_end_id, pi.created_at, pi.creditor_name IS NULL FROM payment_initiation pi "
        "JOIN payment_group pg ON pg.sid = pi.payment_group_sid "
        # A group held for approval is waiting on an operator, not stuck; only an APPROVED group
        # clearing should already have sent counts.
        "WHERE pg.status = 'APPROVED' "
        "AND pi.sent_at IS NULL AND pi.status IS NULL AND pi.is_return IS NOT TRUE "
        "AND pi.created_at < now() - interval '{} minutes' "
        "ORDER BY pi.sid LIMIT {}".format(clock.system_minutes(minutes), int(limit)))
    if error:
        return [], ["clearing payments: {}".format(error)]
    return [{
        "rule": "clearing sends every payment it creates",
        "table": "payment_initiation",
        "subject": row[0],
        "detail": "payment {} was created at {} and is still unsent{}".format(
            row[0], row[1], ", and has no creditor name" if row[2] == "t" else ""),
        "expected": "sent within {} minutes".format(minutes),
        "actual": "unsent",
        "row": row,
    } for row in rows if row], []


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
        "WHERE dca.status IN ('REQUESTED', 'OPEN') "
        "AND s.updated_at < now() - interval '" + str(clock.system_minutes(2)) + " minutes' "
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

    A withdrawal that core held for a frozen customer before cancelling it has a
    direct_customer_instruction_hold row and is exempt.
    """
    rows, error = _psql_on(CORE_DSN,
        "SELECT p.payment_due_uid, p.payment_due_type, i.uid, i.amount, i.full_balance_withdrawal "
        "FROM direct_customer_instruction i "
        "JOIN payment_due_direct_customer_instruction p ON p.direct_instruction_sid = i.sid "
        "WHERE i.status = 'CANCELLED' AND i.instruction_type = 'WITHDRAWAL' "
        "AND NOT EXISTS (SELECT 1 FROM direct_customer_instruction_hold h "
        "WHERE h.direct_instruction_sid = i.sid) "
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


def payments_to_unverified_payees(limit=LIMIT):
    """A payment sent or paid to a nominated account that has not passed Confirmation of Payee.

    DirectAccountValidator.validateNominatedAccountIsUsable refuses a withdrawal to an account
    that is not VERIFIED, and clearing keeps its own copy of the state on external_account.
    """
    rows, error = _psql_on(CLEARING_DSN, (
        "SELECT ppd.uid, ppd.value_amount, ea.uid, ea.account_state, ppd.payment_status "
        "FROM partner_payment_due ppd "
        "JOIN external_account ea ON ea.sid = ppd.external_counterpart_account_sid "
        "WHERE ppd.payment_status IN ('SENT', 'PRODUCED') AND ea.account_state <> 'VERIFIED' "
        "ORDER BY ppd.sid DESC LIMIT {}").format(limit))
    if error:
        return [], ["payments to unverified payees: {}".format(error)]
    return [{
        "rule": "no payment goes to a nominated account that failed Confirmation of Payee",
        "table": "partner_payment_due",
        "subject": row[0],
        "detail": "payment due {} of {} is {} to external account {}, which is {}".format(
            row[0], row[1], row[4], row[2], row[3]),
        "row": row,
    } for row in rows], []


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


def accounts_refunded_by_failed_payout(account_uids):
    """The CLOSED accounts whose money went back because the bank refused the closure payout.

    A refused payout that an operator sends REJECT_FAIL cancels the withdrawal and
    leaves its PLATFORM due FAILED, and core credits the money back to the account, which is the
    intended outcome. The harness's own settlement loop makes these decisions, so the data is read
    rather than the harness's record of them. An unreadable database exempts nothing.
    """
    if not account_uids:
        return set()
    rows, error = _psql(
        "SELECT a.uid, p.payment_due_uid FROM direct_customer_instruction i "
        "JOIN direct_customer_account d ON d.sid = i.direct_customer_account_sid "
        "JOIN customer_product_account a ON a.sid = d.customer_product_account_sid "
        "JOIN payment_due_direct_customer_instruction p ON p.direct_instruction_sid = i.sid "
        "WHERE i.instruction_type = 'WITHDRAWAL' AND i.status = 'CANCELLED' "
        "AND p.payment_due_type = 'PLATFORM' AND a.uid IN ({})".format(
            ", ".join("'{}'".format(u) for u in account_uids)))
    if error or not rows:
        return set()
    owner = {r[1]: r[0] for r in rows if len(r) > 1}
    failed, error = _psql_on(CLEARING_DSN,
        "SELECT uid FROM partner_payment_due WHERE payment_status = 'FAILED' AND uid IN ({})"
        .format(", ".join("'{}'".format(u) for u in owner)))
    if error:
        return set()
    return {owner[r[0]] for r in failed if r and r[0] in owner}


def sweep(limit=LIMIT):
    """Run every chain check once, and return what broke and what could not be read.

    A finding names the rule, the table, and the two rows that disagree, because a chain break
    stays in the data and a person chases it by row number.
    """
    findings = []
    errors = []
    for rule, table, query, shape in CHECKS:
        # The windows are real minutes; under a fake clock the services' clock covers rate times
        # as many.
        rows, error = _psql(query.format(limit=limit, m5=clock.system_minutes(5),
                                         m30=clock.system_minutes(30),
                                         s2=int(clock.system_seconds(2))))
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
    refunded = accounts_refunded_by_failed_payout(
        [f["subject"] for f in findings if f["rule"] == "a closed account holds no money"])
    findings = [f for f in findings
                if not (f["rule"] == "a closed account holds no money"
                        and f["subject"] in refunded)]
    unsent, unread = payments_left_unsent(limit)
    findings.extend(unsent)
    errors.extend(unread)
    paid, unread = payments_clearing_and_the_bank_disagree_on(limit)
    findings.extend(paid)
    errors.extend(unread)
    split, unread = accounts_core_and_clearing_disagree_on_closing(limit)
    findings.extend(split)
    errors.extend(unread)
    paid_out, unread = cancelled_withdrawals_that_pay_out(limit)
    findings.extend(paid_out)
    errors.extend(unread)
    unverified, unread = payments_to_unverified_payees(limit)
    findings.extend(unverified)
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
            "BETWEEN '{t}'::timestamptz AND '{t}'::timestamptz + interval '{m} minutes' "
            "ORDER BY pi.sid".format(a=amount, t=at, m=clock.system_minutes(3)))
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
    "/operations/processor/interest-checks",
)

# Where each service writes its results. The tables carry the same columns under different
# names, so each query is written out rather than shared. Core's interest checks write
# interest_integrity_check, not db_integrity_check (SAV-11730 moved the two interest checks there).
SYSTEM_CHECK_TABLES = (
    ("core", CORE_DSN,
     "SELECT check_name, check_details, created_at FROM db_integrity_check "
     "WHERE check_passed = false AND created_at > now() - interval '{minutes} minutes' "
     "ORDER BY created_at DESC LIMIT {limit}"),
    ("core interest", CORE_DSN,
     "SELECT check_name, check_details, created_at FROM interest_integrity_check "
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
    refused.extend(run_bank_reconciliations(ops_client))
    return refused


# Clearing's bank reconciliations run from prod-only schedulers, so the harness asks for them: the
# per-batch one for every batch the statement polls have completed, and the end-of-day one for the
# last statement date before today. The date is read from the statements clearing holds, because
# the route refuses today and later.
BANK_RECONCILIATION_PATHS = "/operations/processor/reconciliation/external/"


def run_bank_reconciliations(ops_client):
    refused = []
    path = BANK_RECONCILIATION_PATHS + "per-batch"
    call = ops_client.call("POST", path)
    if not getattr(call, "ok", False):
        refused.append("{} answered {}".format(path, getattr(call, "status", "nothing")))
    rows, error = _psql_on(CLEARING_DSN,
        "SELECT max(coalesce(from_date, to_date)) FROM account_statement_file "
        "WHERE coalesce(from_date, to_date) < (now() AT TIME ZONE 'Europe/London')::date")
    if error:
        return refused + ["last statement date: {}".format(error)]
    if rows and rows[0][0]:
        path = BANK_RECONCILIATION_PATHS + "date/" + rows[0][0]
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
        rows, error = _psql_on(dsn, query.format(minutes=clock.system_minutes(minutes),
                                                 limit=int(limit)))
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
    found, unread = bank_reconciliation_failures(minutes, limit)
    findings.extend(found)
    errors.extend(unread)
    return findings, errors


# A per-batch or fallback row fails on its own check_passed. An end-of-day row has no such column:
# the service counts it failed when no statement was found (run_id null) or any difference is not
# zero, as ExternalBankAccountReconciliationRepository.isReconciliationFailure does. Its key is the
# account and statement date, which each run overwrites, so updated_at carries the window.
BANK_RECONCILIATION_QUERIES = (
    ("per-batch or fallback", """
        SELECT 'external-' || sid, connector_type || ' ' || type || ' ' || currency,
               bondsmith_account_balance, bank_account_balance,
               bondsmith_account_balance - bank_account_balance, left(coalesce(comment, ''), 200)
        FROM external_reconciliation
        WHERE NOT check_passed AND created_at > now() - interval '{minutes} minutes'
        ORDER BY sid DESC LIMIT {limit}
        """),
    ("end-of-day", """
        SELECT 'eod-' || e.primary_internal_account_sid || '-' || e.statement_date,
               a.connector || ' ' || a.account_type || ' ' || a.currency || ' ' || e.statement_date,
               e.bondsmith_total_credit_sum - e.bondsmith_total_debit_sum,
               e.bank_total_credit_sum - e.bank_total_debit_sum,
               coalesce(e.total_credit_sum_diff, 0) - coalesce(e.total_debit_sum_diff, 0),
               CASE WHEN e.run_id IS NULL THEN 'no statement found' ELSE
                 'entries diff ' || coalesce(e.total_credit_entries_diff, 0) || ' credit, '
                 || coalesce(e.total_debit_entries_diff, 0) || ' debit' END
        FROM external_bank_account_reconciliation e
        JOIN internal_account a ON a.sid = e.primary_internal_account_sid
        WHERE e.updated_at > now() - interval '{minutes} minutes'
          AND (e.run_id IS NULL OR e.total_credit_entries_diff <> 0 OR e.total_credit_sum_diff <> 0
               OR e.total_debit_entries_diff <> 0 OR e.total_debit_sum_diff <> 0)
        ORDER BY e.updated_at DESC LIMIT {limit}
        """),
)


def bank_reconciliation_failures(minutes=30, limit=LIMIT):
    findings = []
    errors = []
    for kind, query in BANK_RECONCILIATION_QUERIES:
        rows, error = _psql_on(CLEARING_DSN, query.format(minutes=clock.system_minutes(minutes),
                                                          limit=int(limit)))
        if error:
            errors.append("clearing {} bank reconciliation: {}".format(kind, error))
            continue
        for row in rows:
            filled = list(row) + [""] * 6
            subject, account, bondsmith, bank, difference, note = filled[:6]
            findings.append({
                "rule": "clearing's bank reconciliation passes",
                "table": "clearing {} reconciliation".format(kind),
                "subject": subject,
                "detail": "{} reconciliation of {} failed: clearing holds {}, the bank holds {}, "
                          "difference {} ({})".format(kind, account, bondsmith, bank, difference,
                                                      note),
                "expected": "clearing and the bank agree",
                "actual": "difference {}".format(difference),
                "row": row,
            })
    return findings, errors
