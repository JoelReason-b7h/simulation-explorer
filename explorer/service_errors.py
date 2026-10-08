"""Which ERROR signatures in a cycle's service logs have never been seen before.

after_cycle kept every ERROR line and counted them, about 95 a cycle, and nothing read them: a
notice-withdrawal run aborting on a check constraint and an unpaginated ops read failing past
10 MiB sat in that count for days. A signature is the service, the logger, the exception class
and the first exchange frame; with no exception, the message with its numbers and ids blanked.
Signatures seen in an earlier cycle are recorded in service-errors.seen.json, so only a new one
is reported.

    python3 -m explorer.service_errors fleet812.service-errors.log
"""

from __future__ import annotations

import glob
import json
import re
import sys
import tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
SEEN = HERE / "service-errors.seen.json"
# Cycles read to seed the record the first time, so the first judged cycle does not report every
# signature the stack has ever logged.
SEED_CYCLES = 60

ANSI = re.compile(r"\x1b\[[0-9;]*m")
HEAD = re.compile(r"\[([\w-]+)\] \S+ \[[^\]]*\] (?:\[traceId=[^\]]*\] )?ERROR (\S+) - (.*)")
EXCEPTION = re.compile(r"\[[\w-]+\] (?:Caused by: )?([\w.$]+(?:Exception|Error|Throwable))\b")
FRAME = re.compile(r"\[[\w-]+\] \tat (b7h\.[\w.$]+)\(")


def _blank(text):
    text = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>", text)
    text = re.sub(r"\b[0-9a-f]{16,}\b", "<id>", text)
    text = re.sub(r"'[^']*'|\"[^\"]*\"", "<v>", text)
    # An integrity check lists its failing rows, so the same failure with more rows is not new.
    text = re.sub(r"\[[^\]]*\]", "[...]", text)
    text = re.sub(r"\{[^}]*\}", "{...}", text)
    return re.sub(r"\d+(\.\d+)?", "N", text)


def signatures(lines):
    """{signature: {"count", "example"}} for one cycle's service-errors.log lines."""
    found = {}
    entry = None

    def close():
        if not entry:
            return
        if entry["exception"]:
            key = "{} | {} | {} | {}".format(entry["service"], entry["logger"], entry["exception"],
                                             entry["frame"] or "-")
        else:
            key = "{} | {} | {}".format(entry["service"], entry["logger"],
                                        _blank(entry["message"])[:100])
        seen = found.setdefault(key, {"count": 0, "example": entry["example"]})
        seen["count"] += 1

    for raw in lines:
        line = ANSI.sub("", raw.rstrip("\n"))
        head = HEAD.match(line)
        if head:
            close()
            entry = {"service": head.group(1), "logger": head.group(2), "message": head.group(3),
                     "exception": None, "frame": None, "example": line[:400]}
            continue
        if not entry:
            continue
        exception = EXCEPTION.match(line)
        if exception and entry["exception"] is None:
            entry["exception"] = exception.group(1)
        frame = FRAME.match(line)
        if frame and entry["frame"] is None:
            entry["frame"] = frame.group(1)
    close()
    return found


def _archived(name):
    try:
        with tarfile.open(HERE / "archive" / "{}.tgz".format(name)) as tar:
            member = tar.extractfile("{}.service-errors.log".format(name))
            return member.read().decode("utf8", "replace").splitlines() if member else []
    except (OSError, KeyError, tarfile.TarError):
        return []


def _seed():
    tars = sorted(glob.glob(str(HERE / "archive" / "fleet*.tgz")),
                  key=lambda p: int(re.sub(r"\D", "", Path(p).stem) or 0))[-SEED_CYCLES:]
    seen = {}
    for path in tars:
        name = Path(path).stem
        for key in signatures(_archived(name)):
            seen.setdefault(key, {"first": name, "last": name})["last"] = name
    return seen


def judge(name, log_path):
    """The signatures new in this cycle, as dicts, and records every signature as seen."""
    try:
        seen = json.loads(SEEN.read_text())
    except (OSError, ValueError):
        seen = _seed()
    try:
        lines = Path(log_path).read_text(errors="replace").splitlines()
    except OSError:
        return []
    new = []
    for key, entry in sorted(signatures(lines).items()):
        if key not in seen:
            new.append({"signature": key, "count": entry["count"], "example": entry["example"]})
            seen[key] = {"first": name, "last": name}
        else:
            seen[key]["last"] = name
    SEEN.write_text(json.dumps(seen, indent=1, sort_keys=True))
    return new


if __name__ == "__main__":
    path = Path(sys.argv[1])
    for key, entry in sorted(signatures(path.read_text(errors="replace").splitlines()).items(),
                             key=lambda kv: -kv[1]["count"]):
        print(entry["count"], key)
