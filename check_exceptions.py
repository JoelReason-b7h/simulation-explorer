from __future__ import annotations

import json

from explorer import config, world
from explorer.client import BearerClient


def main():
    settings = config.load("local")
    ops = BearerClient(settings["ops_base_url"], world.ops_token(settings))
    call = ops.call("GET", "/operations/account/statement/exceptions")
    print("status {}".format(call.status))
    print(json.dumps(call.body, indent=2)[:1800])
    ops.close()


if __name__ == "__main__":
    main()
