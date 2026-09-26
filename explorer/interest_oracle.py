"""An interest oracle that recomputes each Direct account's accruals and realisations itself.

Every other interest reading the harness takes is core's own number read back. This module takes
core's inputs instead — the balance history in account_transaction and the rate and fee schedules
in rate_detail, platform_rate_detail and fee_detail — and redoes the arithmetic core's code states,
then holds each interest_accrual, interest_realised and INTEREST transaction row against it.

The rules mirrored, from apps/savings-exchange/core on origin/main:

- One accrual per calendar day for a Direct bank: DirectModelInterestProcessing.execute accrues
  the business date, then setNextBusinessDate moves it by plusDays(1) (InterestProcessing.java:58-62),
  and InvestecDirectAccrualFormula "accrues on every calendar day".
- Daily amount = principal * rate / 365, the division at DECIMAL128 (34 digits, HALF_EVEN), then
  8 dp HALF_UP (InvestecDirectAccrualFormula.java:24-32, InterestRoundingMode.java:11,49).
- Customer pot: customer balance at the reduced gross rate. Platform pot, only when the platform
  fee rate is non-zero: (customer + platform fee balance) at (platform fee + reduced rate), less the
  customer amount. Bondsmith pot, when gross > reduced and the fee type is INTEREST: (customer +
  platform fee + bondsmith fee balance) at the gross rate, less both (ProductAccountAccrualsCalculator
  .java:36-51, ProductAccountAccrualContext.java:65-84).
- Reduced gross = gross - platform fee - bondsmith fee when the fee type is INTEREST
  (FeeType.reducesCustomerGrossRate, FeeAndRateDetail.withProductAccountOverriddenGrossRate), and
  cpa.gross_rate_override replaces the gross rate when set.
- The rate for a value date is the one whose range covers that date (RateEffectiveDateRange:19-24).
- The balance is the account's balance when the accrual read it: cutoff_at is the newest
  account_transaction visible to that read (InterestSchedulerRepository.java:52-57), so the
  balance must equal the sum of the account's transactions up to cutoff_at.
- running_accrual carries from row to row (ProductAccountMoneyPot.applyAccrual:12-14).
- A realisation books running_accrual at 2 dp HALF_EVEN and carries the rest
  (ProductAccountMoneyPotAccrual.realise:26-30, InterestRoundingMode.java:21,31), records it in
  interest_realised_amount, and books one INTEREST transaction carrying the three pots when any is
  non-zero (RealisedInterestTransactionService.java:44-81).

Rate rows are rewritten in place when a later proposal supersedes them (end_date set, live
cleared), so the schedule as it stood when an accrual ran is rebuilt from created_at: a row counts
from its creation, and its end_date counts only once the row that follows it exists.

Everything here is read-only SQL with default_transaction_read_only on.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, Context, ROUND_HALF_EVEN, ROUND_HALF_UP

CORE_DSN = os.environ.get("SIM_CORE_DSN", "postgresql://core:password@localhost:5432/core")
CALCULATION = Context(prec=34, rounding=ROUND_HALF_EVEN)
DAYS = Decimal(365)
EIGHT = Decimal("0.00000001")
PENNY = Decimal("0.01")
ZERO = Decimal("0")
# How many findings of one rule one check reports. The first carries the evidence.
LIMIT = 20


def _psql(sql, timeout=300):
    env = dict(os.environ, PGOPTIONS="-c default_transaction_read_only=on")
    try:
        done = subprocess.run(["psql", CORE_DSN, "-tA", "-F", "\t", "-v", "ON_ERROR_STOP=1",
                               "-c", sql], capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as fault:
        return None, repr(fault)
    if done.returncode != 0:
        return None, done.stderr.strip()[:300]
    return [line.split("\t") for line in done.stdout.splitlines() if line.strip()], None


def _d(text):
    return Decimal(text) if text not in (None, "") else None


def _day(text):
    return date.fromisoformat(text[:10]) if text else None


def _when(text):
    if not text:
        return None
    # Python 3.9 reads only three or six fractional digits, and psql trims trailing zeros.
    match = re.match(r"(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d:\d\d)(?:\.(\d+))?([+-]\d\d)(?::?(\d\d))?$",
                     text.strip())
    if not match:
        return datetime.fromisoformat(text)
    day, clock, fraction, hours, minutes = match.groups()
    return datetime.fromisoformat("{}T{}.{}{}:{}".format(
        day, clock, (fraction or "0").ljust(6, "0")[:6], hours, minutes or "00"))


def accrue(principal, rate):
    """InvestecDirectAccrualFormula.getAccruedAmount."""
    return CALCULATION.divide(principal * rate, DAYS).quantize(EIGHT, rounding=ROUND_HALF_UP)


class Schedule:
    """Rows of one rate table for one product, answerable as of a moment for a value date."""

    def __init__(self, rows):
        # rows: (value, start, end, created_at)
        self.rows = sorted(rows, key=lambda r: r[3])

    def at(self, moment, value_date):
        known = [r for r in self.rows if r[3] <= moment]
        starts = {r[1] for r in known}
        covering = []
        for value, start, end, created in known:
            if start > value_date:
                continue
            # An end date is written onto a row when a later row takes over the next day. Before
            # that later row exists the row runs open-ended.
            if end is not None and value_date > end and (end + timedelta(days=1)) in starts:
                continue
            covering.append((created, value))
        return max(covering)[1] if covering else None


def _query_accounts(platform_uid, account_uids):
    where = []
    if platform_uid:
        where.append("pp.uid = '{}'".format(platform_uid))
    if account_uids:
        where.append("cpa.uid IN ({})".format(",".join("'{}'".format(u) for u in account_uids)))
    return """
      SELECT cpa.sid, cpa.uid, cpa.gross_rate_override, plp.sid, bp.sid, bp.product_type,
             bp.interest_feature_realisation_period, dca.status, pb.sid
      FROM customer_product_account cpa
      JOIN direct_customer_account dca ON dca.customer_product_account_sid = cpa.sid
      JOIN platform_product plp ON plp.sid = cpa.platform_product_sid
      JOIN partner_platform pp ON pp.sid = plp.platform_sid
      JOIN bank_product bp ON bp.sid = plp.product_sid
      JOIN partner_bank pb ON pb.sid = bp.bank_sid
      WHERE {}""".format(" AND ".join(where) or "true")


def check(platform_uid=None, account_uids=None, since_sid=0, limit=LIMIT):
    """Recompute the accounts' interest and answer (findings, stats, newest accrual sid read).

    `since_sid` limits the accrual rows judged to those after it, so a sweep on a timer reads only
    what is new; the row before the first judged one is still read, for the running accrual.
    """
    uids = [u for u in (account_uids or []) if u]
    if not platform_uid and not uids:
        return [], {"error": "no platform or account named"}, since_sid
    accounts, error = _psql(_query_accounts(platform_uid, uids))
    if accounts is None:
        return [], {"error": error}, since_sid
    if not accounts:
        return [], {"accounts": 0}, since_sid
    info = {row[0]: {"uid": row[1], "override": _d(row[2]), "pp": row[3], "bp": row[4],
                     "type": row[5], "period": row[6], "status": row[7], "bank": row[8]}
            for row in accounts}
    sids = ",".join(info)
    pps = ",".join(sorted({a["pp"] for a in info.values()}))
    bps = ",".join(sorted({a["bp"] for a in info.values()}))
    banks = ",".join(sorted({a["bank"] for a in info.values()}))

    # The realisation and its INTEREST transaction commit together, but they are read by two
    # queries, so a realisation committing between them reads as a transaction with none behind
    # it. Both reads stop a minute back, which no realisation transaction outlasts.
    settled_before = (_psql("SELECT now() - interval '60 seconds'")[0] or [["now()"]])[0][0]
    rates, e1 = _psql("SELECT bank_product_sid, gross_rate, start_date, end_date, created_at "
                      "FROM rate_detail WHERE bank_product_sid IN ({})".format(bps))
    fees, e2 = _psql("SELECT platform_product_sid, rate, start_date, end_date, created_at "
                     "FROM platform_rate_detail WHERE platform_product_sid IN ({})".format(pps))
    bfees, e3 = _psql("SELECT platform_product_sid, amount, start_date, end_date, created_at, type "
                      "FROM fee_detail WHERE platform_product_sid IN ({})".format(pps))
    dates, e4 = _psql("SELECT bank_sid, business_date FROM bank_business_date "
                      "WHERE bank_sid IN ({})".format(banks))
    # Every accrual row from the one before `since_sid` on, with its pots and the balances the
    # account's own transactions add up to at the row's cutoff.
    accruals, e5 = _psql("""
      WITH judged AS (
        (SELECT ia.* FROM interest_accrual ia
         WHERE ia.customer_product_account_sid IN ({sids}) AND ia.sid > {since})
        UNION ALL
        (SELECT DISTINCT ON (ia.customer_product_account_sid) ia.* FROM interest_accrual ia
         WHERE ia.customer_product_account_sid IN ({sids}) AND ia.sid <= {since}
         ORDER BY ia.customer_product_account_sid, ia.sid DESC))
      SELECT j.sid, j.customer_product_account_sid, j.value_date, j.created_at, j.cutoff_at,
             j.product_account_balance, j.platform_fee_account_balance,
             j.bondsmith_fee_account_balance, j.realised_interest_sid,
             max(a.accrual_amount) FILTER (WHERE a.pot_type = 'CUSTOMER'),
             max(a.running_accrual) FILTER (WHERE a.pot_type = 'CUSTOMER'),
             max(a.accrual_amount) FILTER (WHERE a.pot_type = 'PLATFORM_FEE'),
             max(a.running_accrual) FILTER (WHERE a.pot_type = 'PLATFORM_FEE'),
             max(a.accrual_amount) FILTER (WHERE a.pot_type = 'BONDSMITH_FEE'),
             max(a.running_accrual) FILTER (WHERE a.pot_type = 'BONDSMITH_FEE'),
             (SELECT coalesce(sum(t.customer_amount), 0) || '|' ||
                     coalesce(sum(t.platform_fee_amount), 0) || '|' ||
                     coalesce(sum(t.fee_amount), 0) || '|' || count(*)
                FROM account_transaction t
               WHERE t.customer_product_account_sid = j.customer_product_account_sid
                 AND j.cutoff_at IS NOT NULL AND t.created_at <= j.cutoff_at),
             (SELECT ir.uid FROM interest_realised ir
               WHERE j.cutoff_at IS NULL
                 AND ir.customer_product_account_sid = j.customer_product_account_sid
                 AND ir.value_date = j.value_date
                 AND abs(extract(epoch FROM ir.created_at - j.created_at)) < 2
               ORDER BY abs(extract(epoch FROM ir.created_at - j.created_at)) LIMIT 1)
      FROM judged j JOIN interest_accrual_amount a ON a.interest_accrual_sid = j.sid
      GROUP BY j.sid, j.customer_product_account_sid, j.value_date, j.created_at, j.cutoff_at,
               j.product_account_balance, j.platform_fee_account_balance,
               j.bondsmith_fee_account_balance, j.realised_interest_sid
      ORDER BY j.customer_product_account_sid, j.sid""".format(sids=sids, since=int(since_sid)),
        timeout=600)
    realised, e6 = _psql("""
      SELECT ir.uid, ir.customer_product_account_sid, ir.value_date, ir.created_at,
             max(ra.amount) FILTER (WHERE ra.pot_type = 'CUSTOMER'),
             max(ra.amount) FILTER (WHERE ra.pot_type = 'PLATFORM_FEE'),
             max(ra.amount) FILTER (WHERE ra.pot_type = 'BONDSMITH_FEE')
      FROM interest_realised ir JOIN interest_realised_amount ra ON ra.interest_realised_sid = ir.sid
      WHERE ir.customer_product_account_sid IN ({sids}) AND ir.created_at < '{before}'
      GROUP BY ir.uid, ir.customer_product_account_sid, ir.value_date, ir.created_at
      """.format(sids=sids, before=settled_before), timeout=600)
    booked, e7 = _psql("""
      SELECT customer_product_account_sid, value_date, customer_amount,
             coalesce(platform_fee_amount, 0), coalesce(fee_amount, 0), uid, created_at
      FROM account_transaction
      WHERE customer_product_account_sid IN ({sids}) AND transaction_type = 'INTEREST'
        AND created_at < '{before}'
      """.format(sids=sids, before=settled_before), timeout=600)
    # A transaction booked after an accrual read the balance belongs to a later day: the adjust
    # pass moves any such row still carrying the old date on to the new business date
    # (DirectModelInterestProcessing.java:29-38, InterestProcessing.adjustValueDate:77-92). Rows
    # younger than a minute are left out, because the pass may not have run yet.
    dated, e8 = _psql("""
      WITH acc AS (
        SELECT ia.sid, ia.customer_product_account_sid AS cpa, ia.value_date AS d,
               ia.cutoff_at AS c,
               lead(ia.cutoff_at) OVER (PARTITION BY ia.customer_product_account_sid
                                        ORDER BY ia.sid) AS nc
        FROM interest_accrual ia
        WHERE ia.customer_product_account_sid IN ({sids}) AND ia.cutoff_at IS NOT NULL)
      SELECT acc.cpa, acc.d, acc.c, t.uid, t.transaction_type, t.customer_amount, t.created_at,
             t.value_date
      FROM acc JOIN account_transaction t ON t.customer_product_account_sid = acc.cpa
       AND t.created_at > acc.c AND (acc.nc IS NULL OR t.created_at <= acc.nc)
      WHERE t.value_date <= acc.d AND acc.sid > {since}
        AND t.created_at < now() - interval '1 minute'
      ORDER BY t.created_at DESC LIMIT 50""".format(sids=sids, since=int(since_sid)),
        timeout=600)
    errors = [e for e in (e1, e2, e3, e4, e5, e6, e7, e8) if e]
    if errors:
        return [], {"error": "; ".join(errors)[:400]}, since_sid

    gross = defaultdict(list)
    for sid, value, start, end, created in rates:
        gross[sid].append((_d(value), _day(start), _day(end), _when(created)))
    gross = {k: Schedule(v) for k, v in gross.items()}
    platform_fee = defaultdict(list)
    for sid, value, start, end, created in fees:
        platform_fee[sid].append((_d(value), _day(start), _day(end), _when(created)))
    platform_fee = {k: Schedule(v) for k, v in platform_fee.items()}
    bondsmith = defaultdict(list)
    for sid, value, start, end, created, kind in bfees:
        bondsmith[sid].append(((_d(value), kind), _day(start), _day(end), _when(created)))
    bondsmith = {k: Schedule(v) for k, v in bondsmith.items()}
    business_date = {row[0]: _day(row[1]) for row in dates}

    realised_by_uid = {}
    realised_by_account = defaultdict(list)
    for uid, cpa, value_date, created, customer, platform, fee in realised:
        entry = {"uid": uid, "value_date": _day(value_date), "created": _when(created),
                 "CUSTOMER": _d(customer) or ZERO, "PLATFORM_FEE": _d(platform) or ZERO,
                 "BONDSMITH_FEE": _d(fee) or ZERO}
        realised_by_uid[uid] = entry
        realised_by_account[cpa].append(entry)
    booked_by_account = defaultdict(lambda: defaultdict(list))
    for cpa, value_date, customer, platform, fee, uid, created in booked:
        booked_by_account[cpa][_day(value_date)].append(
            (_d(customer), _d(platform), _d(fee), uid))

    findings = defaultdict(list)
    stats = {"accounts": len(info), "accrualsJudged": 0, "realisationsJudged": 0,
             "withPlatformFee": 0, "matchedBeforeARateCommitted": 0, "newestSid": since_sid}

    def find(rule, subject, detail, expected, actual):
        if len(findings[rule]) < limit:
            findings[rule].append({"rule": rule, "subject": subject, "detail": detail,
                                   "expected": str(expected), "actual": str(actual)})

    rows_by_account = defaultdict(list)
    for row in accruals:
        rows_by_account[row[1]].append(row)

    for cpa, rows in rows_by_account.items():
        account = info[cpa]
        uid = account["uid"]
        previous = None
        last_accrual_day = None
        for row in rows:
            sid = int(row[0])
            value_date, created, cutoff = _day(row[2]), _when(row[3]), _when(row[4])
            balance, platform_balance, fee_balance = _d(row[5]), _d(row[6]) or ZERO, _d(row[7]) or ZERO
            pots = {"CUSTOMER": (_d(row[9]) or ZERO, _d(row[10]) or ZERO),
                    "PLATFORM_FEE": (_d(row[11]) or ZERO, _d(row[12]) or ZERO),
                    "BONDSMITH_FEE": (_d(row[13]) or ZERO, _d(row[14]) or ZERO)}
            history = (row[15] or "0|0|0|0").split("|")
            realisation_uid = row[16] or None
            judged = sid > int(since_sid)
            stats["newestSid"] = max(stats["newestSid"], sid)
            if previous is not None and judged:
                for pot, (amount, running) in pots.items():
                    if running.compare(previous[pot][1] + amount) != 0:
                        find("the running accrual carries from row to row", uid,
                             "{} running accrual on {} is {} after {} of {}, and the row before "
                             "held {}".format(pot, value_date, running, "an accrual",
                                              amount, previous[pot][1]),
                             previous[pot][1] + amount, running)
            if realisation_uid:
                if judged:
                    stats["realisationsJudged"] += 1
                    entry = realised_by_uid.get(realisation_uid)
                    for pot, (amount, running) in pots.items():
                        before = previous[pot][1] if previous else ZERO
                        expected = before.quantize(PENNY, rounding=ROUND_HALF_EVEN)
                        if (-amount).compare(expected) != 0 or running.compare(before - expected) != 0:
                            find("a realisation books the running accrual to the penny", uid,
                                 "{} realised {} on {} with {} carried, from a running accrual "
                                 "of {}".format(pot, -amount, value_date, running, before),
                                 "{} carrying {}".format(expected, before - expected),
                                 "{} carrying {}".format(-amount, running))
                        if entry is not None and entry[pot].compare(-amount) != 0:
                            find("the realised amount equals the realisation's accrual row", uid,
                                 "{} interest_realised_amount on {} is {} and the accrual row "
                                 "realised {}".format(pot, value_date, entry[pot], -amount),
                                 -amount, entry[pot])
                previous = pots
                continue
            if judged:
                stats["accrualsJudged"] += 1
                if last_accrual_day is not None and value_date != last_accrual_day + timedelta(days=1):
                    find("a Direct account accrues once on every calendar day", uid,
                         "an accrual for {} follows the one for {}".format(value_date,
                                                                          last_accrual_day),
                         last_accrual_day + timedelta(days=1), value_date)
                total, platform_total, fee_total, count = (Decimal(history[0]),
                                                           Decimal(history[1]),
                                                           Decimal(history[2]), int(history[3]))
                if cutoff is not None and balance is not None and balance.compare(total) != 0:
                    find("an accrual reads the balance the transactions add up to", uid,
                         "the accrual for {} used a balance of {}, and the {} transactions up to "
                         "its cutoff {} add up to {}".format(value_date, balance, count, cutoff,
                                                              total), total, balance)
                if cutoff is not None and platform_balance.compare(platform_total) != 0:
                    find("an accrual reads the platform fee balance the transactions add up to",
                         uid, "the accrual for {} used a platform fee balance of {}, and the "
                              "transactions up to its cutoff add up to {}".format(
                                  value_date, platform_balance, platform_total),
                         platform_total, platform_balance)
                candidates = []
                for moment in candidate_moments(account, gross, platform_fee, bondsmith, created):
                    found = expected_pots(account, gross, platform_fee, bondsmith, moment,
                                          value_date, balance or ZERO, platform_balance,
                                          fee_balance)
                    if found is not None:
                        candidates.append(found)
                if not candidates:
                    find("an accrued day has a rate", uid,
                         "no rate covers {} for the account's product as of {}".format(
                             value_date, created), "a rate", "none")
                else:
                    matched = next((c for c in candidates
                                    if all(pots[p][0].compare(c[0][p]) == 0 for p in c[0])), None)
                    if matched is not None and matched is not candidates[0]:
                        stats["matchedBeforeARateCommitted"] += 1
                    wanted, rate_used = matched or candidates[0]
                    if rate_used[1]:
                        stats["withPlatformFee"] += 1
                    for pot in ("CUSTOMER", "PLATFORM_FEE", "BONDSMITH_FEE"):
                        amount = pots[pot][0]
                        if matched is None and amount.compare(wanted[pot]) != 0:
                            find("an accrual equals principal x rate / 365", uid,
                                 "{} accrued {} on {} on a balance of {} (platform fee balance "
                                 "{}), and gross {} less platform fee {} and fee {} gives {}"
                                 .format(pot, amount, value_date, balance, platform_balance,
                                         rate_used[0], rate_used[1], rate_used[2], wanted[pot]),
                                 wanted[pot], amount)
            last_accrual_day = value_date
            previous = pots

        # Accruals that never reach a realisation. An END_OF_DAY product realises on the day
        # after each accrual, so anything older than two days that is still unrealised was missed.
        today = business_date.get(account["bank"])
        if account["period"] == "END_OF_DAY" and today and rows:
            stale = [r for r in rows if not r[8] and not r[16]
                     and _day(r[2]) < today - timedelta(days=3) and int(r[0]) > int(since_sid)]
            if stale and account["status"] not in ("CLOSED", "CANCELLED"):
                find("every accrual of a daily-paying account is realised", uid,
                     "{} accruals up to {} are unrealised on business date {}".format(
                         len(stale), _day(stale[-1][2]), today), "none", len(stale))

    # Each realisation books one INTEREST transaction carrying its pots, when any is non-zero.
    for cpa, entries in realised_by_account.items():
        uid = info[cpa]["uid"]
        for entry in entries:
            pots = (entry["CUSTOMER"], entry["PLATFORM_FEE"], entry["BONDSMITH_FEE"])
            rows = booked_by_account[cpa].get(entry["value_date"], [])
            if all(p == 0 for p in pots):
                continue
            match = [r for r in rows if r[0] is not None and r[0].compare(pots[0]) == 0
                     and r[1].compare(pots[1]) == 0 and r[2].compare(pots[2]) == 0]
            if not match:
                find("a realisation books its INTEREST transaction", uid,
                     "the realisation on {} of {} to the customer, {} platform fee and {} fee "
                     "has INTEREST transactions {} on that value date".format(
                         entry["value_date"], pots[0], pots[1], pots[2],
                         [(str(r[0]), str(r[1]), str(r[2])) for r in rows]),
                     "one of {}|{}|{}".format(*pots), [str(r[0]) for r in rows])
    # And no INTEREST transaction without a realisation behind it.
    for cpa, by_day in booked_by_account.items():
        realised_days = defaultdict(int)
        for entry in realised_by_account.get(cpa, []):
            if any(entry[p] != 0 for p in ("CUSTOMER", "PLATFORM_FEE", "BONDSMITH_FEE")):
                realised_days[entry["value_date"]] += 1
        for day, rows in by_day.items():
            if len(rows) > realised_days.get(day, 0):
                find("every INTEREST transaction has a realisation", info[cpa]["uid"],
                     "{} INTEREST transactions on {} against {} realisations".format(
                         len(rows), day, realised_days.get(day, 0)),
                     realised_days.get(day, 0), len(rows))

    for cpa, day, cutoff, tx, kind, amount, created, value_date in dated:
        find("a transaction booked after an accrual's cutoff is dated after that day",
             info[cpa]["uid"],
             "{} {} {} created {} after the accrual for {} read the balance at {}, and carries "
             "value date {}".format(kind, amount, tx, created, day, cutoff, value_date),
             "after {}".format(day), value_date)

    stats["realisations"] = sum(len(v) for v in realised_by_account.values())
    flat = [f for rule in findings.values() for f in rule]
    stats["findings"] = len(flat)
    return flat, stats, stats["newestSid"]


# A rate row's created_at is when its INSERT ran, and the approval that inserts it commits later, so
# an accrual that read the rates in that gap still sees the schedule without the row. Seen on the
# stack at 0.46 s; ten seconds is the allowance, and each accepted case is counted in the stats.
COMMIT_ALLOWANCE = timedelta(seconds=10)


def candidate_moments(account, gross, platform_fee, bondsmith, moment):
    """The accrual's own moment first, then just before each rate row created in the allowance."""
    moments = [moment]
    for table, key in ((gross, account["bp"]), (platform_fee, account["pp"]),
                       (bondsmith, account["pp"])):
        if key in table:
            moments += [r[3] - timedelta(microseconds=1) for r in table[key].rows
                        if moment - COMMIT_ALLOWANCE < r[3] <= moment]
    return moments


