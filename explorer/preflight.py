"""Refuses to start a run against an environment that cannot answer what the run assumes.

A run took 223 trials against a PLATFORM_UID that matched no platform at all, on a cohort whose
platform was INDIVIDUAL rather than POOLED. Every batch call was correctly refused, the ops calls
posted to a platform that did not exist and answered 200, and the output read like findings. The
harness had no way to tell, because it takes both identifiers from the environment and checks
neither.

Each check states what it proves. A check that cannot prove its claim says so rather than passing.
"""

from __future__ import annotations

from explorer import clock


class NotReady(Exception):
    """The environment cannot support a run. Carries every failed check, not just the first."""

    def __init__(self, failures):
        self.failures = failures
        super().__init__("\n".join("  - " + f for f in failures))


def _rows(body):
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        return body.get("content") or body.get("allocations") or []
    return []


def check(client, ops, hsb, platform_uid, virtual_iban):
    """Every precondition a run depends on, checked before the first action.

    Returns the product id the cohort uses, because finding it is part of proving the client is
    bound to a usable platform.
    """
    failures = []

    # 1. The credentials reach the Direct API at all, and the platform behind them has a product.
    products = client.call("GET", "/direct/v1/products")
    rows = _rows(products.body) if products.ok else []
    product_id = None
    if not products.ok:
        failures.append(
            "the Direct API refused GET /direct/v1/products with {} — the client id and secret do "
            "not reach a platform".format(products.status))
    elif not rows:
        failures.append(
            "the platform behind these credentials has no product, so no account can be opened")
    else:
        product_id = rows[0].get("productId")
        if not product_id:
            failures.append("the products read returned rows carrying no productId")

    # 2. The platform is POOLED. Batches are POOLED-only, so an INDIVIDUAL platform refuses every
    #    funding call and the run can never move money, which is most of what it exists to test.
    batches = client.call("GET", "/direct/v1/batches")
    message = ""
    if isinstance(batches.body, dict):
        message = str(batches.body.get("message") or "")
    if "INDIVIDUAL platforms are not allowed" in message:
        failures.append(
            "the platform behind these credentials is INDIVIDUAL, and batches are POOLED-only, so "
            "nothing can ever be funded — stand up a POOLED cohort")
    elif not batches.ok and batches.status not in (404,):
        failures.append(
            "GET /direct/v1/batches answered {} — {}".format(batches.status, message or "no message"))

    # 3. PLATFORM_UID names a real platform. The ops endpoint answers 200 for a uid that does not
    #    exist, so a wrong value is silent: every due raised goes nowhere.
    if not platform_uid:
        failures.append("PLATFORM_UID is not set")
    else:
        if clock.schedulers_run():
            # Raising a due by hand would run the platform's DEPOSIT_PAYMENT_DUE out of its
            # schedule, so the fake clock reads the platform's own account instead.
            probe = ops.call("GET", "/operations/entity-internal-account/platforms/{}/currency/GBP"
                             .format(platform_uid))
        else:
            probe = ops.call("POST", "/operations/batch/processor/platform/{}/{}/sync".format(
                platform_uid, "DEPOSIT_PAYMENT_DUE"))
        if not probe.ok:
            failures.append(
                "the ops endpoint refused PLATFORM_UID {} with {}".format(platform_uid, probe.status))

    # No check on the virtual account. Both bank reads tried answer the same way for an account
    # the bank minted itself and for one it has never seen, so neither can tell them apart, and a
    # check that cannot fail on a bad value only blocks good ones.
    if not virtual_iban:
        failures.append("PLATFORM_VIRTUAL_IBAN is not set")

    if failures:
        raise NotReady(failures)
    return product_id
