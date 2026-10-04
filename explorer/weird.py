"""Requests a real integrator sends by mistake: a valid body with one thing wrong or strange.

Each mutation says whether the result is invalid, which the API must refuse with a 4xx, or only
odd, which it may accept. Neither may answer 5xx, and a refused request must change nothing.
"""

from __future__ import annotations

import copy

BASES = ("CreateCustomer", "OpenAccount", "PlaceWithdrawal", "AddNominatedAccount")


def _paths(body, prefix=()):
    """Every (path, value) in a JSON body, depth first."""
    if isinstance(body, dict):
        for key, value in body.items():
            yield prefix + (key,), value
            yield from _paths(value, prefix + (key,))
    elif isinstance(body, list):
        for index, value in enumerate(body):
            yield prefix + (index,), value
            yield from _paths(value, prefix + (index,))


def _set(body, path, value):
    target = body
    for step in path[:-1]:
        target = target[step]
    target[path[-1]] = value


def _drop(body, path):
    target = body
    for step in path[:-1]:
        target = target[step]
    del target[path[-1]]


def _first(body, names):
    for path, value in _paths(body):
        if path and path[-1] in names and not isinstance(value, (dict, list)):
            return path
    return None


AMOUNT = ("amount",)
LAST_FIELD = [None]
TEXT = ("customerReference", "accountReference", "instructionReference", "firstName",
        "accountName")
ENUM = ("instructionRequestType", "accountHolderType", "title", "currency")


def _amount(value):
    def apply(body):
        path = _first(body, AMOUNT)
        if path is None:
            return None
        _set(body, path, value)
        return body
    return apply


def _text(value):
    def apply(body):
        path = _first(body, TEXT)
        if path is None:
            return None
        _set(body, path, value)
        # Name the field in the finding: "reference of 5000 characters" was really accountName.
        LAST_FIELD[0] = ".".join(str(part) for part in path) if isinstance(path, (list, tuple)) else str(path)
        return body
    return apply


def _lower_enum(body):
    path = _first(body, ENUM)
    if path is None:
        return None
    current = body
    for step in path:
        current = current[step]
    _set(body, path, str(current).lower())
    return body


def _unknown_field(body):
    body["simulationExplorerUnknownField"] = "x"
    return body


def _drop_required(body):
    for name in ("productId", "instructionRequestType", "customerReference", "person",
                 "nominatedAccount", "accountReference"):
        if name in body:
            del body[name]
            return body
    return None


def _future_birth(body):
    path = _first(body, ("dateOfBirth",))
    if path is None:
        return None
    _set(body, path, "2099-01-01")
    return body


def _minor(body):
    path = _first(body, ("dateOfBirth",))
    if path is None:
        return None
    _set(body, path, "2012-06-01")
    return body


def _bad_sort_code(body):
    path = _first(body, ("sortCode",))
    if path is None:
        return None
    _set(body, path, "12-34-5")
    return body


# (name, invalid, mutate). An invalid request must be refused; an odd one may be accepted.
MUTATIONS = (
    ("negative amount", True, _amount("-1.00")),
    ("zero amount", True, _amount("0.00")),
    ("three decimal places", True, _amount("1.001")),
    ("amount in exponent form", True, _amount("1e2")),
    ("amount too large", True, _amount("99999999999999.99")),
    ("amount as a number", False, _amount(1.5)),
    ("amount as text", True, _amount("one pound")),
    ("unknown field", True, _unknown_field),
    ("required field missing", True, _drop_required),
    # Odd, not invalid: the Direct enums' @JsonCreator matches with equalsIgnoreCase on purpose
    # (ExternalDirectAccountHolderType.fromValue), so "individual" is accepted as INDIVIDUAL.
    ("enum in lower case", False, _lower_enum),
    ("text of 5000 characters", True, _text("r" * 5000)),
    ("non-ASCII text", False, _text("Zoë 名前 🙂")),
    ("SQL in text", False, _text("x'; DROP TABLE customer; --")),
    ("empty text", True, _text("")),
    # Postgres refuses a NUL in any text value, so one that reaches a query unchecked answers 500:
    # GET /direct/v1/customers?customerName=a%00b did (explorer/params.py, 2026-10-04).
    ("a NUL byte in text", False, _text("a\x00b")),
    ("text of spaces only", True, _text("   ")),
    ("a right-to-left override in text", False, _text("abc‮def")),
    ("date of birth in the future", True, _future_birth),
    ("customer under 18", True, _minor),
    ("malformed sort code", True, _bad_sort_code),
)


def mutate(body, index):
    """The body with the index-th applicable mutation, and that mutation, or (None, None)."""
    for offset in range(len(MUTATIONS)):
        name, invalid, apply = MUTATIONS[(index + offset) % len(MUTATIONS)]
        LAST_FIELD[0] = None
        changed = apply(copy.deepcopy(body)) if body is not None else None
        if changed is not None:
            if LAST_FIELD[0]:
                name = "{} in {}".format(name, LAST_FIELD[0])
            return changed, (name, invalid)
    return None, None
