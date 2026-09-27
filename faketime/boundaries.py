"""Where a fake-clock run starts: a little before a moment worth crossing.

    python3 faketime/boundaries.py <rate>                       choose a start from the catalogue
    python3 faketime/boundaries.py <rate> "2026-12-31 18:00"    start there (London time)

Prints one JSON object: the SIM_CLOCK spec (UTC) and the boundary, which ./harness records in the
timeline so every cycle's results say where the run began and why.

A start with no time given is a random boundary from the catalogue within the next two years,
less a random 30 minutes to 6 hours, so the run crosses it early and never at the same second.
"""

from __future__ import annotations

import calendar
import json
import random
import sys
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")
UTC = timezone.utc
YEARS_AHEAD = 2
EARLIEST, LATEST = timedelta(minutes=30), timedelta(hours=6)
# The daily and monthly times the services' crons fire at, London time: accruals and the daily
# deposit at midnight, insured statistics and the ISA transfer-out at 01:00, the ISA projection and
# the Direct reconciliation at 02:00, deferred term deposits at 02:30, notice withdrawals and order
# staging at 03:00, the platform deposit due at 17:45 (end of day 17:00 plus 45 minutes), the
# interest cut-off and rate announcements at 18:00, the harness's accrual schedule at 20:00, the
# withdrawal due and the monthly MI at 23:00.
CRON_TIMES = (time(0), time(1), time(2), time(2, 30), time(3), time(17, 45), time(18), time(20),
              time(23))


def _london(day, at=time(0)):
    return datetime.combine(day, at, LONDON)


def _last_sunday(year, month):
    last = date(year, month, calendar.monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() + 1) % 7)


def _easter(year):
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return date(year, month, (h + l - 7 * m + 114) % 31 + 1)


def _month_ends(year):
    for month in range(1, 13):
        last = date(year, month, calendar.monthrange(year, month)[1])
        working = last
        while working.weekday() >= 5:
            working -= timedelta(days=1)
        yield last, working


def catalogue(year):
    """(name, instant) for every boundary in one year."""
    found = []
    for month, name in ((3, "GMT to BST"), (10, "BST to GMT")):
        found.append((name, datetime.combine(_last_sunday(year, month), time(1), UTC)))
    found.append(("London midnight in BST", _london(date(year, 7, 15))))
    for last, working in _month_ends(year):
        after = last + timedelta(days=1)
        found.append(("month end", _london(after)))
        if working != last:
            found.append(("month end, last working day", _london(working + timedelta(days=1))))
        if last.month in (3, 6, 9):
            found.append(("quarter end", _london(after)))
    found.append(("year end", _london(date(year + 1, 1, 1))))
    friday = date(year, 1, 1) + timedelta(days=(4 - date(year, 1, 1).weekday()) % 7)
    found.append(("Friday evening into Monday", _london(friday, time(17))))
    easter = _easter(year)
    for name, day in (("Christmas Day", date(year, 12, 25)), ("Boxing Day", date(year, 12, 26)),
                      ("Good Friday", easter - timedelta(days=2)),
                      ("Easter Monday", easter + timedelta(days=1))):
        found.append((name, _london(day)))
    if calendar.isleap(year):
        found.append(("29 February", _london(date(year, 2, 29))))
        found.append(("1 March after 29 February", _london(date(year, 3, 1))))
    day = date(year, 1, 1) + timedelta(days=random.randrange(365))
    at = random.choice(CRON_TIMES)
    found.append(("cron at {} London".format(at.strftime("%H:%M")), _london(day, at)))
    return found


def choose(now=None):
    now = now or datetime.now(UTC)
    candidates = [(name, at) for year in range(now.year, now.year + YEARS_AHEAD + 1)
                  for name, at in catalogue(year)
                  if now < at <= now + timedelta(days=366 * YEARS_AHEAD)]
    # Pick a kind first, so the twelve month ends a year do not crowd out the rest.
    kinds = sorted({name.split(",")[0] if name.startswith("month end") else name
                    for name, _ in candidates})
    kind = random.choice(kinds)
    name, at = random.choice([c for c in candidates if c[0] == kind or c[0].startswith(kind)])
    offset = timedelta(seconds=random.uniform(EARLIEST.total_seconds(), LATEST.total_seconds()))
    return name, at.astimezone(UTC), offset


def spec_for(rate, at_london=None):
    if at_london:
        start = datetime.strptime(at_london, "%Y-%m-%d %H:%M").replace(tzinfo=LONDON)
        boundary = {"name": "given", "at": start.astimezone(UTC).isoformat(), "offsetSeconds": 0}
    else:
        name, at, offset = choose()
        start = at - offset
        boundary = {"name": name, "at": at.isoformat(),
                    "atLondon": at.astimezone(LONDON).isoformat(),
                    "offsetSeconds": round(offset.total_seconds())}
    rate = float(rate)
    if rate <= 0:
        raise SystemExit("the rate must be a positive number, not {}".format(rate))
    spec = "@{} x{:g}".format(start.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S"), rate)
    return {"spec": spec, "boundary": boundary}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    print(json.dumps(spec_for(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)))
