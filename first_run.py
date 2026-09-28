"""Drives one customer to a REQUESTED account and reports what the reads show.

This is the plumbing check the driver sits on top of, and it doubles as the empirical test of
the §1 claim that `depositInfo` is populated for a POOLED customer.
"""

from __future__ import annotations

import json
import random
import string
import sys
from datetime import timedelta

from explorer import clock, config
from explorer.client import DirectClient

RUN_ID = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(6))


def reference(kind, limit):
    """Run-id prefixed and alphanumeric, because batchPaymentReference allows nothing else."""
    return (RUN_ID + kind)[:limit]


def past_instant():
    return (clock.utcnow_naive() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")


def customer_body():
    return {
        "accountHolderType": "INDIVIDUAL",
        "customerReference": reference("cust", 128),
        "fscsAcknowledgedAt": past_instant(),
        "nominatedAccounts": [{
            "accountName": "nominated account",
            "currency": "GBP",
            "accountHolderAddress": {
                "addressLine1": "123 Example Street", "addressLine2": "Flat 4B",
                "town": "London", "county": "Greater London",
                "postCode": "AB12 3CD", "country": "GBR",
            },
            "ukAccountDetails": {"sortCode": "100000", "accountNumber": "41610008"},
        }],
        "person": {
            "title": "MR",
            "firstName": "Sim",
            "lastName": "Explorer Pass",
            "dateOfBirth": "1995-03-07",
            "address": {
                "addressLine1": "123 Example Street", "addressLine2": "Flat 4B",
                "town": "London", "county": "Greater London",
                "postCode": "E54HNB", "country": "GBR",
            },
            "nationality": ["GBR"],
            "sourceOfFunds": "INSURANCE",
            "email": "{}@example.com".format(reference("sim", 40)),
            "phoneNumber": "07783746574",
            "annualIncome": "50000.00",
            "industry": "TECHNOLOGY_SOFTWARE_DEVELOPMENT",
            "nino": "XP332310B",
            "isVulnerable": False,
            "vulnerableDescription": "",
        },
    }


def show(call, note=""):
    marker = "ok " if call.ok else "REJ"
    print("  {} {:6} {:58} {} {}".format(marker, call.method, call.path, call.status, note))
    if not call.ok:
        print("      {}".format(json.dumps(call.body)[:400]))


def main():
    settings = config.load("local")
    base_url, token_url, client_id, client_secret = config.require(
        settings, "base_url", "auth_token_url", "auth_client_id", "auth_client_secret"
    )
    client = DirectClient(base_url, token_url, client_id, client_secret, settings.get("auth_scope"))
    print("run id {} against {}".format(RUN_ID, base_url))

    products = client.call("GET", "/direct/v1/products", params={"productType": "INSTANT"})
    show(products)
    if not products.ok:
        return 1
    content = products.body.get("content") or []
    if not content:
        print("  no INSTANT products on this platform — cannot open an account")
        return 1
    product_id = content[0]["productId"]
    print("      product {} ({})".format(product_id, content[0].get("productName")))

    created = client.call("POST", "/direct/v1/customers", json_body=customer_body())
    show(created)
    if not created.ok:
        return 1
    customer_id = created.body["customerId"]
    print("      customer {} status {}".format(customer_id, created.body.get("status")))

    opened = client.call(
        "POST", "/direct/v1/customers/{}/accounts".format(customer_id),
        json_body={
            "productId": product_id,
            "accountReference": reference("acct", 18),
            "termsAndConditionsAcceptedAt": past_instant(),
        },
    )
    show(opened)
    if not opened.ok:
        return 1
    account_id = opened.body["accountId"]

    read_back = client.call(
        "GET", "/direct/v1/customers/{}/accounts/{}".format(customer_id, account_id)
    )
    show(read_back)
    if read_back.ok:
        account = read_back.body
        print()
        print("  account status    : {}".format(account.get("status")))
        print("  paymentReference  : {!r}".format(account.get("paymentReference")))
        print("  depositInfo       : {}".format(json.dumps(account.get("depositInfo"))))
        print()
        print("  Both fields above are populated on a POOLED platform, which contradicts the")
        print("  @Schema text on depositInfo and on paymentReference. Read the platform's")
        print("  transfer_type from the database rather than inferring it from either field.")

    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
