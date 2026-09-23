"""Drives the SAV-11580 sequence: fund an account, close it, then open a new one on the same product.

Before this fix the second open answers 500, because check_non_term_cpa_uniqueness counts an account
in CLOSING towards the one-account-per-product limit while validateAccountOpeningOrder does not.
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


def wait_for(client, path, predicate, what, attempts=30, pause=2.0):
    for attempt in range(attempts):
        call = client.call("GET", path)
        if call.ok and predicate(call.body):
            print("  settled: {} after {:.0f}s".format(what, attempt * pause))
            return call
        time.sleep(pause)
    print("  TIMED OUT waiting for {}".format(what))
    return None


def open_account(direct, customer_id, product_id, account_reference):
    return direct.call(
        "POST", "/direct/v1/customers/{}/accounts".format(customer_id),
        json_body={
            "productId": product_id,
            "accountReference": account_reference,
            "termsAndConditionsAcceptedAt": past_instant(),
        },
    )


def main():
    platform_uid = os.environ.get("PLATFORM_UID")
    virtual_iban = os.environ.get("PLATFORM_VIRTUAL_IBAN")
    if not platform_uid or not virtual_iban:
        print("set PLATFORM_UID and PLATFORM_VIRTUAL_IBAN from cohort.env")
        return 1

    settings = config.load("local")
    base_url, token_url, client_id, client_secret = config.require(
        settings, "base_url", "auth_token_url", "auth_client_id", "auth_client_secret"
    )
    direct = DirectClient(base_url, token_url, client_id, client_secret, settings.get("auth_scope"))
    ops = BearerClient(settings["ops_base_url"], world.ops_token(settings))
    hsb = BearerClient(settings["hsb_base_url"])
    print("run id {} against {}".format(RUN_ID, base_url))

    products = direct.call("GET", "/direct/v1/products", params={"productType": "INSTANT"})
    product_id = products.body["content"][0]["productId"]
    print("  product {}".format(product_id))

    created = direct.call("POST", "/direct/v1/customers", json_body=customer_body())
    show(created)
    customer_id = created.body["customerId"]

    if wait_for(
        direct, "/direct/v1/customers/{}".format(customer_id),
        lambda body: body.get("customerStatus") == "ACTIVATED",
        "customer {} reaching ACTIVATED".format(customer_id),
    ) is None:
        return 1

    first_reference = reference("acctA", 18)
    opened = open_account(direct, customer_id, product_id, first_reference)
    show(opened)
    if not opened.ok:
        return 1
    account_id = opened.body["accountId"]

    batch = direct.call(
        "POST", "/direct/v1/batches",
        json_body={
            "batchReference": reference("batch", 36),
            "paymentReference": reference("p", 16),
            "totalPaymentRequired": DEPOSIT,
            "allocations": [{
                "customerId": customer_id,
                "accountReference": first_reference,
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
    pay_reference = batch.body["paymentReference"]

    show(world.credit_platform_at_bank(hsb, virtual_iban, DEPOSIT, pay_reference,
                                       counterpart="GB29NWBK60161331926819"))
    show(world.poll_bank_transactions(ops))
    show(world.drain_transactions(ops))
    for call in world.raise_platform_dues(ops, platform_uid):
        show(call)
    for call in world.settle_payments(ops):
        show(call)
    show(world.drain_transactions(ops))

    account_path = "/direct/v1/customers/{}/accounts/{}".format(customer_id, account_id)
    if wait_for(direct, account_path, lambda body: body.get("status") == "OPEN",
                "account {} reaching OPEN".format(account_id)) is None:
        return 1

    closed = direct.call("POST", account_path + "/close", params={"reason": "NO_LONGER_NEEDED"})
    show(closed)
    if not closed.ok:
        return 1

    landed = direct.call("GET", account_path)
    print("  first account status after close: {}".format(landed.body.get("status")))
    if landed.body.get("status") != "CLOSING":
        print("  NOT CLOSING — the sequence under test needs CLOSING, stopping")
        return 1

    second = open_account(direct, customer_id, product_id, reference("acctB", 18))
    show(second)
    print()
    print("  second open status : {}".format(second.status))
    print("  second open body   : {}".format(json.dumps(second.body)[:400]))
    print("  RESULT: {}".format("PASS" if second.ok else "FAIL"))

    for client in (direct, ops, hsb):
        client.close()
    return 0 if second.ok else 1


if __name__ == "__main__":
    sys.exit(main())
