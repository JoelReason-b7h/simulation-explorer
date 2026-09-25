"""Mark every repeated unallocated statement line IGNORED once, outside a run.

    python3 checks/ignore_repeats.py

A run does this itself every minute; this is for a stack that already holds the repeats.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from explorer import config, local_auth, world  # noqa: E402
from explorer.client import BearerClient  # noqa: E402


def main():
    settings = config.load("local")
    if settings.get("local_auth"):
        local_auth.ensure_serving()
    ops = BearerClient(settings["ops_base_url"], world.ops_token(settings), timeout=600,
                       renew=lambda: world.ops_token(settings))
    result = world.ignore_repeated_exceptions(ops)
    ops.close()
    if result is None:
        print("the exception queue could not be read")
        return 1
    print("{} exception lines read, {} ignored as repeats, {} refused".format(*result))
    return 0 if not result[2] else 1


if __name__ == "__main__":
    sys.exit(main())
