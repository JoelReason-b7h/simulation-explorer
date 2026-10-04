"""A read oracle that recomputes what the Direct API's read endpoints say from core's base tables.

Every other oracle holds a write or a ledger against core. This one holds the reads a platform
makes all day against the tables behind them: an account, a customer's balances, and a product with
its rates. Each derived value is rebuilt from the base tables by the rule the code states, not by
running the repository's own SQL, and the API is read between two reads of core, so a value that
moves (an accrual run, a deposit, a rate approval) is accepted anywhere between the two readings.

Account rules, from apps/savings-exchange/core on the deployed source:

- balance: the sum of the account's account_transaction customer_amount. The API prints
  customer_product_account.product_account_balance (DirectSavingsAccountRepository.java:34), which
  the interest oracle already holds against the same sum.
- totalAccruedInterestToDate: the CUSTOMER running_accrual of the newest interest_accrual row with
  realised_interest_sid NULL (value_date, created_at, sid descending; DirectSavingsAccountRepository
  .java:121-129), at 2 dp HALF_EVEN (DirectModelResponseMapper.toRealisedScale:97-102). Because each
  row's running_accrual is the previous one plus its accrual_amount, it also equals the sum of every
  CUSTOMER accrual_amount, which is the value used here, with the newest row held against it.
- lastAccrualDate: the value_date of that same row.
- nextInterestRealisationDate: interest_processing_schedule.realised_next_value_date, rebuilt from
  the payout period's rule (InterestRealisationPeriod.getNextRealisationDate:60-100) on the account's
  last realisation (the newest interest_realised value_date, else the schedule's opening day). A
  Direct bank (partner_bank.is_direct) takes the date as it falls, any other bank rolls to a working
  day (InterestRealisationPeriod.ifHolidayFetchNextWorkingDay:155-159). CLOSED has no schedule left
  (DirectModelInterestProcessing.closeOffSchedulesForClosingAccounts), and CLOSING is brought
  forward to tomorrow at the earliest (CustomerProductAccountInterestRealisationService
  .bringForwardFinalRealisation:113-131), so it may be null or earlier than the rule's date.
- maturityDate: working day on or after (the London date the maturity row was written + term
  months) on the bank's calendar (BondsmithBankCustomMaturityDateFormula.getMaturityDate:19; the
  BEFORE_HOLIDAY formula steps back instead). The row is written when the account opens
  (CustomerProductAccountStateService.openProductAccount:155-164).
- topUpDeadline: working day on or after (the same date + top_up_window_days)
  (TopUpFtdActivationService.activate:88-95), null without a window.
- projectedMaturityValue: DirectMaturityEstimation.projectedMaturityValue (DirectMaturityEstimation
  .java:30-52), non-null only for a TERM account with a balance above zero and a maturity date after
  today. The value is OrderEstimation.simulate (OrderEstimation.java:111-165) run from
  interest_processing_schedule.realised_last_value_date to the maturity date:
    day = last realised: accrue only
    day = maturity date: realise only
    otherwise: realise when it is the next realisation date, then accrue
  with accrue = principal * rate / 365 at DECIMAL128 then 8 dp HALF_UP (InvestecDirectAccrualFormula
  .java:24-32) on the balance after the last realisation, rate = the product's reduced gross rate for
  that day (4 dp, from the live rate slices) or, for an account carrying recorded term rates, gross -
  platform fee - bondsmith fee from the account (AccountRateOverride.RecordedFixedTermRates
  .reducedGrossRate:60-63), and realise = the running accrual at 2 dp HALF_EVEN, the remainder carried
  (ProductAccountMoneyPotAccrual.realise:26-30). The balance after the maturity day's realisation is
  the answer, at 2 dp HALF_EVEN. For a one-realisation product that is
  principal + round(sum of the daily accruals from the last realised day to the day before maturity).
- openedAt: documented as "the moment an account has a deposit completion and is therefore gone live"
  (ExternalDirectSavingsAccountResponse.openedAt). The first account_transaction is that deposit.
  The API prints customer_product_account.created_at (DirectSavingsAccountRepository.java:44), which
  is the moment the account was requested.
- depositInfo (REQUESTED only, DirectModelResponseMapper.depositInfo:158-166): minimumDeposit = the
  product's effective minimum (platform override first), instructedAmount = requested_amount,
  depositedAmount = the sum of cash_transaction on the account's internal account, requiredAmount =
  minimum - deposited (DirectDepositInfo:14-16).
- status, ukAccountDetail (null when the platform is POOLED), paymentReference.

Customer balances (direct_customer_holdings_fn): totalSavingsBalance = sum of the customer's
account balances, unallocatedCashBalance = sum of cash_transaction over the customer's accounts'
internal accounts, totalBalance = both, grouped by currency and by ISA / not ISA.

Product rules (live_rates_and_fees_fn, ExternalRateAndFeeMapper.mapFeeAdjustRateDetails,
InternalInterestPayoutPeriodType.getAer): the live rate_detail, platform_rate_detail and fee_detail
rows cut the calendar at every start_date and end_date + 1; each slice that a rate row covers shows
reduced gross = round(gross - platform rate - bondsmith fee when its type is INTEREST, 4), AER from
that rate, bonus and leaver from the rate row, and an announcedAt of the latest announcement among
the rows. Rates are fractions (0.045 is 4.5%). The API drops a slice that ended before today and
sorts by end date, open-ended last. The rest of the product is held against bank_product,
platform_product, platform_product_configuration, bank_product_term_feature and
bank_product_notice_feature.
"""

from __future__ import annotations

import calendar
import glob
import json
import os
import random
import re
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from explorer import clock
from explorer.interest_oracle import ZERO, PENNY, _d, _day, _psql, _when, accrue
from explorer.statement_oracle import aer as statement_aer

LONDON = ZoneInfo("Europe/London")
EPOCH = datetime(2000, 1, 1, tzinfo=timezone.utc)
FOUR = Decimal("0.0001")
# The status the client answers when nothing answered; it is not something the API said.
TRANSPORT_FAULT = 598
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MONEY_TEXT = re.compile(r"^-?\d+\.\d\d$")
# How many findings of one rule one check reports.
LIMIT = 20
# How often one account is read again when a value moved while it was read.
ATTEMPTS = 3
RETRY_PAUSE = 2.0
SAMPLE_ORDER = (("TERM", "OPEN"), ("INSTANT", "OPEN"), ("TERM", "REQUESTED"), ("NOTICE", "OPEN"),
                ("INSTANT", "CLOSING"), ("NOTICE", "CLOSING"), ("INSTANT", "CLOSED"),
                ("INSTANT", "REQUESTED"), ("NOTICE", "REQUESTED"), ("TERM", "CANCELLED"))
ANNIVERSARY = ("ANNUAL_PAYOUT", "MONTHLY_PAYOUT")
COMPOUNDING = ("ANNUAL_COMPOUNDING",)
EXTERNAL_PAYOUT = {"ANNUAL_PAYOUT": "AT_MATURITY_AND_ANNIVERSARY",
                   "MONTHLY_PAYOUT": "AT_MATURITY_AND_MONTHLY", "ANNUAL_COMPOUNDING": "ANNUALLY",
                   "AT_WITHDRAWAL": "AT_MATURITY"}


class ProjectionError(Exception):
    """The estimation would throw, and the API then leaves projectedMaturityValue out."""


# ---------------------------------------------------------------------------------------------
# core reads


def _rows(sql, timeout=120):
    rows, error = _psql(sql, timeout=timeout)
    if rows is None:
        raise RuntimeError(error)
    return rows


def _first(sql):
    rows = _rows(sql)
    return rows[0] if rows else None


def _uuid(text):
    if not UUID.match(str(text)):
        raise ValueError("not a uuid: {}".format(text))
    return text


def _flag(text):
    return text == "t"


def _list(text):
    text = (text or "").strip("{}")
    return [item.strip('"') for item in text.split(",") if item] if text else []


