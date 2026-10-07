"""The calls one platform makes with another platform's identifiers, and what must follow.

The Direct API names the tenant in the token, so every identifier in a path is only reachable by
the platform that owns it. Each probe here sends one write or one read for another platform's
customer, account, batch, instruction, document or product, with a body the service would accept
from the owner, so a refusal can only be about ownership.

The published spec lists 400, 403 and 404 for every one of these paths and says nothing about which
a foreign identifier gets. Fleet 2 answered 400 "Customer not found" for every foreign read, so the
three are all accepted as a refusal. The harness holds no credentials of the other platform, so
what the call must leave alone is read from core's tables, before and after.
"""

from __future__ import annotations

import uuid

from explorer import actions, integrity

REFUSALS = (400, 403, 404)
# A 401 is this platform's own token failing, which says nothing about the other platform.
NOT_JUDGED = (401,)


class Probe:
    def __init__(self, name, method, path, body=None, writes=False, rule=None):
        self.name = name
        self.method = method
        self.path = path
        self.body = body
        self.writes = writes
        self.rule = rule


def _read_rule(what):
    return "a platform cannot read another platform's {}".format(what)


def _change_rule(what):
    return "another platform's {} cannot be changed".format(what)


def probes_for(theirs, mint, own_products=(), document_id=None):
    """Every probe the shared identifiers in `theirs` make possible, in a fixed order.

    `theirs` is one entry of the fleet's subjects file. A probe whose identifier was not shared is
    left out rather than sent with a made-up one, because a made-up identifier is refused for being
    unknown and proves nothing about ownership. The document and instruction paths are the
    exception when core holds no document or the run no pending withdrawal: the customer in the
    path is still the other platform's, and that is the boundary under test.
    """
    customer = theirs.get("customerId")
    account = theirs.get("accountId")
    batch = theirs.get("batchId")
    instruction = theirs.get("instructionId")
    product = theirs.get("productId")
    found = []
    if batch:
        path = "/direct/v1/batches/{}".format(batch)
        found.append(Probe("read the batch", "GET", path, rule=_read_rule("batch")))
        found.append(Probe(
            "cancel an allocation of the batch", "DELETE", path,
            body={"cancelAll": False, "instructionIds": [instruction or str(uuid.uuid4())]},
            writes=True, rule=_change_rule("batch")))
    if customer:
        base = "/direct/v1/customers/{}".format(customer)
        update = actions.update_customer_body(mint, "Pass", 0)
        update["person"]["email"] = "{}@example.com".format(mint("tnt", 40))
        nominated = actions.nominated_account_body({}, mint)
        nominated["nominatedAccount"]["accountName"] = "tenant probe {}".format(mint("tnt", 12))
        found += [
            Probe("change the customer", "PUT", base, body=update, writes=True,
                  rule=_change_rule("customer")),
            Probe("add a nominated account to the customer", "PATCH", base + "/nominated-account",
                  body=nominated, writes=True, rule=_change_rule("customer")),
            Probe("close the customer", "POST", base + "/close", writes=True,
                  rule=_change_rule("customer")),
            Probe("list the customer's documents", "GET", base + "/documents",
                  rule=_read_rule("customer documents")),
            Probe("read one of the customer's documents", "GET",
                  "{}/documents/{}".format(base, document_id or uuid.uuid4()),
                  rule=_read_rule("customer documents")),
        ]
        if account:
            held = "{}/accounts/{}".format(base, account)
            found += [
                Probe("close the account", "POST", held + "/close?reason=NO_LONGER_NEEDED",
                      writes=True, rule=_change_rule("account")),
                Probe("cancel the account's opening", "DELETE", held, writes=True,
                      rule=_change_rule("account")),
                Probe("cancel an instruction of the account", "DELETE",
                      "{}/instruction/{}".format(held, instruction or uuid.uuid4()), writes=True,
                      rule=_change_rule("account")),
                Probe("clear the account's maturity destination", "DELETE",
                      held + "/maturityDestination", writes=True, rule=_change_rule("account")),
                Probe("read the account's transactions", "GET", held + "/transactions",
                      rule=_read_rule("account transactions")),
            ]
            if product:
                found.append(Probe(
                    "set the account's maturity destination", "POST", held + "/maturityDestination",
                    body={"productId": product}, writes=True, rule=_change_rule("account")))
    if product and product not in own_products:
        found.append(Probe("read the product", "GET", "/direct/v1/products/{}".format(product),
                           rule=_read_rule("product")))
    return found


