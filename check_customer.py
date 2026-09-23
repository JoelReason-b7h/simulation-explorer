from __future__ import annotations

import json

from explorer import config
from explorer.client import DirectClient


def main():
    settings = config.load("local")
    base_url, token_url, client_id, client_secret = config.require(
        settings, "base_url", "auth_token_url", "auth_client_id", "auth_client_secret"
    )
    direct = DirectClient(base_url, token_url, client_id, client_secret, settings.get("auth_scope"))

    listed = direct.call("GET", "/direct/v1/customers", params={"size": 3})
    content = listed.body.get("content") or []
    print("list content[0]:")
    print(json.dumps(content[0], indent=2)[:900] if content else "  none")
    if not content:
        return
    customer_id = content[0]["customerId"]
    detail = direct.call("GET", "/direct/v1/customers/{}".format(customer_id))
    print()
    print("detail {} -> {}".format(customer_id, detail.status))
    print(json.dumps(detail.body, indent=2)[:1200])
    direct.close()


if __name__ == "__main__":
    main()