def _int(text):
    return int(text) if text not in (None, "") else None


def _london_day(instant):
    return instant.astimezone(LONDON).date() if instant else None


def _instant(text):
    """An API timestamp such as 2028-03-20T10:17:18.108096Z as an aware datetime, or None."""
    if not text:
        return None
    match = re.match(r"(\d{4}-\d\d-\d\d)T(\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)$", text)
    if not match:
        return datetime.fromisoformat(text)
    day, clock_text, fraction, zone = match.groups()
    return datetime.fromisoformat("{}T{}.{}{}".format(
        day, clock_text, (fraction or "0").ljust(6, "0")[:6], "+00:00" if zone == "Z" else zone))


def _money(text):
    return Decimal(text) if text not in (None, "") else None


def _add_months(day, months):
    """LocalDate.plusMonths: the day of month is kept, or clamped to the month's last day."""
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    month += 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


class Calendar:
    """National holidays and the working-day rules of NationalHolidayService."""

    def __init__(self):
        self.holidays = defaultdict(set)
        for region, value in _rows("SELECT region, value_date FROM national_holiday"):
            self.holidays[region].add(_day(value))

    def is_holiday(self, day, region):
        return day.weekday() >= 5 or day in self.holidays[region]

    def on_or_after(self, day, region):
        while self.is_holiday(day, region):
            day += timedelta(days=1)
        return day

    def before(self, day, region):
        day -= timedelta(days=1)
        while self.is_holiday(day, region):
            day -= timedelta(days=1)
        return day


def next_realisation(period, last, maturity, direct, region, calendar_):
    """InterestRealisationPeriod.getNextRealisationDate, or None when none is due."""
    def roll(day):
        return day if direct else calendar_.on_or_after(day, region)

    def before_maturity(supplier):
        return supplier() if maturity is None or last < maturity else None

    def anniversary(step):
        nxt = roll(step(last))
        return before_maturity(lambda: maturity if maturity is not None and nxt > maturity else nxt)

    if period == "END_OF_DAY":
        return roll(last + timedelta(days=1))
    if period == "END_OF_MONTH":
        return roll(_add_months(last, 1))
    if period == "START_OF_CALENDAR_MONTH":
        return roll(_add_months(last, 1).replace(day=1))
    if period == "END_OF_CALENDAR_MONTH":
        following = last + timedelta(days=1)
        # capitalisesOnLiteralCalendarMonthEnd only differs for a non-Direct bank, and that bank
        # rolls to a working day; a Direct bank takes the literal month end either way.
        return roll(following.replace(day=calendar.monthrange(following.year, following.month)[1]))
    if period == "END_OF_8_MONTH":
        return roll(_add_months(last, 8))
    if period == "AT_MATURITY":
        return before_maturity(lambda: maturity)
    if period in ("ANNUAL_PAYOUT", "ANNUAL_COMPOUNDING"):
        return anniversary(lambda d: _add_months(d, 12))
    if period == "MONTHLY_PAYOUT":
        return anniversary(lambda d: _add_months(d, 1))
    return None


def aer_of(rate, period, calculate_on, product_type, term):
    """InternalInterestPayoutPeriodType.getAer: a fraction at 4 dp HALF_DOWN, or None."""
    if rate is None:
        return None
    gross = float(rate) * 100
    if product_type == "TERM" and term is not None and term > 12:
        remaining = term % 12
        years = (term - remaining) // 12
        if period in ANNIVERSARY or period in COMPOUNDING:
            result = float(rate)
        else:
            ticks = years + remaining / 12.0
            result = (1 + (ticks * gross) / 100) ** (1 / ticks) - 1
    else:
        if period == "END_OF_DAY":
            periods = 366.0 if calendar.isleap(calculate_on.year) else 365.0
        elif period in ("END_OF_MONTH", "START_OF_CALENDAR_MONTH", "END_OF_CALENDAR_MONTH",
                        "MONTHLY_PAYOUT"):
            periods = 12.0
        elif period == "END_OF_8_MONTH":
            periods = 1.5
        elif period in ("AT_MATURITY", "ANNUAL_PAYOUT"):
            remaining = (term or 0) % 12
            periods = 1.0 if remaining == 0 else 12 / remaining
        else:
            periods = 1.0
        result = (1 + gross / (periods * 100)) ** periods - 1
    return Decimal(repr(result)).quantize(FOUR, rounding=ROUND_HALF_DOWN)


# ---------------------------------------------------------------------------------------------
# rate slices


def read_rate_rows(bp_sid, plp_sid):
    """The live rows behind a platform product's rates, as dicts."""
    rates = [{"gross": _d(g), "start": _day(s), "end": _day(e), "announced": _when(a),
              "bonus": _d(b), "leaver": _d(l)}
             for g, s, e, a, b, l in _rows(
                 "SELECT gross_rate, start_date, end_date, announced_at, bonus_rate, leaver_rate "
                 "FROM rate_detail WHERE bank_product_sid = {} AND live".format(int(bp_sid)))]
    platform = [{"rate": _d(r), "start": _day(s), "end": _day(e), "announced": _when(a)}
                for r, s, e, a in _rows(
                    "SELECT rate, start_date, end_date, announced_at FROM platform_rate_detail "
                    "WHERE platform_product_sid = {} AND live".format(int(plp_sid)))]
    fees = [{"amount": _d(r), "start": _day(s), "end": _day(e), "announced": _when(a), "type": k}
            for r, s, e, a, k in _rows(
                "SELECT amount, start_date, end_date, announced_at, type FROM fee_detail "
                "WHERE platform_product_sid = {} AND live".format(int(plp_sid)))]
    return rates, platform, fees


def rate_slices(rates, platform, fees):
    """Every (slice, rate row, platform fee row, bondsmith fee row) the live rows cut out."""
    boundaries = set()
    for row in rates + platform + fees:
        boundaries.add(row["start"])
        if row["end"] is not None:
            boundaries.add(row["end"] + timedelta(days=1))
    ordered = sorted(b for b in boundaries if b is not None)
    slices = []
    for index, start in enumerate(ordered):
        end = ordered[index + 1] - timedelta(days=1) if index + 1 < len(ordered) else None

        def covers(row):
            return row["start"] <= start and (row["end"] is None or (end is not None
                                                                     and row["end"] >= end))
        for rate in (r for r in rates if covers(r)):
            for plat in [p for p in platform if covers(p)] or [None]:
                for fee in [f for f in fees if covers(f)] or [None]:
                    slices.append({"from": start, "to": end, "rate": rate, "platform": plat,
                                   "fee": fee})
    return slices


def reduced_gross(piece):
    """round(determine_reduced_gross(gross - platform rate, fee amount, fee type), 4)."""
    value = piece["rate"]["gross"] - (piece["platform"]["rate"] if piece["platform"] else ZERO)
    fee = piece["fee"]
    if fee is not None and fee["type"] == "INTEREST":
        value -= fee["amount"]
    return value.quantize(FOUR, rounding=ROUND_HALF_UP)


def announced_of(piece):
    stamps = [r["announced"] for r in (piece["rate"], piece["platform"], piece["fee"])
              if r is not None and r["announced"] is not None]
    return max(stamps) if stamps else None


def expected_rate_details(slices, today, product):
    """The rateDetails the API must show for the product, sorted as the API sorts them."""
    shown = []
    for piece in slices:
        if piece["to"] is not None and piece["to"] < today:
            continue
        gross = reduced_gross(piece)
        saye = product["type"] == "SAYE"
        shown.append({"aerRate": None if saye else aer_of(gross, product["period"], today,
                                                          product["type"], product["term"]),
                      "grossRate": None if saye else gross,
                      "bonusRate": piece["rate"]["bonus"], "leaverRate": piece["rate"]["leaver"],
                      "startDate": piece["from"], "endDate": piece["to"],
                      "announcedAt": announced_of(piece)})
    shown.sort(key=lambda d: (d["endDate"] is None, d["endDate"] or date.min, d["startDate"]))
    return shown


