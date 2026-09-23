"""Prints what the harness derives from the published Direct API spec.

Run this after the spec changes. It reports the three things the harness depends on and
cannot recover from silently: how many operations it found, which path parameters bind,
and the length and pattern limits on every reference the harness has to mint itself.
"""

from __future__ import annotations

from explorer.spec import Spec


def operations(spec):
    found = spec.direct_operations()
    print("direct operations: {}".format(len(found)))
    print("direct paths:      {}".format(len({op.path for op in found})))
    print("schemas:           {}".format(len(spec.schemas)))
    print()
    for op in found:
        print("{:6} {:70} params={} -> {}".format(
            op.method.upper(), op.path, op.path_params or "-", op.success_schema))
    print()


def path_parameters(spec):
    print("=== path parameter usage ===")
    counts = {}
    for op in spec.direct_operations():
        for name in op.path_params:
            counts[name] = counts.get(name, 0) + 1
    for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print("  {:14} {}".format(name, count))
    print()


def minted_references(spec):
    """The harness mints these, so their limits decide the run-id prefix budget."""
    print("=== reference formats on request schemas ===")
    rows = []
    for name, node in spec.schemas.items():
        if not name.endswith("Request") and name != "CancelBatchOrder":
            continue
        for field, definition in node.get("properties", {}).items():
            if "eference" not in field:
                continue
            rows.append((field, name, definition.get("maxLength"), definition.get("pattern")))
    for field, owner, maximum, pattern in sorted(rows):
        print("  {:22} {:24} max={!s:6} pattern={}".format(field, owner, maximum, pattern))
    print()


def main():
    spec = Spec.load()
    operations(spec)
    path_parameters(spec)
    minted_references(spec)


if __name__ == "__main__":
    main()