def expected_pots(account, gross, platform_fee, bondsmith, moment, value_date, balance,
                  platform_balance, fee_balance):
    """({pot: amount}, (gross, platform fee rate, bondsmith fee rate)) or None when no rate."""
    gross_rate = gross.get(account["bp"]).at(moment, value_date) if account["bp"] in gross else None
    if gross_rate is None:
        return None
    if account["override"] is not None:
        gross_rate = account["override"]
    fee_rate = (platform_fee[account["pp"]].at(moment, value_date)
                if account["pp"] in platform_fee else None) or ZERO
    fee = bondsmith[account["pp"]].at(moment, value_date) if account["pp"] in bondsmith else None
    bondsmith_rate, fee_type = fee if fee else (ZERO, None)
    reduces = fee_type == "INTEREST"
    reduced = gross_rate - fee_rate - (bondsmith_rate if reduces else ZERO)
    customer = accrue(balance, reduced)
    platform = (accrue(balance + platform_balance, fee_rate + reduced) - customer
                if fee_rate != 0 else ZERO)
    fee_pot = (accrue(balance + platform_balance + fee_balance, gross_rate) - platform - customer
               if gross_rate > reduced and fee_type == "INTEREST" else ZERO)
    return ({"CUSTOMER": customer, "PLATFORM_FEE": platform, "BONDSMITH_FEE": fee_pot},
            (gross_rate, fee_rate, bondsmith_rate))