def slice_at(slices, day):
    """The slice row that covers the day (the newest rate row when several do), or None."""
    covering = [p for p in slices if p["from"] <= day and (p["to"] is None or p["to"] >= day)]
    return max(covering, key=lambda p: p["rate"]["announced"] or EPOCH) if covering else None


# ---------------------------------------------------------------------------------------------
# estimation


def project(balance, last, maturity, period, running, rate_of, direct, region, calendar_):
    """OrderEstimation.simulate to the maturity date, then DirectMaturityEstimation's scale.

    Returns the projection as a Decimal, or None when the estimate is negative. Raises
    ProjectionError where the code would throw.
    """
    if balance <= 0:
        return balance
    held = balance
    due = next_realisation(period, last, maturity, direct, region, calendar_)
    if not last < maturity + timedelta(days=1):
        return held.quantize(PENNY, rounding=ROUND_HALF_EVEN)
    payout = ZERO
    day = last

    def realise(today_):
        nonlocal held, due, running, payout
        if due is None:
            raise ProjectionError("no next realisation date on {}".format(today_))
        if due != today_:
            return
        realised = running.quantize(PENNY, rounding=ROUND_HALF_EVEN)
        carry = running - realised
        if period in ANNIVERSARY:
            if today_ == maturity:
                payout += realised
        else:
            held += realised
        due = next_realisation(period, today_, maturity, direct, region, calendar_)
        running = carry

    while day <= maturity:
        if day == last:
            running += accrue(held, rate_of(day))
        elif day == maturity:
            realise(day)
        else:
            realise(day)
            running += accrue(held, rate_of(day))
        day += timedelta(days=1)
    final = max(held + payout if period in ANNIVERSARY else held, ZERO)
    return final.quantize(PENNY, rounding=ROUND_HALF_EVEN)


# ---------------------------------------------------------------------------------------------
# accounts


def sample_accounts(platform_uid, count):
    """Up to `count` of the platform's accounts, one per product type and status first."""
    rows = _rows("""
      SELECT uid, customer, sid, type, status FROM (
        SELECT cpa.uid, pc.uid AS customer, cpa.sid, bp.product_type AS type, dca.status,
               row_number() OVER (PARTITION BY bp.product_type, dca.status ORDER BY random()) AS n
        FROM direct_customer_account dca
        JOIN customer_product_account cpa ON cpa.sid = dca.customer_product_account_sid
        JOIN customer_account ca ON ca.sid = cpa.customer_account_sid
        JOIN platform_customer pc ON pc.sid = ca.platform_customer_sid
        JOIN partner_platform pp ON pp.sid = pc.platform_sid
        JOIN platform_product plp ON plp.sid = cpa.platform_product_sid
        JOIN bank_product bp ON bp.sid = plp.product_sid
        WHERE pp.uid = '{}') x WHERE n <= 3""".format(_uuid(platform_uid)), timeout=180)
    groups = defaultdict(list)
    for uid, customer, sid, kind, status in rows:
        groups[(kind, status)].append({"uid": uid, "customer": customer, "sid": int(sid),
                                       "type": kind, "status": status})
    keys = [k for k in SAMPLE_ORDER if k in groups]
    rest = [k for k in groups if k not in keys]
    random.shuffle(rest)
    keys += rest
    picked = []
    while len(picked) < count and any(groups[k] for k in keys):
        for key in keys:
            if groups[key] and len(picked) < count:
                picked.append(groups[key].pop())
    return picked


def read_static(account):
    sid = int(account["sid"])
    row = _first("""
      SELECT cpa.created_at, cpa.gross_rate_override, cpa.platform_fee_rate_override,
             cpa.bondsmith_fee_rate_override, cpa.rate_override_source, dca.requested_amount,
             dca.entity_internal_account_sid, plp.sid, plp.uid, bp.sid, bp.product_type,
             bp.interest_feature_realisation_period,
             coalesce(ppc.minimum_deposit_override, bp.deposit_requirement_min),
             bp.deposit_requirement_min, pb.is_direct, pb.holiday_calendar, pb.sid,
             bptf.term_period, bptf.top_up_window_days, pp.transfer_type,
             coalesce(cpc.individual_pay_by_reference, false), dca.payment_reference,
             cbc.maturity_date_formula_type
      FROM customer_product_account cpa
      JOIN direct_customer_account dca ON dca.customer_product_account_sid = cpa.sid
      JOIN customer_account ca ON ca.sid = cpa.customer_account_sid
      JOIN platform_customer pc ON pc.sid = ca.platform_customer_sid
      JOIN partner_platform pp ON pp.sid = pc.platform_sid
      JOIN platform_product plp ON plp.sid = cpa.platform_product_sid
      JOIN bank_product bp ON bp.sid = plp.product_sid
      JOIN partner_bank pb ON pb.sid = bp.bank_sid
      LEFT JOIN platform_product_configuration ppc ON ppc.platform_product_sid = plp.sid
      LEFT JOIN bank_product_term_feature bptf ON bptf.sid = bp.sid AND bptf.product_type = bp.product_type
      LEFT JOIN custom_platform_config cpc ON cpc.platform_sid = pp.sid
      LEFT JOIN custom_bank_config cbc ON cbc.bank_sid = pb.sid
      WHERE cpa.sid = {}""".format(sid))
    (created, gross_override, platform_override, bondsmith_override, source, requested, eia,
     plp_sid, plp_uid, bp_sid, kind, period, minimum, bank_minimum, direct, calendar_name,
     bank_sid, term, window, transfer, pay_by_reference, payment_reference, formula) = row
    account.update({
        "created": _when(created), "grossOverride": _d(gross_override),
        "platformOverride": _d(platform_override), "bondsmithOverride": _d(bondsmith_override),
        "overrideSource": source or None, "requested": _d(requested), "eia": int(eia),
        "plp": int(plp_sid), "plpUid": plp_uid, "bp": int(bp_sid), "type": kind, "period": period,
        "minimum": _d(minimum), "bankMinimum": _d(bank_minimum), "direct": _flag(direct),
        "region": calendar_name, "bank": int(bank_sid), "term": _int(term),
        "window": _int(window), "transfer": transfer, "payByReference": _flag(pay_by_reference),
        "paymentReference": payment_reference or None, "formula": formula or "BONDSMITH"})
    return account


def read_moving(account):
    """Everything about the account that can change while it is read."""
    sid = int(account["sid"])
    balance = _d(_first("SELECT coalesce(sum(customer_amount), 0) FROM account_transaction "
                        "WHERE customer_product_account_sid = {}".format(sid))[0])
    status = _first("SELECT status FROM direct_customer_account "
                    "WHERE customer_product_account_sid = {}".format(sid))[0]
    accruals = [{"sid": int(s), "day": _day(v), "created": _when(c), "open": _flag(o),
                 "amount": _d(a), "running": _d(r)}
                for s, v, c, o, a, r in _rows("""
      SELECT ia.sid, ia.value_date, ia.created_at, ia.realised_interest_sid IS NULL,
             iaa.accrual_amount, iaa.running_accrual
      FROM interest_accrual ia
      JOIN interest_accrual_amount iaa ON iaa.interest_accrual_sid = ia.sid
       AND iaa.pot_type = 'CUSTOMER'
      WHERE ia.customer_product_account_sid = {}""".format(sid))]
    schedule = _first("SELECT realised_last_value_date, realised_next_value_date "
                      "FROM interest_processing_schedule WHERE customer_product_account_sid = {}"
                      .format(sid))
    realised = _first("SELECT max(value_date) FROM interest_realised "
                      "WHERE customer_product_account_sid = {}".format(sid))
    cash = _d(_first("SELECT coalesce(sum(transaction_amount), 0) FROM cash_transaction "
                     "WHERE entity_account_sid = {}".format(account["eia"]))[0])
    maturity = _first("SELECT due_date, top_up_deadline, created_at FROM "
                      "customer_product_account_maturity WHERE customer_product_account_sid = {}"
                      .format(sid))
    first = _first("SELECT min(created_at) FROM account_transaction "
                   "WHERE customer_product_account_sid = {}".format(sid))
    moving = {"balance": balance, "status": status, "accruals": accruals,
              "scheduleLast": _day(schedule[0]) if schedule else None,
              "scheduleNext": _day(schedule[1]) if schedule and len(schedule) > 1 else None,
              "hasSchedule": schedule is not None,
              "lastRealised": _day(realised[0]) if realised else None, "cash": cash,
              "due": _day(maturity[0]) if maturity else None,
              "deadline": _day(maturity[1]) if maturity and len(maturity) > 1 else None,
              "maturityWritten": _when(maturity[2]) if maturity and len(maturity) > 2 else None,
              "firstTransaction": _when(first[0]) if first else None}
    if account["type"] == "TERM" and moving["due"] is not None:
        moving["rows"] = read_rate_rows(account["bp"], account["plp"])
    return moving