# Narrow on purpose: the other run keeps working on its own customer, so a wide row comparison
# would flag its own writes. These are the states a refused call would have to move.
FINGERPRINT_SQL = """
SELECT concat_ws('|',
  (SELECT pc.verification_status::text || ':' || pc.record_version FROM platform_customer pc
    WHERE pc.uid = '{u}'),
  (SELECT count(*) FROM platform_customer_status_history h
    JOIN platform_customer pc ON pc.sid = h.platform_customer_sid WHERE pc.uid = '{u}'),
  (SELECT coalesce(string_agg(cpa.sid || ':' || coalesce(s.current_state::text, '-'), ','
                              ORDER BY cpa.sid), '')
     FROM platform_customer pc
     JOIN customer_account ca ON ca.platform_customer_sid = pc.sid
     JOIN customer_product_account cpa ON cpa.customer_account_sid = ca.sid
     LEFT JOIN customer_product_account_state s
       ON s.customer_product_account_sid = cpa.sid AND s.live
    WHERE pc.uid = '{u}'),
  (SELECT coalesce(string_agg(o.sid || ':' || o.order_status::text, ',' ORDER BY o.sid), '')
     FROM platform_customer pc
     JOIN customer_account ca ON ca.platform_customer_sid = pc.sid
     JOIN customer_product_account cpa ON cpa.customer_account_sid = ca.sid
     JOIN customer_product_order o ON o.product_account_sid = cpa.sid
    WHERE pc.uid = '{u}'),
  (SELECT coalesce(string_agg(m.customer_product_account_sid || ':' || m.due_date || ':'
                              || coalesce(m.destination_product_account_sid::text, '-'), ','
                              ORDER BY m.customer_product_account_sid), '')
     FROM platform_customer pc
     JOIN customer_account ca ON ca.platform_customer_sid = pc.sid
     JOIN customer_product_account cpa ON cpa.customer_account_sid = ca.sid
     JOIN customer_product_account_maturity m ON m.customer_product_account_sid = cpa.sid
    WHERE pc.uid = '{u}'),
  (SELECT count(*) || ':' || count(*) FILTER (WHERE l.is_active)
     FROM cash_account_nominated_account_link l
     JOIN platform_customer pc ON pc.sid = l.customer_sid WHERE pc.uid = '{u}'))
"""
FINGERPRINT_PARTS = ("customer status and version", "status history rows", "account states",
                     "order states", "maturity destinations", "nominated accounts")


def fingerprint(customer_uid):
    """What a refused call must leave alone for this customer, or None when core cannot be read."""
    rows, error = integrity._psql(FINGERPRINT_SQL.format(u=uuid.UUID(str(customer_uid))))
    if error or not rows:
        return None
    return rows[0]


def changed_parts(before, after):
    if before is None or after is None or len(before) != len(after):
        return []
    return [(name, was, now) for name, was, now in zip(FINGERPRINT_PARTS, before, after)
            if was != now]


def document_of(customer_uid):
    """A document uid core holds for the customer, so the document probe names a real one."""
    rows, _ = integrity._psql(
        "SELECT d.uid FROM customer_document d JOIN platform_customer pc ON pc.sid = d.customer_sid "
        "WHERE pc.uid = '{}' ORDER BY d.sid DESC LIMIT 1".format(uuid.UUID(str(customer_uid))))
    return rows[0][0] if rows else None
