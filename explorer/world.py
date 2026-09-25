"""The calls a real integrator never makes.

Everything here is a test-stack accommodation. Locally the schedulers are off, so nothing moves
unless the harness asks; each call below stands in for a cron that would otherwise do the work.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from datetime import date
from pathlib import Path

PERF_ROOT = Path(__file__).resolve().parents[2] / "performance-testing"
if str(PERF_ROOT) not in sys.path:
    sys.path.insert(0, str(PERF_ROOT))

from lib.auth import CognitoUserAuth  # noqa: E402

from explorer import local_auth  # noqa: E402

DRAIN_TRANSACTIONS = "/operations/hsbc/statement/transactions/process"
POLL_TRANSACTIONS = "/operations/hsbc/statement/transactions/poll"
PLATFORM_SCHEDULED_TASK = "/operations/batch/processor/platform/{}/{}/sync"
SETTLE_OUTGOING = "/operations/processor/payments/outgoing"
SETTLE_TRANSFERS = "/operations/processor/payments/transfers"
PROCESS_GROUPS = "/operations/processor/payment/groups/process"
ENQUIRE_GROUPS = "/operations/processor/payment/groups/enquire"
ACCRUE_AND_REALISE = "/operations/batch/processor/bank/{}/ACCRUALS_AND_REALISATIONS/sync"
PROCESS_DUE_NOTICE = "/operations/processor/direct/notice"
PROCESS_CLOSURES = "/operations/processor/direct/account-closure"
SIMULATE_PLATFORM_CREDIT = "/simulator/direct/payment/platform/transactions"
SET_KYC_STATUS = "/simulator/direct/customer/{}/kyc/check"


def ops_token(settings):
    if settings.get("local_auth"):
        return local_auth.ops_token()
    auth = CognitoUserAuth(
        settings.get("cognito_client_id"),
        settings.get("cognito_username"),
        settings.get("cognito_password"),
    )
    return auth.get_token()


def platform_virtual_account(ops_client, platform_uid, currency="GBP"):
    """The platform's own virtual account identifier, which the funding payment is paid into."""
    return ops_client.call(
        "GET", "/operations/entity-internal-account/platforms/{}/currency/{}".format(
            platform_uid, currency))


def credit_platform_at_bank(hsb_client, virtual_iban, amount, reference, counterpart=None):
    """Credits the platform's virtual account at the bank, which is how the corpus funds a batch.

    This is not the Direct simulator's pooled deposit endpoint. That endpoint calls core-ro to
    resolve the platform's accounts, and the local compose stack gives the simulator no core-ro
    URL, so it fails with `Connection refused: localhost:4001`. The acceptance corpus never uses
    it — `PaymentsSteps` credits the bank instead, through `Payments.creditHsbcAccount`.
    """
    return hsb_client.call(
        "POST", "/hsb/accounts/{}/transactions".format(virtual_iban),
        json_body={
            "counterpartAccountId": counterpart,
            "amount": float(amount),
            # The SWIFT code, not the enum name: ExchangeDebitCreditCodeType is
            # CREDIT("C", "CRDT", "CREDIT") and the bank rejects "CREDIT" with
            # "Unexpected value: CREDIT".
            "debitCreditMark": "C",
            "reference": reference,
        },
    )


def poll_bank_transactions(ops_client, connector="INVESTEC", account_type="DIRECT",
                           currency="GBP", transactions_date=None):
    """Pulls what the bank holds into clearing as statement lines.

    Crediting the bank alone leaves `account_statement_line` empty, so processing finds nothing and
    every call still returns 200. The corpus polls before it processes.
    """
    return ops_client.call("POST", POLL_TRANSACTIONS, json_body={
        "connectorType": connector,
        "realAccountType": account_type,
        "internalCurrencyCode": currency,
        "transactionsDate": transactions_date or date.today().isoformat(),
        "taxWrapperType": "DEFAULT",
    })


def drain_transactions(ops_client):
    return ops_client.call("POST", DRAIN_TRANSACTIONS)