def newest(accruals):
    open_rows = [a for a in accruals if a["open"]]
    return max(open_rows, key=lambda a: (a["day"], a["created"], a["sid"])) if open_rows else None


def derive(account, moving, today, calendar_):
    """What the API must say, from one reading of core. Missing values come back as None."""
    status = moving["status"]
    latest = newest(moving["accruals"])
    total = sum((a["amount"] for a in moving["accruals"]), ZERO) if moving["accruals"] else None
    expected = {
        "status": status, "balance": moving["balance"],
        "totalAccruedInterestToDate": None if latest is None else
            total.quantize(PENNY, rounding=ROUND_HALF_EVEN),
        "chainBreak": None if latest is None or total == latest["running"] else
            (latest["running"], total),
        "lastAccrualDate": latest["day"] if latest else None}

    # next realisation
    nxt, mode = None, "exact"
    if moving["hasSchedule"] and status not in ("CLOSED", "REQUESTED", "CANCELLED"):
        last = moving["lastRealised"] or moving["scheduleLast"]
        nxt = next_realisation(account["period"], last, moving["due"], account["direct"],
                               account["region"], calendar_) if last else None
        mode = "closing" if status == "CLOSING" else "exact"
    expected["nextInterestRealisationDate"] = (mode, nxt) if mode == "closing" else nxt

    # maturity and top-up
    due, deadline = None, None
    written = _london_day(moving["maturityWritten"])
    if moving["due"] is not None and written is not None and account["term"]:
        if account["formula"] == "BEFORE_HOLIDAY":
            estimate = _add_months(written, account["term"])
            due = (calendar_.before(estimate, account["region"])
                   if calendar_.is_holiday(estimate, account["region"]) else estimate)
        elif account["formula"] == "BONDSMITH":
            due = calendar_.on_or_after(_add_months(written, account["term"]), account["region"])
        if account["window"] is not None:
            deadline = calendar_.on_or_after(written + timedelta(days=account["window"]),
                                             account["region"])
    expected["maturityDate"] = due
    expected["topUpDeadline"] = deadline
    expected["maturityFormulaKnown"] = moving["due"] is None or due is not None

    # projection
    projected, skipped = None, None
    if (account["type"] == "TERM" and moving["due"] is not None and moving["due"] > today
            and moving["balance"] > 0):
        slices = rate_slices(*moving["rows"])
        last = moving["scheduleLast"]
        zero_running = ZERO
        if last is not None:
            held = [a for a in moving["accruals"] if a["day"] == last and a["amount"] <= 0]
            if held:
                zero_running = max(held, key=lambda a: (a["created"], a["sid"]))["running"]

        def rate_of(day):
            if account["platformOverride"] is not None or account["bondsmithOverride"] is not None:
                return (account["grossOverride"] - (account["platformOverride"] or ZERO)
                        - (account["bondsmithOverride"] or ZERO))
            piece = slice_at(slices, day)
            if piece is None:
                raise ProjectionError("no rate on {}".format(day))
            if account["grossOverride"] is not None:
                reduced = account["grossOverride"] - (piece["platform"]["rate"]
                                                      if piece["platform"] else ZERO)
                fee = piece["fee"]
                return reduced - (fee["amount"] if fee and fee["type"] == "INTEREST" else ZERO)
            return reduced_gross(piece)
        try:
            if last is None:
                raise ProjectionError("no schedule")
            projected = project(moving["balance"], last, moving["due"], account["period"],
                                zero_running, rate_of, account["direct"], account["region"],
                                calendar_)
        except ProjectionError as fault:
            skipped = str(fault)
    expected["projectedMaturityValue"] = projected
    expected["projectionSkipped"] = skipped

    # opening and deposit
    expected["openedAt"] = (None if status in ("REQUESTED", "CANCELLED")
                            else moving["firstTransaction"])
    if status == "REQUESTED":
        expected["depositInfo"] = {"minimumDeposit": account["minimum"],
                                   "instructedAmount": account["requested"],
                                   "depositedAmount": moving["cash"],
                                   "requiredAmount": account["minimum"] - moving["cash"]}
    else:
        expected["depositInfo"] = None
    return expected


def _same(api, expected, tolerance=None):
    if isinstance(expected, tuple):
        # CLOSING: the schedule may be ended, or brought forward, never later than the rule says.
        return api is None or expected[1] is None or api <= expected[1]
    if api is None or expected is None:
        return api is None and expected is None
    if isinstance(expected, datetime):
        return abs(api - expected) <= (tolerance or timedelta(seconds=1))
    if isinstance(expected, dict):
        return all(_same(api.get(k), v) for k, v in expected.items())
    return api == expected


def _accepts(api, first, second, tolerance=None):
    if _same(api, first, tolerance) or _same(api, second, tolerance):
        return True
    if (api is not None and first is not None and second is not None
            and not isinstance(first, (tuple, dict, str)) and not isinstance(second, (tuple, dict, str))
            and not isinstance(first, datetime)):
        low, high = sorted([first, second])
        return low <= api <= high
    return False


def _text(value):
    if isinstance(value, tuple):
        return "null or on/before {}".format(value[1])
    if isinstance(value, dict):
        return json.dumps({k: str(v) for k, v in value.items()})
    return "null" if value is None else str(value)


def api_values(body):
    """The account response's derived values in the types the oracle compares."""
    deposit = body.get("depositInfo")
    return {
        "status": body.get("status"), "balance": _money(body.get("balance")),
        "totalAccruedInterestToDate": _money(body.get("totalAccruedInterestToDate")),
        "lastAccrualDate": _day(body.get("lastAccrualDate")),
        "nextInterestRealisationDate": _day(body.get("nextInterestRealisationDate")),
        "maturityDate": _day(body.get("maturityDate")),
        "topUpDeadline": _day(body.get("topUpDeadline")),
        "projectedMaturityValue": _money(body.get("projectedMaturityValue")),
        "openedAt": _instant(body.get("openedAt")),
        "depositInfo": None if deposit is None else {k: _money(v) for k, v in deposit.items()}}


def money_strings(body):
    """(field, text) for every money value an account response prints."""
    found = []
    for field in ("balance", "totalAccruedInterestToDate", "projectedMaturityValue"):
        if body.get(field) is not None:
            found.append((field, body[field]))
    for field, text in (body.get("depositInfo") or {}).items():
        if text is not None:
            found.append(("depositInfo." + field, text))
    return found


class Findings:
    def __init__(self):
        self.rows = []
        self.counts = defaultdict(int)

    def add(self, rule, subject, detail, expected, actual):
        self.counts[rule] += 1
        if self.counts[rule] <= LIMIT:
            self.rows.append({"rule": rule, "subject": subject, "detail": detail,
                              "expected": str(expected), "actual": str(actual)})


