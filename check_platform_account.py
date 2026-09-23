from __future__ import annotations

import json

from explorer import config, world
from explorer.client import BearerClient

PLATFORM_UID = "7a07b306-5730-42ca-9eca-6ea0b7e6f723"


def main():
    settings = config.load("local")
    ops = BearerClient(settings["ops_base_url"], world.ops_token(settings))
    call = world.platform_virtual_account(ops, PLATFORM_UID)
    print("status {}".format(call.status))
    print(json.dumps(call.body, indent=2)[:1200])
    ops.close()


if __name__ == "__main__":
    main()