def realised_totals(platform_uid, limit=8):
    """{account uid: (customer uid, sum of realised customer interest)} for the platform's accounts
    with the most realisations, so a caller can hold the Direct API's INTEREST rows against it."""
    rows, _ = _psql("""
      SELECT cpa.uid, pc.uid, coalesce(sum(ra.amount), 0)
      FROM interest_realised ir
      JOIN interest_realised_amount ra ON ra.interest_realised_sid = ir.sid
       AND ra.pot_type = 'CUSTOMER'
      JOIN customer_product_account cpa ON cpa.sid = ir.customer_product_account_sid
      JOIN customer_account ca ON ca.sid = cpa.customer_account_sid
      JOIN platform_customer pc ON pc.sid = ca.platform_customer_sid
      JOIN partner_platform pp ON pp.sid = pc.platform_sid
      WHERE pp.uid = '{}'
      GROUP BY cpa.uid, pc.uid ORDER BY count(*) DESC LIMIT {}""".format(platform_uid, int(limit)))
    if rows is None:
        return None
    return {row[0]: (row[1], Decimal(row[2])) for row in rows}


def business_date(bank_uid):
    """The bank's business date as core holds it, or None."""
    rows, _ = _psql("SELECT b.business_date FROM bank_business_date b JOIN partner_bank pb "
                    "ON pb.sid = b.bank_sid WHERE pb.uid = '{}'".format(bank_uid), timeout=30)
    return _day(rows[0][0]) if rows else None


if __name__ == "__main__":
    import json
    import sys
    found, stats, newest = check(platform_uid=sys.argv[1] if len(sys.argv) > 1 else None,
                                 account_uids=sys.argv[2:])
    print(json.dumps(stats, indent=1, default=str))
    for f in found[:40]:
        print(json.dumps(f, default=str))