FIELD_RULES = {
    "status": "an account reads the status core holds",
    "balance": "an account's balance is the sum of its transactions",
    "totalAccruedInterestToDate": "an account's accrued interest is its unrealised customer "
                                  "accrual at 2 dp",
    "lastAccrualDate": "an account's last accrual date is the value date of its newest "
                       "unrealised accrual",
    "nextInterestRealisationDate": "an account's next realisation date follows its payout "
                                   "period from its last realisation",
    "maturityDate": "a term account matures a term after it opened, on a working day",
    "topUpDeadline": "a top-up deadline is the window after opening, on a working day",
    "projectedMaturityValue": "a term account's projected maturity value is its balance plus "
                              "the interest it accrues to maturity",
    "openedAt": "openedAt is when the account's first deposit completed",
    "depositInfo": "a requested account's depositInfo is the minimum, the instructed and the "
                   "deposited amount",
}


def check_account(client, account, today, calendar_, products, findings, stats):
    """Read the account through the API between two readings of core and hold every field."""
    subject = account["uid"]
    read_static(account)
    path = "/direct/v1/customers/{}/accounts/{}".format(account["customer"], account["uid"])
    body, first, second = None, None, None
    mismatches = []
    for attempt in range(ATTEMPTS):
        before = read_moving(account)
        call = client.call("GET", path)
        after = read_moving(account)
        stats["apiReads"] += 1
        if call.status == TRANSPORT_FAULT:
            stats["transportFaults"] += 1
            return
        if not call.ok or not isinstance(call.body, dict):
            findings.add("an account read answers 200", subject,
                         "GET {} answered {} for a {} {} account".format(
                             path, call.status, account["status"], account["type"]),
                         200, call.status)
            return
        body = call.body
        account["scheduleLast"] = before["scheduleLast"]
        account["lastRealisedText"] = before["lastRealised"]
        first = derive(account, before, today, calendar_)
        second = derive(account, after, clock.today(), calendar_)
        api = api_values(body)
        mismatches = []
        tolerance = timedelta(seconds=max(120, clock.system_seconds(5)))
        for field in FIELD_RULES:
            if field == "projectedMaturityValue" and (first["projectionSkipped"]
                                                      or second["projectionSkipped"]):
                stats["projectionSkipped"] += 1
                continue
            if field in ("maturityDate", "topUpDeadline") and not first["maturityFormulaKnown"]:
                continue
            stats["fieldsChecked"] += 1
            if not _accepts(api[field], first[field], second[field], tolerance):
                mismatches.append(field)
        moved = [f for f in mismatches if not _same_pair(first[f], second[f])]
        matched = [f for f in FIELD_RULES if f not in mismatches and api[f] is not None]
        if not mismatches or not moved or attempt == ATTEMPTS - 1:
            break
        stats["retriedMoving"] += 1
        time.sleep(RETRY_PAUSE)
    stats["accountsChecked"] += 1
    for field in matched:
        stats["matchedNonNull"][field] += 1
    stats["byKind"]["{} {}".format(account["type"], account["status"])] += 1
    for field in mismatches:
        report_field(field, account, body, first, second, api, findings)
    if first["chainBreak"] is not None:
        findings.add("an account's running accrual is the sum of its accrual amounts", subject,
                     "the newest unrealised CUSTOMER accrual row of account {} carries a "
                     "running_accrual of {}, and the CUSTOMER accrual_amount rows add up to {}"
                     .format(subject, first["chainBreak"][0], first["chainBreak"][1]),
                     first["chainBreak"][1], first["chainBreak"][0])
    for field, text in money_strings(body):
        if not MONEY_TEXT.match(str(text)):
            findings.add("a money value prints with two decimals", subject,
                         "account {} prints {} as {!r}".format(subject, field, text),
                         "two decimals", text)
    check_account_contract(account, body, first, findings)
    check_account_product(account, body, products, today, calendar_, findings, stats)


def _same_pair(first, second):
    return _text(first) == _text(second)


def report_field(field, account, body, first, second, api, findings):
    rule = FIELD_RULES[field]
    subject = account["uid"]
    expected = first[field] if _same_pair(first[field], second[field]) else \
        "{} .. {}".format(_text(first[field]), _text(second[field]))
    where = "{} {} account {}".format(account["status"], account["type"], subject)
    detail = "the Direct API prints {} of {} for {}, and core's tables give {}".format(
        field, _text(api[field]), where, expected)
    if field == "projectedMaturityValue":
        detail += (" (principal {} accrued daily at the product's reduced rate from {} to {} "
                   "and realised at maturity: rate_override_source {})".format(
                       first["balance"], account.get("scheduleLast") or "the last realisation",
                       first["maturityDate"], account["overrideSource"]))
    elif field == "openedAt":
        detail += ("; the account row was created at {}, the first transaction booked at {}"
                   .format(account["created"], _text(first["openedAt"])))
        if api["openedAt"] is not None and abs(api["openedAt"] - account["created"]) < timedelta(
                seconds=1):
            detail += ("; the API prints customer_product_account.created_at, the moment the "
                       "account was requested, where the field is documented as the moment a "
                       "deposit completed and the account went live")
    elif field == "nextInterestRealisationDate":
        detail += " ({} payout, last realised {})".format(account["period"],
                                                          account.get("lastRealisedText", ""))
    findings.add(rule, subject, detail, _text(expected), _text(api[field]))


def check_account_contract(account, body, first, findings):
    """The documented contract of fields core could hold correctly and still print wrongly."""
    subject = account["uid"]
    if account["transfer"] == "POOLED":
        if body.get("ukAccountDetail") is not None:
            findings.add("a pooled account has no UK account detail", subject,
                         "the Direct API prints a ukAccountDetail for pooled account {}"
                         .format(subject), "null", json.dumps(body["ukAccountDetail"]))
        if not account["payByReference"]:
            if body.get("paymentReference") is not None:
                findings.add("a pooled account has no payment reference", subject,
                             "paymentReference is documented as null for pooled accounts, and "
                             "pooled account {} on a platform that does not pay by reference "
                             "prints {}".format(subject, body["paymentReference"]), "null",
                             body["paymentReference"])
            if body.get("depositInfo") is not None:
                findings.add("a pooled account has no depositInfo", subject,
                             "depositInfo is documented as always null for pooled customers, and "
                             "pooled account {} on a platform that does not pay by reference "
                             "prints it".format(subject), "null", json.dumps(body["depositInfo"]))
    if account["payByReference"] and account["paymentReference"] != body.get("paymentReference"):
        findings.add("an account's payment reference is the one core holds", subject,
                     "account {} prints the paymentReference {} and core holds {}".format(
                         subject, body.get("paymentReference"), account["paymentReference"]),
                     account["paymentReference"], body.get("paymentReference"))
    if account["minimum"] != account["bankMinimum"] and account["status"] == "REQUESTED":
        findings.add("a requested account's minimum deposit is the platform's override", subject,
                     "the platform product's effective minimum deposit is {} and the bank "
                     "product's is {}, and depositInfo.minimumDeposit on account {} prints {}"
                     .format(account["minimum"], account["bankMinimum"], subject,
                             (body.get("depositInfo") or {}).get("minimumDeposit")),
                     account["minimum"], (body.get("depositInfo") or {}).get("minimumDeposit"))


