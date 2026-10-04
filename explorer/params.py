"""Reads with one query or path parameter wrong, the half of a request weird.py never touched.

weird.py mutates request bodies only, so a 500 that a bad query or path parameter causes went
unseen: SAV-11708 answered 500 when a caller left out partnerUid, and SAV-11143 refused a date
query parameter with an offset. Every Direct GET in the published spec is a candidate. One
parameter is changed at a time, and the change says whether the request is invalid, which the API
must refuse with a 4xx, or only odd, which it may accept. Neither may answer 5xx.

Only GETs are sent, so a probe changes nothing whatever the API does with it.
"""

from __future__ import annotations

import uuid
from urllib.parse import quote

from explorer.spec import Spec, _ref_name

# Path parameters the run holds a real value for.
HELD = {"customerId": "customerId", "accountId": "accountId", "productId": "productId",
        "batchId": "batchId", "documentId": "documentId"}


def _kind(param):
    schema = param.get("schema") or {}
    if _ref_name(schema) or "enum" in schema:
        return "enum"
    if schema.get("type") == "array":
        return "array"
    if schema.get("format") == "date":
        return "date"
    if schema.get("format") == "uuid":
        return "uuid"
    return schema.get("type") or "string"


# (name, value, invalid) by parameter kind. A value the API may reasonably read either way is
# odd, not invalid: the rule for it is only "no 5xx".
MUTATIONS = {
    "uuid": [("a malformed uuid", "not-a-uuid", True),
             ("a uuid with a trailing space", None, True),
             ("a uuid of nothing the platform owns", "random", True)],
    "date": [("a date with a time and offset", "2028-03-01T00:00:00+01:00", True),
             ("a month 13 date", "2028-13-01", True),
             ("a word for a date", "yesterday", True),
             ("a date in year 9999", "9999-12-31", False)],
    "integer": [("a negative number", "-1", True),
                ("a number past int32", "2147483648", True),
                ("a word for a number", "abc", True),
                ("a huge page", "1000000", False),
                ("zero", "0", False)],
    "number": [("a word for an amount", "abc", True),
               ("NaN for an amount", "NaN", True),
               ("an amount past a double", "1e400", True),
               ("a negative amount", "-0.01", False)],
    "enum": [("a value outside the enum", "NOT_A_VALUE", True),
             ("an empty value", "", False)],
    "array": [("a malformed list item", "not-a-uuid", True),
              ("an empty list item", "", False)],
    "string": [("10,000 characters", "x" * 10000, False),
               ("a NUL byte", "a\x00b", False),
               ("SQL text", "' OR 1=1 --", False)],
}


def operations(spec_path=None):
    """Every Direct GET with at least one parameter, from the published spec."""
    spec = Spec.load(spec_path)
    found = []
    for op in spec.direct_operations():
        if op.method != "get":
            continue
        params = [p for p in op.raw.get("parameters", []) if p.get("in") in ("path", "query")]
        if params:
            found.append((op.path, params))
    return found


def build(ops, held, index):
    """(path, query, description, invalid) for the index-th probe the held ids can fill, or None."""
    probes = []
    for path, params in ops:
        names = [p["name"] for p in params if p.get("in") == "path"]
        if any(not held.get(HELD.get(n, n)) for n in names):
            continue
        for param in params:
            for name, value, invalid in MUTATIONS.get(_kind(param), []):
                probes.append((path, params, param, name, value, invalid))
    if not probes:
        return None
    path, params, param, name, value, invalid = probes[index % len(probes)]
    filled = path
    for p in params:
        if p.get("in") != "path":
            continue
        real = held[HELD.get(p["name"], p["name"])]
        if p is param:
            if value == "random":
                real = str(uuid.uuid4())
            elif value is None:
                real = real + " "
            else:
                real = value
        filled = filled.replace("{" + p["name"] + "}", quote(str(real), safe=""))
    query = {param["name"]: value} if param.get("in") == "query" else None
    return filled, query, "{} for {} {}".format(name, param.get("in"), param["name"]), invalid