def raise_platform_dues(ops_client, platform_uid):
    """Raises the platform's own payment dues, which is what moves pooled money on to the accounts."""
    return [
        ops_client.call("POST", PLATFORM_SCHEDULED_TASK.format(platform_uid, event))
        for event in ("DEPOSIT_PAYMENT_DUE", "WITHDRAWAL_PAYMENT_DUE", "DISTRIBUTION_PAYMENT_DUE")
    ]


def advance_business_day(ops_client, bank_uid):
    """Moves this bank one business day on, and accrues and realises interest over that day.

    Direct accrual reads the bank's own persisted business date and self-advances: it accrues,
    calls setNextBusinessDate, then realises. So n calls put the cohort n days into logical time
    with no clock movement, which is the only way a run reaches a balance that has earned interest.
    """
    return ops_client.call("POST", ACCRUE_AND_REALISE.format(bank_uid))


def set_kyc_status(sim_client, customer_uid, status):
    """Forces a customer's compliance status, which nothing a tenant can do reaches.

    The surname decides the outcome of the first KYC check, so it can only set the status a
    customer is born with. This moves a live customer, which is how the run reaches a customer
    that compliance stopped after it had already opened and funded an account.
    """
    return sim_client.call("POST", SET_KYC_STATUS.format(customer_uid),
                           json_body={"customerStatus": status})


def enquire_payment_status(ops_client):
    """Ask the bank what became of each payment already sent.

    Sending a payment is not the same as learning its outcome. A payment the bank rejects stays
    as sent until this call reads the status back, so nothing downstream of a rejection runs
    without it.
    """
    return ops_client.call("POST", ENQUIRE_GROUPS)


def process_closures(ops_client):
    """Sweeps every account sitting at CLOSING, which is what finishes a close."""
    return ops_client.call("POST", PROCESS_CLOSURES)


def process_due_notice(ops_client):
    """Raises every notice withdrawal whose due date has come, which the 03:00 cron does.

    DirectNoticeWithdrawalOperations reads the due rows in one transaction and raises them in a
    second one, and it locks an account only when that account already reads CLOSING, so a close
    that lands between the two is the case worth racing against it.
    """
    return ops_client.call("POST", PROCESS_DUE_NOTICE)


CORE_DSN = os.environ.get("SIM_CORE_DSN", "postgresql://core:password@localhost:5432/core")


def bring_notice_due(account_uid):
    """Moves the account's waiting notice withdrawals to fall due today, in core's own date.

    The due date is core's date plus the notice period, and the run never moves core's clock,
    because every timestamp core writes would move with it. The acceptance helper TimeTravel moves
    payment dues the same way. Returns how many rows moved, or None when core could not be read.
    """
    try:
        uuid.UUID(str(account_uid))
    except ValueError:
        return None
    sql = (
        "UPDATE direct_customer_account_notice n "
        "SET due_date = (now() AT TIME ZONE 'Europe/London')::date "
        "FROM direct_customer_instruction i "
        "JOIN direct_customer_account dca ON dca.sid = i.direct_customer_account_sid "
        "JOIN customer_product_account cpa ON cpa.sid = dca.customer_product_account_sid "
        "WHERE n.direct_instruction_sid = i.sid AND n.processed_at IS NULL "
        "AND n.due_date > (now() AT TIME ZONE 'Europe/London')::date "
        "AND cpa.uid = '{}' RETURNING n.sid".format(account_uid))
    try:
        done = subprocess.run(["psql", CORE_DSN, "-tA", "-v", "ON_ERROR_STOP=1", "-c", sql],
                              capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return len([line for line in done.stdout.splitlines() if line.strip().isdigit()])


def settle_payments(ops_client):
    """The global sweep. One cohort's call moves every cohort's money, which is why §10's P1 exists."""
    return [
        ops_client.call("POST", SETTLE_OUTGOING),
        ops_client.call("POST", SETTLE_TRANSFERS),
        ops_client.call("POST", PROCESS_GROUPS),
    ]