def check_account_product(account, body, products, today, calendar_, findings, stats):
    """The product the account carries against the product's own read and the rate slices."""
    product = body.get("product") or {}
    subject = account["uid"]
    detail = products.get(account["plpUid"])
    if detail is None:
        return
    embedded = product.get("rateDetails")
    if embedded != detail["body"].get("rateDetails") and detail["stable"]:
        findings.add("an account carries the product's rate details", subject,
                     "account {} carries {} rate details and product {}'s own read shows {}"
                     .format(subject, len(embedded or []), account["plpUid"],
                             len(detail["body"].get("rateDetails") or [])),
                     json.dumps(detail["body"].get("rateDetails"))[:300],
                     json.dumps(embedded)[:300])
    period = product.get("period") or product.get("periodFeature") or {}
    wanted = detail["body"].get("periodFeature") or {}
    for field in ("noticePeriod", "termPeriod", "coolOffPeriod", "topUpWindowDays"):
        if period.get(field) != wanted.get(field):
            findings.add("an account carries the product's period feature", subject,
                         "account {} carries {} of {} and product {}'s own read shows {}"
                         .format(subject, field, period.get(field), account["plpUid"],
                                 wanted.get(field)), wanted.get(field), period.get(field))
    for field in ("name", "productType"):
        if product.get(field) != detail["body"].get(field):
            findings.add("an account carries the product's name and type", subject,
                         "account {} carries {} of {} and product {}'s own read shows {}".format(
                             subject, field, product.get(field), account["plpUid"],
                             detail["body"].get(field)), detail["body"].get(field),
                         product.get(field))


# ---------------------------------------------------------------------------------------------
# customer balances


def customer_balances(customer_uid):
    """{(currency, wrapper): (total, savings, cash)} from core's tables."""
    rows = _rows("""
      SELECT ca.currency, CASE WHEN ca.tax_wrapper = 'ISA' THEN 'ISA' ELSE 'NONE' END,
             coalesce((SELECT sum(t.customer_amount) FROM account_transaction t
                       WHERE t.customer_product_account_sid = cpa.sid), 0),
             coalesce((SELECT sum(ct.transaction_amount) FROM cash_transaction ct
                       JOIN entity_internal_account eia ON eia.sid = ct.entity_account_sid
                       WHERE eia.entity_uid = cpa.uid
                         AND eia.entity_type = 'CUSTOMER_PRODUCT_ACCOUNT'), 0)
      FROM platform_customer pc
      JOIN customer_account ca ON ca.platform_customer_sid = pc.sid
      JOIN customer_product_account cpa ON cpa.customer_account_sid = ca.sid
      WHERE pc.uid = '{}'""".format(_uuid(customer_uid)))
    held = defaultdict(lambda: [ZERO, ZERO])
    for currency, wrapper, savings, cash in rows:
        held[(currency, wrapper)][0] += _d(savings)
        held[(currency, wrapper)][1] += _d(cash)
    return {k: (v[0] + v[1], v[0], v[1]) for k, v in held.items()}


def check_balances(client, account, findings, stats):
    customer = account["customer"]
    path = "/direct/v1/customers/{}/balances".format(customer)
    for attempt in range(ATTEMPTS):
        before = customer_balances(customer)
        call = client.call("GET", path)
        after = customer_balances(customer)
        stats["apiReads"] += 1
        if call.status == TRANSPORT_FAULT:
            stats["transportFaults"] += 1
            return
        if not call.ok or not isinstance(call.body, dict):
            findings.add("a balances read answers 200", customer,
                         "GET {} answered {}".format(path, call.status), 200, call.status)
            return
        shown = {(b.get("currency"), b.get("taxWrapper")):
                 (_money(b.get("totalBalance")), _money(b.get("totalSavingsBalance")),
                  _money(b.get("unallocatedCashBalance")))
                 for b in call.body.get("balances") or []}
        for b in call.body.get("balances") or []:
            for field in ("totalBalance", "totalSavingsBalance", "unallocatedCashBalance"):
                if not MONEY_TEXT.match(str(b.get(field))):
                    findings.add("a money value prints with two decimals", customer,
                                 "customer {} prints balances.{} as {!r}".format(
                                     customer, field, b.get(field)), "two decimals", b.get(field))
        bad = []
        for key in set(shown) | set(before) | set(after):
            api = shown.get(key, (ZERO, ZERO, ZERO))
            low = before.get(key, (ZERO, ZERO, ZERO))
            high = after.get(key, (ZERO, ZERO, ZERO))
            for index, name in enumerate(("totalBalance", "totalSavingsBalance",
                                          "unallocatedCashBalance")):
                stats["fieldsChecked"] += 1
                lo, hi = sorted([low[index], high[index]])
                if not lo <= api[index] <= hi:
                    bad.append((key, name, api[index], low[index], high[index]))
        if not bad or all(b[3] == b[4] for b in bad) or attempt == ATTEMPTS - 1:
            break
        stats["retriedMoving"] += 1
        time.sleep(RETRY_PAUSE)
    stats["balanceReads"] += 1
    for key, name, api, low, high in bad:
        findings.add("a customer's balances are their accounts' balances and cash", customer,
                     "the Direct API prints {} of {} for customer {} in {} {}, and core's tables "
                     "give {}".format(name, api, customer, key[0], key[1],
                                      low if low == high else "{} .. {}".format(low, high)),
                     low if low == high else "{} .. {}".format(low, high), api)


# ---------------------------------------------------------------------------------------------
# products


def read_products(platform_uid):
    """Every platform product of the platform, with what core holds for it."""
    rows = _rows("""
      SELECT plp.sid, plp.uid, plp.alias, plp.access_status, bp.sid, bp.product_type,
             bp.exotic_type, bp.currency, bp.start_date, bp.account_holder_types,
             bp.guarantee_scheme, coalesce(ppc.minimum_deposit_override, bp.deposit_requirement_min),
             coalesce(ppc.maximum_deposit_override, bp.deposit_requirement_max),
             bp.interest_feature_realisation_period, bp.interest_cut_off_time, bptf.term_period,
             bptf.top_up_window_days, bpnf.notice_period, bp.cooloff_period,
             bp.product_availability_from, bp.product_availability_to, bp.tax_wrappers,
             bp.record_version, pb.trading_name, pb.hex_colour, pb.logo_url,
             determine_effective_state(bp.current_state, plp.current_state) = 'ACTIVE',
             bp.product_availability_from <= now(),
             bp.stop_display_at IS NULL OR bp.stop_display_at > now(),
             coalesce(bpmd.maximum_available - bpmd.deposits + bpmd.withdrawals
                      + bpmd.distributions, 0) > 0
      FROM platform_product plp
      JOIN bank_product bp ON bp.sid = plp.product_sid
      JOIN partner_bank pb ON pb.sid = bp.bank_sid
      JOIN partner_platform pp ON pp.sid = plp.platform_sid
      JOIN synced_bank_product_max_deposit bpmd ON bpmd.product_sid = bp.sid
      LEFT JOIN platform_product_configuration ppc ON ppc.platform_product_sid = plp.sid
      LEFT JOIN bank_product_term_feature bptf ON bptf.sid = bp.sid AND bptf.product_type = bp.product_type
      LEFT JOIN bank_product_notice_feature bpnf ON bpnf.sid = bp.sid AND bpnf.product_type = bp.product_type
      WHERE pp.uid = '{}'""".format(_uuid(platform_uid)), timeout=180)
    products = {}
    for row in rows:
        (plp_sid, uid, alias, access, bp_sid, kind, exotic, currency, start, holders, scheme,
         minimum, maximum, period, cutoff, term, window, notice, cooloff, available_from,
         available_to, wrappers, version, bank, colour, logo, active, started, visible,
         allows) = row
        products[uid] = {
            "uid": uid, "plp": int(plp_sid), "bp": int(bp_sid), "name": alias, "access": access,
            "type": kind, "exotic": exotic, "currency": currency, "start": _day(start),
            "holders": _list(holders), "scheme": scheme or None, "minimum": _d(minimum),
            "maximum": _d(maximum), "period": period, "cutoff": cutoff or None,
            "term": _int(term), "window": _int(window), "notice": _int(notice),
            "cooloff": cooloff, "from": _day(available_from), "to": _day(available_to),
            "wrappers": sorted(set(_list(wrappers))), "version": version, "bank": bank,
            "colour": colour or None, "logo": logo or None,
            "listed": access != "INACTIVE" and _flag(active) and _flag(started) and _flag(visible),
            "allows": _flag(allows)}
    return products


