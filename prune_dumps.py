"""Keep the database dumps in archive/ under a size budget, dropping the least recently used first.

    python3 prune_dumps.py            # budget from SIM_DUMP_BUDGET_GB, default 100

A dump folder's last use is the newest access time of its files, so copying one off the box with
scp or rsync counts as a use: relatime updates the access time on the first read after a write.
Only whole archive/<cycle>-db folders are removed; the cycle logs in archive/<cycle>.tgz stay.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

ARCHIVE = Path(__file__).resolve().parent / "archive"
BUDGET = float(os.environ.get("SIM_DUMP_BUDGET_GB", "100")) * 2**30


def folders():
    found = []
    for folder in ARCHIVE.glob("*-db"):
        files = [f for f in folder.iterdir() if f.is_file()]
        size = sum(f.stat().st_size for f in files)
        used = max([f.stat().st_atime for f in files] + [folder.stat().st_mtime])
        found.append((used, size, folder))
    return sorted(found)


def prune():
    dumps = folders()
    total = sum(size for _, size, _ in dumps)
    for _, size, folder in dumps:
        if total <= BUDGET:
            break
        shutil.rmtree(folder)
        total -= size
        print("removed {} ({:.0f} MB)".format(folder.name, size / 2**20))
    print("dumps {:.1f} GB of {:.0f} GB".format(total / 2**30, BUDGET / 2**30))


if __name__ == "__main__":
    prune()
