"""Compares the fields the published spec declares against the fields the live API returns.

The harness is driven from the published spec, so any field the running service returns but the
spec omits is a field the explorer will never look at.
"""

from __future__ import annotations

import json
import sys

from explorer import config
from explorer.client import DirectClient
from explorer.spec import Spec


def compare(spec, schema_name, body, label):
    declared = set(spec.schemas.get(schema_name, {}).get("properties", {}))
    returned = set(body) if isinstance(body, dict) else set()
    print("=== {} (spec schema {}) ===".format(label, schema_name))
    print("  declared but absent from the response: {}".format(sorted(declared - returned) or "none"))
    print("  returned but NOT in the spec:          {}".format(sorted(returned - declared) or "none"))
    print()


def main():
    spec = Spec.load()
    settings = config.load("local")
    base_url, token_url, client_id, client_secret = config.require(
        settings, "base_url", "auth_token_url", "auth_client_id", "auth_client_secret"
    )
    client = DirectClient(base_url, token_url, client_id, client_secret, settings.get("auth_scope"))

    customers = client.call("GET", "/direct/v1/customers", params={"size": 1})
    if not customers.ok:
        print("could not list customers: {}".format(customers.status))
        return 1
    content = customers.body.get("content") or []
    if not content:
        print("no customers on this platform yet — run first_run.py first")
        return 1
    compare(spec, "CustomerResponse", content[0], "GET /direct/v1/customers content[0]")
    customer_id = content[0]["customerId"]

    accounts = client.call("GET", "/direct/v1/customers/{}/accounts".format(customer_id))
    if accounts.ok:
        first = accounts.body[0] if isinstance(accounts.body, list) else accounts.body
        compare(spec, "SavingsAccountResponse", first, "GET .../accounts [0]")
        print("  sample account body:")
        print("  {}".format(json.dumps(first)[:600]))

    client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