def fetch_products(client, findings, stats):
    """{platform product uid: {"body", "stable"}} from the list and the single reads."""
    listed = client.call("GET", "/direct/v1/products", params={"take": 1000})
    if listed.status == TRANSPORT_FAULT:
        raise RuntimeError("the Direct API did not answer: {}".format(listed.body))
    if not listed.ok or not isinstance(listed.body, dict):
        findings.add("the product list answers 200", "products",
                     "GET /direct/v1/products answered {}".format(listed.status), 200,
                     listed.status)
        return {}, {}
    stats["apiReads"] += 1
    by_list = {p.get("productId"): p for p in listed.body.get("content") or []}
    if listed.body.get("totalSize") != len(by_list):
        findings.add("the product list totals its content", "products",
                     "totalSize is {} and the page holds {} products".format(
                         listed.body.get("totalSize"), len(by_list)),
                     len(by_list), listed.body.get("totalSize"))
    return by_list, listed.body


def check_products(client, platform_uid, today, findings, stats):
    """Hold the product list and every product's own read against core. Returns the reads."""
    core = read_products(platform_uid)
    stats["platformProducts"] = len(core)
    listed, _ = fetch_products(client, findings, stats)
    reads = {}
    for uid in sorted(core):
        product = core[uid]
        slices_before = rate_slices(*read_rate_rows(product["bp"], product["plp"]))
        call = client.call("GET", "/direct/v1/products/{}".format(uid))
        stats["apiReads"] += 1
        if call.status == TRANSPORT_FAULT:
            stats["transportFaults"] += 1
            continue
        if product["access"] == "INACTIVE":
            if call.ok:
                findings.add("an inactive product is not readable", uid,
                             "GET /direct/v1/products/{} answered {} for an INACTIVE platform "
                             "product".format(uid, call.status), "404", call.status)
            continue
        if not call.ok or not isinstance(call.body, dict):
            findings.add("a platform product is readable", uid,
                         "GET /direct/v1/products/{} answered {} for a platform product that "
                         "is {}".format(uid, call.status, product["access"]), 200, call.status)
            continue
        reads[uid] = {"body": call.body, "stable": True}
        slices_after = rate_slices(*read_rate_rows(product["bp"], product["plp"]))
        # rows are read after the call, so a rate approved during it shows as a change.
        reads[uid]["stable"] = _slice_key(slices_before) == _slice_key(slices_after)
        check_product(product, call.body, slices_before, slices_after, today, findings, stats,
                      "product read")
        stats["productsChecked"] += 1
        in_list = listed.get(uid)
        if in_list is not None and reads[uid]["stable"] and in_list != call.body:
            keys = sorted(k for k in set(in_list) | set(call.body)
                          if in_list.get(k) != call.body.get(k))
            findings.add("the product list and the single read agree", uid,
                         "GET /direct/v1/products lists product {} with {} different from "
                         "GET /direct/v1/products/{}".format(uid, keys, uid),
                         json.dumps({k: call.body.get(k) for k in keys})[:300],
                         json.dumps({k: in_list.get(k) for k in keys})[:300])
        if product["listed"] and in_list is None:
            findings.add("the product list holds every listable product", uid,
                         "platform product {} ({}) is ACTIVE, available and visible, and "
                         "GET /direct/v1/products does not list it".format(uid, product["name"]),
                         "listed", "missing")
        if not product["listed"] and in_list is not None:
            findings.add("the product list holds only listable products", uid,
                         "platform product {} ({}) is not ACTIVE, available and visible, and "
                         "GET /direct/v1/products lists it".format(uid, product["name"]),
                         "not listed", "listed")
    for uid in listed:
        if uid not in core:
            findings.add("the product list holds only the platform's products", uid,
                         "GET /direct/v1/products lists product {}, which is not a product of "
                         "platform {}".format(uid, platform_uid), "not listed", "listed")
    return reads


def _slice_key(slices):
    return sorted((str(p["from"]), str(p["to"]), str(p["rate"]["gross"]),
                   str(p["platform"] and p["platform"]["rate"]),
                   str(p["fee"] and (p["fee"]["amount"], p["fee"]["type"]))) for p in slices)


def check_product(product, body, slices_before, slices_after, today, findings, stats, source):
    uid = product["uid"]
    sets = [expected_rate_details(slices_before, today, product),
            expected_rate_details(slices_after, clock.today(), product)]
    shown = body.get("rateDetails") or []
    if not any(_rates_agree(shown, expected) for expected in sets):
        report_rates(uid, source, shown, sets, product, findings)
    stats["rateEntries"] += len(shown)
    statement_checked(product, shown, today, stats)
    if _text(sets[0]) == _text(sets[1]):
        order = [(d.get("endDate") is None, d.get("endDate") or "") for d in shown]
        if order != sorted(order):
            findings.add("rate details are sorted by end date, open-ended last", uid,
                         "{} of product {} lists its rate details out of order of endDate"
                         .format(source, uid), "ascending endDate, null last",
                         [d.get("endDate") for d in shown])
    check_static(product, body, findings, source)


def statement_checked(product, shown, today, stats):
    """Count how often the statement oracle's AER agrees with the port used here."""
    if product["type"] == "TERM" or product["period"] not in (
            "END_OF_DAY", "END_OF_MONTH", "START_OF_CALENDAR_MONTH", "END_OF_CALENDAR_MONTH",
            "END_OF_8_MONTH"):
        return
    for entry in shown:
        if entry.get("grossRate") is None:
            continue
        rate = Decimal(str(entry["grossRate"]))
        if statement_aer(rate, product["period"], today) == aer_of(
                rate, product["period"], today, product["type"], product["term"]):
            stats["aerAgreesWithStatementOracle"] += 1
        else:
            stats["aerDisagreesWithStatementOracle"] += 1


def _rates_agree(shown, expected):
    if len(shown) != len(expected):
        return False
    for api, want in zip(sorted(shown, key=_api_key), sorted(expected, key=_exp_key)):
        if _day(api.get("startDate")) != want["startDate"] or _day(api.get("endDate")) != want["endDate"]:
            return False
        for field in ("grossRate", "bonusRate", "leaverRate"):
            if not _decimal_equal(api.get(field), want[field], ZERO):
                return False
        if not _decimal_equal(api.get("aerRate"), want["aerRate"], FOUR):
            return False
        stamp = _instant(api.get("announcedAt"))
        if want["announcedAt"] is None or stamp is None:
            if stamp != want["announcedAt"]:
                return False
        elif abs(stamp - want["announcedAt"]) > timedelta(milliseconds=1):
            return False
    return True


def _decimal_equal(api, want, tolerance):
    if api is None or want is None:
        return api is None and want is None
    return abs(Decimal(str(api)) - want) <= tolerance


def _api_key(entry):
    return (entry.get("startDate") or "", entry.get("endDate") or "9999")


def _exp_key(entry):
    return (str(entry["startDate"]), str(entry["endDate"]) if entry["endDate"] else "9999")


