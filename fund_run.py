"""Takes one customer from nothing to an OPEN account, through the pooled funding route.

A deposit is not an instruction call: it is a batch payment, a credit into the platform pot, and a
drain of what arrived. This script is the grouped action the design calls FundAccount, written out
step by step so each response can be read.
"""

from __future__ import annotations

import json
import os
import sys
import time

from explorer import config, world
from explorer.client import BearerClient, DirectClient
from first_run import RUN_ID, customer_body, past_instant, reference, show

DEPOSIT = "3.00"
PLATFORM_UID = "953f177b-6848-4992-92c4-bfa2782bf0cc"


def wait_for(client, path, predicate, what, attempts=30, pause=2.0):
    """Polls a read until it settles. No fixed wait, because the design rules those out."""
    for attempt in range(attempts):
        call = client.call("GET", path)
        if call.ok and predicate(call.body):
            print("  settled: {} after {:.0f}s".format(what, attempt * pause))
            return call
        time.sleep(pause)
    print("  TIMED OUT waiting for {}".format(what))
    return None


def main():
    settings = config.load("local")
    base_url, token_url, client_id, client_secret = config.require(
        settings, "base_url", "auth_token_url", "auth_client_id", "auth_client_secret"
    )
    direct = DirectClient(base_url, token_url, client_id, client_secret, settings.get("auth_scope"))
    ops = BearerClient(settings["ops_base_url"], world.ops_token(settings))
    hsb = BearerClient(settings["hsb_base_url"])
    print("run id {} against {}".format(RUN_ID, base_url))

    products = direct.call("GET", "/direct/v1/products", params={"productType": "INSTANT"})
    show(products)
    product_id = products.body["content"][0]["productId"]

    created = direct.call("POST", "/direct/v1/customers", json_body=customer_body())
    show(created)
    customer_id = created.body["customerId"]

    activated = wait_for(
        direct, "/direct/v1/customers/{}".format(customer_id),
        lambda body: body.get("customerStatus") == "ACTIVATED",
        "customer {} reaching ACTIVATED".format(customer_id),
    )
    if activated is None:
        last = direct.call("GET", "/direct/v1/customers/{}".format(customer_id))
        print("  customer stuck at {} — a batch allocation will be rejected".format(
            last.body.get("customerStatus") if last.ok else last.status))
        return 1

    account_reference = reference("acct", 18)
    opened = direct.call(
        "POST", "/direct/v1/customers/{}/accounts".format(customer_id),
        json_body={
            "productId": product_id,
            "accountReference": account_reference,
            "termsAndConditionsAcceptedAt": past_instant(),
        },
    )
    show(opened)
    account_id = opened.body["accountId"]

    batch = direct.call(
        "POST", "/direct/v1/batches",
        json_body={
            "batchReference": reference("batch", 36),
            "paymentReference": reference("p", 16),
            "totalPaymentRequired": DEPOSIT,
            "allocations": [{
                "customerId": customer_id,
                "accountReference": account_reference,
                "instructionReference": reference("i", 18),
                "instructionType": "DEPOSIT",
                "productId": product_id,
                "amount": DEPOSIT,
            }],
        },
    )
    show(batch)
    if not batch.ok:
        return 1
    print("  batch response fields: {}".format(sorted(batch.body)))
    print("  {}".format(json.dumps(batch.body)[:500]))

    # The batch's paymentReference, not the account's. Both fields carry that name and mean
    # different things: the account's names the per-account internal account, while the batch's is
    # what the funding payment must quote to match it. Quoting the account's leaves the batch
    # unfunded and the account stuck at REQUESTED, with every call still returning 200.
    pay_reference = batch.body["paymentReference"]

    # Pay into the platform's own virtual account at the bank, quoting the batch's payment
    # reference. The account's paymentReference is a different field with the same name.
    # The ops read returns the account uid but not its identifier, and no ops endpoint exposes the
    # IBAN, so the harness is told it. The acceptance suite reads it from the database for the same
    # reason. Setup may do that; the exploration itself stays on HTTP.
    account_call = world.platform_virtual_account(ops, PLATFORM_UID)
    show(account_call)
    virtual_iban = os.environ.get("PLATFORM_VIRTUAL_IBAN")
    if not virtual_iban:
        print("  set PLATFORM_VIRTUAL_IBAN — read it from core's entity_internal_account")
        return 1
    print("  crediting {} with {} reference {!r}".format(virtual_iban, DEPOSIT, pay_reference))

    # The corpus order, from seed_dm_per_cpa_lifecycle.feature: credit the bank, poll what it
    # holds into clearing, process the statement lines, raise the platform's own dues, then send
    # and process. Skipping the poll leaves account_statement_line empty and nothing moves, while
    # every call still returns 200.
    # The e2e suite always passes the platform's nominated account as the debtor; leaving it
    # null is the one difference left between this call and Payments.creditHsbcAccount.
    show(world.credit_platform_at_bank(hsb, virtual_iban, DEPOSIT, pay_reference,
                                       counterpart="GB29NWBK60161331926819"))
    show(world.poll_bank_transactions(ops))
    show(world.drain_transactions(ops))
    for call in world.raise_platform_dues(ops, PLATFORM_UID):
        show(call)
    for call in world.settle_payments(ops):
        show(call)
    show(world.drain_transactions(ops))

    wait_for(
        direct,
        "/direct/v1/customers/{}/accounts/{}".format(customer_id, account_id),
        lambda body: body.get("status") == "OPEN",
        "account {} reaching OPEN".format(account_id),
    )

    final = direct.call("GET", "/direct/v1/customers/{}/accounts/{}".format(customer_id, account_id))
    if final.ok:
        print()
        print("  status      : {}".format(final.body.get("status")))
        print("  balance     : {}".format(final.body.get("balance")))
        print("  depositInfo : {}".format(json.dumps(final.body.get("depositInfo"))))

    for client in (direct, ops, hsb):
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
