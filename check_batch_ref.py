from __future__ import annotations

from explorer.spec import Spec


def main():
    spec = Spec.load()
    print("=== schemas carrying a paymentReference field ===")
    for name, node in sorted(spec.schemas.items()):
        properties = node.get("properties", {})
        if "paymentReference" in properties:
            print("  {:36} {}".format(name, properties["paymentReference"].get("description", "")[:90]))
    print()
    print("=== BatchPaymentResponse fields ===")
    for field in spec.schemas.get("BatchPaymentResponse", {}).get("properties", {}):
        print("  {}".format(field))
    print()
    print("=== BatchPaymentAllocationResult fields ===")
    for field in spec.schemas.get("BatchPaymentAllocationResult", {}).get("properties", {}):
        print("  {}".format(field))


if __name__ == "__main__":
    main()