def report_rates(uid, source, shown, sets, product, findings):
    expected = sets[0] if _text(sets[0]) == _text(sets[1]) else sets[1]
    api_days = {(e.get("startDate"), e.get("endDate")): e for e in shown}
    want_days = {(str(e["startDate"]), str(e["endDate"]) if e["endDate"] else None): e
                 for e in expected}
    missing = sorted(k for k in want_days if k not in api_days)
    extra = sorted((k for k in api_days if k not in want_days), key=str)
    if missing or extra:
        findings.add("a product shows the rate slices core's live rows give", uid,
                     "{} of {} product {} shows {} rate details and core's live rate_detail, "
                     "platform_rate_detail and fee_detail rows give {}; missing from the API {}, "
                     "not in core {}".format(source, product["type"], uid, len(shown),
                                             len(expected), missing[:6], extra[:6]),
                     len(expected), len(shown))
    for key in sorted(set(api_days) & set(want_days), key=str)[:50]:
        api, want = api_days[key], want_days[key]
        for field in ("grossRate", "aerRate", "bonusRate", "leaverRate"):
            tolerance = FOUR if field == "aerRate" else ZERO
            if not _decimal_equal(api.get(field), want[field], tolerance):
                actual = api.get(field)
                scaled = (actual is not None and want[field] not in (None, ZERO)
                          and abs(Decimal(str(actual)) - want[field] * 100) <= tolerance)
                rule = ("a rate prints as a fraction, not a percent" if scaled
                        else "a product shows the reduced gross rate and AER of each slice")
                findings.add(rule, uid,
                             "{} of {} product {} prints {} of {} for {}..{}, and core's rows give"
                             " {}{}".format(source, product["type"], uid, field, actual, key[0],
                                            key[1], want[field],
                                            " (the printed value is 100 times core's: a percent "
                                            "where core and the documented examples hold a "
                                            "fraction)" if scaled else ""),
                             want[field], actual)
        stamp = _instant(api.get("announcedAt"))
        if want["announcedAt"] is not None and (stamp is None or abs(stamp - want["announcedAt"])
                                                > timedelta(milliseconds=1)):
            findings.add("a rate slice announces when its newest row was announced", uid,
                         "{} of product {} prints announcedAt {} for {}..{}, and the latest "
                         "announcement among its rows is {}".format(
                             source, uid, api.get("announcedAt"), key[0], key[1],
                             want["announcedAt"]), want["announcedAt"], api.get("announcedAt"))


def check_static(product, body, findings, source):
    uid = product["uid"]

    def hold(rule, field, api, want):
        if api != want:
            findings.add(rule, uid, "{} of {} product {} prints {} of {!r} and core holds {!r}"
                         .format(source, product["type"], uid, field, api, want), want, api)

    hold("a product reads its name", "name", body.get("name"), product["name"])
    hold("a product reads its type", "productType", body.get("productType"), product["type"])
    hold("a product reads its exotic type", "exoticType", body.get("exoticType"),
         product["exotic"])
    hold("a product reads its currency", "currency", body.get("currency"), product["currency"])
    hold("a product reads its start date", "startDate", _day(body.get("startDate")),
         product["start"])
    hold("a product reads its account holder types", "accountHolderTypes",
         body.get("accountHolderTypes"), [h for h in product["holders"] if h == "INDIVIDUAL"])
    hold("a product reads its guarantee scheme", "guaranteeScheme", body.get("guaranteeScheme"),
         product["scheme"])
    deposit = body.get("depositRequirement") or {}
    for field, want in (("minDepositAmount", product["minimum"]),
                        ("maxDepositAmount", product["maximum"])):
        text = deposit.get(field)
        if text is not None and not MONEY_TEXT.match(str(text)):
            findings.add("a money value prints with two decimals", uid,
                         "{} of product {} prints depositRequirement.{} as {!r}".format(
                             source, uid, field, text), "two decimals", text)
        if (_money(text) if text is not None else None) != want:
            findings.add("a product reads the effective deposit limits", uid,
                         "{} of {} product {} prints depositRequirement.{} of {} and the bank "
                         "product (with the platform override first) holds {}".format(
                             source, product["type"], uid, field, text, want), want, text)
    feature = body.get("interestFeature") or {}
    payout = EXTERNAL_PAYOUT.get(product["period"], product["period"])
    hold("a product reads its payout period", "interestFeature.payoutPeriod",
         feature.get("payoutPeriod"), payout)
    hold("a product reads its accrual period", "interestFeature.accrualPeriod",
         feature.get("accrualPeriod"), "DAILY")
    hold("a product reads its interest cut-off time", "interestFeature.cutOffTime",
         feature.get("cutOffTime"), product["cutoff"])
    period = body.get("periodFeature") or {}
    hold("a product reads its notice period in days", "periodFeature.noticePeriod",
         period.get("noticePeriod"), product["notice"])
    hold("a product reads its term in months", "periodFeature.termPeriod",
         period.get("termPeriod"), product["term"])
    hold("a product reads its cool-off period", "periodFeature.coolOffPeriod",
         period.get("coolOffPeriod"), product["cooloff"])
    hold("a product reads its top-up window", "periodFeature.topUpWindowDays",
         period.get("topUpWindowDays"), product["window"])
    availability = body.get("productAvailability") or {}
    hold("a product reads when it is available from", "productAvailability.availableFrom",
         _day(availability.get("availableFrom")), product["from"])
    hold("a product reads when it is available until", "productAvailability.availableUntil",
         _day(availability.get("availableUntil")), product["to"])
    hold("a product reads whether it allows deposits", "allowsDeposits",
         body.get("allowsDeposits"), product["allows"])
    hold("a product reads its tax wrappers", "taxWrappers", sorted(set(body.get("taxWrappers")
                                                                         or [])),
         product["wrappers"])
    hold("a product reads its record version", "recordVersion", body.get("recordVersion"),
         str(product["version"]))
    bank = body.get("bank") or {}
    hold("a product reads its bank", "bank.bankName", bank.get("bankName"), product["bank"])
    hold("a product reads its bank colour", "bank.colorHexCode", bank.get("colorHexCode"),
         product["colour"])
    hold("a product reads its bank logo", "bank.logoUrl", bank.get("logoUrl"), product["logo"])


# ---------------------------------------------------------------------------------------------


def check(client, platform_uid, sample=5):
    """Read a sample of the platform's accounts and all its products and answer (findings, stats)."""
    findings = Findings()
    stats = {"platform": platform_uid, "apiReads": 0, "accountsChecked": 0, "fieldsChecked": 0,
             "retriedMoving": 0, "transportFaults": 0, "projectionSkipped": 0, "productsChecked": 0, "rateEntries": 0,
             "balanceReads": 0, "byKind": defaultdict(int),
             "matchedNonNull": defaultdict(int),
             "aerAgreesWithStatementOracle": 0, "aerDisagreesWithStatementOracle": 0,
             "errors": []}
    today = clock.today()
    try:
        calendar_ = Calendar()
        products = check_products(client, platform_uid, today, findings, stats)
        accounts = sample_accounts(platform_uid, sample)
    except RuntimeError as fault:
        stats["errors"].append(str(fault)[:300])
        return findings.rows, _finish(stats, findings)
    for account in accounts:
        try:
            check_account(client, account, today, calendar_, products, findings, stats)
            check_balances(client, account, findings, stats)
        except RuntimeError as fault:
            stats["errors"].append(str(fault)[:300])
    return findings.rows, _finish(stats, findings)


def _finish(stats, findings):
    stats["byKind"] = dict(stats["byKind"])
    stats["matchedNonNull"] = dict(stats["matchedNonNull"])
    stats["findings"] = sum(findings.counts.values())
    stats["findingsByRule"] = dict(findings.counts)
    return stats


def newest_cohorts():
    paths = sorted(glob.glob("fleet*.cohorts.json"), key=os.path.getmtime)
    return json.load(open(paths[-1])) if paths else []


if __name__ == "__main__":
    import sys

    from explorer import config
    from explorer.client import DirectClient

    settings = config.load("local")
    cohorts = newest_cohorts()
    if not cohorts:
        sys.exit("no fleet*.cohorts.json in the current directory")
    wanted = sys.argv[1] if len(sys.argv) > 1 else None
    cohort = next((c for c in cohorts if c["platformUid"] == wanted),
                  cohorts[int(wanted)] if wanted and wanted.isdigit() else cohorts[0])
    direct = DirectClient(settings["base_url"], settings["auth_token_url"], cohort["clientId"],
                          "local", settings.get("auth_scope"))
    try:
        found, stats = check(direct, cohort["platformUid"],
                             sample=int(sys.argv[2]) if len(sys.argv) > 2 else 5)
    finally:
        direct.close()
    print(json.dumps(stats, indent=1, default=str))
    for f in found:
        print(json.dumps(f, default=str))
