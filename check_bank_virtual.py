from __future__ import annotations

import json

from explorer import config
from explorer.client import BearerClient


def main():
    settings = config.load("local")
    hsb = BearerClient(settings["hsb_base_url"])
    call = hsb.call("GET", "/hsb/accounts/virtual")
    count = len(call.body) if isinstance(call.body, list) else call.body
    print("status {} — virtual accounts known to the bank: {}".format(call.status, count))
    if isinstance(call.body, list):
        print(json.dumps(call.body[:6], indent=2)[:900])
    hsb.close()


if __name__ == "__main__":
    main()
