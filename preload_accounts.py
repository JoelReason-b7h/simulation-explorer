"""Fills the preloaded virtual account pool the §9 preconditions need.

The schedulers that would do this are shedlock-guarded and off locally, so the harness has to ask.
"""

from __future__ import annotations

from explorer import config, world
from explorer.client import BearerClient


def main():
    settings = config.load("local")
    ops = BearerClient(settings["ops_base_url"], world.ops_token(settings))
    for path in ("/account/preload/investec/auto-preload",
                 "/operations/account/own/account/preloaded/activate"):
        call = ops.call("POST", path)
        print("POST {:52} {}".format(path, call.status))
        if call.body:
            print("     {}".format(str(call.body)[:300]))
    available = ops.call("GET", "/account/preload", params={"connectorType": "INVESTEC"})
    print("GET  /account/preload?connectorType=INVESTEC        {}".format(available.status))
    print("     {}".format(str(available.body)[:400]))
    ops.close()


if __name__ == "__main__":
    main()
