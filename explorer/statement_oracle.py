"""A statement oracle that reads a Direct customer statement's PDF and holds it against core.

The run had read every number a statement prints through the Direct API or core's tables, and never
the statement itself, so SAV-11794 (the gross rate and the AER printed at one hundredth of their
value) went out to every Direct customer with nothing in the run looking. This module asks ops for
a statement over a closed period, takes its text with `pdftotext -layout`, and recomputes what each
part must say from core's own rows:

- Gross rate: the product's reduced gross rate for min(end date, today), as a percent at 2 dp
  (DirectStatementDetailsService, rate read through interest_rates_and_fees_fn). The account's own
  gross_rate_override is not used, because the statement shows the product's rate.
- AER: (1 + r/n)^n - 1 in doubles, 4 dp HALF_DOWN, then as a percent at 2 dp, with n the days in
  the rate date's year for END_OF_DAY (InternalInterestPayoutPeriodType.getAer). TERM products are
  left out, because their AER depends on the term length.
- Lines: account_transaction rows with a value date in the period and a non-zero customer amount,
  less FEES and PLATFORM_FEES (AccountTransactionRepository.listSavingsStatementLines,
  DirectStatementTransactionProvider.mapSavingsAccountTransaction).
- Balance as at the end date: updated_product_account_balance of the newest row on or before it
  (AccountTransactionRepository.getBalancesAtDate). The brought-forward balance is the same read
  for the day before the period.
- Interest paid over period: the sum of the period's INTEREST lines.

Every money and rate value is compared as the text the statement prints.
"""

from __future__ import annotations

import os
import random
import re
import subprocess
import tempfile
import time
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_DOWN, ROUND_HALF_EVEN

from explorer import clock
from explorer.interest_oracle import Schedule, ZERO, _d, _day, _psql, _when

S3 = "http://localhost:4566"
BUCKET = "savings-exchange-customer-statements-local-source"
AWS_ENV = {"AWS_ACCESS_KEY_ID": "secret", "AWS_SECRET_ACCESS_KEY": "secret",
           "AWS_DEFAULT_REGION": "eu-west-2", "AWS_PAGER": ""}
PENNY = Decimal("0.01")
HUNDRED = Decimal(100)
ELIGIBLE = ("OPEN", "CLOSING", "CLOSED")
UNLISTED = ("FEES", "PLATFORM_FEES")
# The words each line starts with, by transaction type (DirectStatementTransactionProvider).
DESCRIPTION = {"INTEREST": "Interest paid", "SAVINGS_DEPOSIT": "Deposit to",
               "SAVINGS_WITHDRAWAL": "Withdrawal from", "ADJUSTMENT": "Correction",
               "MATURITY": "Maturity", "INCOME": "Income from"}
PERIODS = {"END_OF_MONTH": 12.0, "START_OF_CALENDAR_MONTH": 12.0, "END_OF_CALENDAR_MONTH": 12.0,
           "END_OF_8_MONTH": 1.5}
# Real seconds to wait for the document row after ops answered the request.
DOCUMENT_WAIT = 30
MONEY = r"(-?)£(-?[\d,]+\.\d\d)"
DAY = r"(\d\d/\d\d/\d{4})"


def _money(sign, digits):
    value = Decimal(digits.replace(",", ""))
    return -value if sign == "-" else value


def _printed_day(text):
    return datetime.strptime(text, "%d/%m/%Y").date()


def percent(fraction):
    """A fraction as the statement must print it: a percent at 2 dp, HALF_EVEN (DecimalFormat)."""
    return (fraction * HUNDRED).quantize(PENNY, rounding=ROUND_HALF_EVEN)


def aer(rate, period, rate_date):
    """The AER as a fraction at 4 dp, or None when the payout period is not one this mirrors."""
    if period == "END_OF_DAY":
        periods = 366.0 if _leap(rate_date.year) else 365.0
    elif period in PERIODS:
        periods = PERIODS[period]
    else:
        return None
    gross = float(rate) * 100
    result = (1 + gross / (periods * 100)) ** periods - 1
    return Decimal(repr(result)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_DOWN)


def _leap(year):
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def eligible_accounts(platform_uid, start, end, count):
    """A sample of the platform's statement-eligible accounts with lines in [start, end]."""
    rows, error = _psql("""
      SELECT cpa.uid, pc.uid, cpa.sid, dca.account_reference, dca.status, bp.product_type,
             bp.interest_feature_realisation_period, plp.sid, bp.sid
      FROM customer_product_account cpa
      JOIN direct_customer_account dca ON dca.customer_product_account_sid = cpa.sid
      JOIN customer_account ca ON ca.sid = cpa.customer_account_sid
      JOIN platform_customer pc ON pc.sid = ca.platform_customer_sid
      JOIN platform_product plp ON plp.sid = cpa.platform_product_sid
      JOIN partner_platform pp ON pp.sid = plp.platform_sid
      JOIN bank_product bp ON bp.sid = plp.product_sid
      WHERE pp.uid = '{platform}' AND dca.status IN ('OPEN', 'CLOSING', 'CLOSED')
        AND EXISTS (SELECT 1 FROM account_transaction t
                    WHERE t.customer_product_account_sid = cpa.sid
                      AND t.value_date BETWEEN '{start}' AND '{end}' AND t.customer_amount <> 0)
      """.format(platform=platform_uid, start=start, end=end), timeout=120)
    if rows is None:
        return None, error
    keys = ("account", "customer", "sid", "reference", "status", "type", "period", "pp", "bp")
    accounts = [dict(zip(keys, row)) for row in rows]
    # One of each product type first, so a sample of two is not all INSTANT.
    random.shuffle(accounts)
    by_type = {}
    for account in accounts:
        by_type.setdefault(account["type"], account)
    picked = list(by_type.values())[:count]
    picked += [a for a in accounts if a not in picked][:count - len(picked)]
    return picked, None


def ledger(account, start, end):
    """What core holds for the period: (lines, opening, closing), or (None, error, None)."""
    lines, e1 = _psql("""
      SELECT sid, transaction_type, customer_amount, value_date, created_at
      FROM account_transaction
      WHERE customer_product_account_sid = {sid} AND value_date BETWEEN '{start}' AND '{end}'
        AND customer_amount <> 0 AND transaction_type NOT IN ('FEES', 'PLATFORM_FEES')
      ORDER BY value_date DESC, created_at DESC, sid DESC
      """.format(sid=int(account["sid"]), start=start, end=end))
    balances, e2 = _psql("""
      SELECT (SELECT updated_product_account_balance FROM account_transaction
              WHERE customer_product_account_sid = {sid} AND value_date < '{start}'
              ORDER BY value_date DESC, created_at DESC, sid DESC LIMIT 1),
             (SELECT updated_product_account_balance FROM account_transaction
              WHERE customer_product_account_sid = {sid} AND value_date <= '{end}'
              ORDER BY value_date DESC, created_at DESC, sid DESC LIMIT 1)
      """.format(sid=int(account["sid"]), start=start, end=end))
    if lines is None or balances is None:
        return None, e1 or e2, None
    rows = [{"sid": r[0], "type": r[1], "amount": _d(r[2]), "day": _day(r[3]),
             "created": r[4]} for r in lines]
    opening, closing = balances[0] if balances else ("", "")
    return rows, _d(opening) or ZERO, _d(closing) or ZERO


def product_rate(account, rate_date):
    """The product's reduced gross rate for the date as core holds it now, or None, or an error."""
    rates, e1 = _psql("SELECT gross_rate, start_date, end_date, created_at FROM rate_detail "
                      "WHERE bank_product_sid = {}".format(int(account["bp"])))
    fees, e2 = _psql("SELECT rate, start_date, end_date, created_at FROM platform_rate_detail "
                     "WHERE platform_product_sid = {}".format(int(account["pp"])))
    bfees, e3 = _psql("SELECT amount, start_date, end_date, created_at, type FROM fee_detail "
                      "WHERE platform_product_sid = {}".format(int(account["pp"])))
    if rates is None or fees is None or bfees is None:
        raise RuntimeError(e1 or e2 or e3)
    now = clock.now()
    gross = Schedule([(_d(v), _day(s), _day(e), _when(c)) for v, s, e, c in rates]).at(now, rate_date)
    if gross is None:
        return None
    fee = Schedule([(_d(v), _day(s), _day(e), _when(c)) for v, s, e, c in fees]).at(now, rate_date)
    bondsmith = Schedule([((_d(v), k), _day(s), _day(e), _when(c))
                          for v, s, e, c, k in bfees]).at(now, rate_date)
    bondsmith_rate, kind = bondsmith if bondsmith else (ZERO, None)
    return gross - (fee or ZERO) - (bondsmith_rate if kind == "INTEREST" else ZERO)


def request(ops, account, start, end):
    """Ask ops for the statement and answer (document key, error).

    The request renders and uploads the PDF before ops answers, and a second statement for the same
    account and end date replaces the first under the same key, so the newest row is this one.
    """
    call = ops.call("POST", "/operations/statement/customer/{}/documents".format(account["customer"]),
                    json_body={"documentType": "MONTHLY_STATEMENT", "from": start.isoformat(),
                               "to": end.isoformat(), "productAccountUid": account["account"],
                               "includeHoldingAccountDetails": False})
    if not getattr(call, "ok", False):
        return None, "ops answered {} to the statement request".format(getattr(call, "status", "?"))
    deadline = time.time() + DOCUMENT_WAIT
    while time.time() < deadline:
        rows, error = _psql("""
          SELECT d.document_key FROM customer_document d
          JOIN platform_customer pc ON pc.sid = d.customer_sid
          WHERE pc.uid = '{customer}' AND d.document_date = '{end}' AND d.generated
            AND d.document_name LIKE '%{reference}.pdf'
          ORDER BY d.created_at DESC LIMIT 1""".format(
            customer=account["customer"], end=end, reference=account["reference"]))
        if rows:
            return rows[0][0], None
        time.sleep(2)
    return None, ""


def text_of(key):
    """The statement's text, or raise when it cannot be fetched or read."""
    env = dict(os.environ)
    env.update(AWS_ENV)
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "statement.pdf")
        got = subprocess.run(["aws", "--endpoint-url", S3, "s3", "cp",
                              "s3://{}/{}".format(BUCKET, key), path, "--quiet"],
                             capture_output=True, text=True, timeout=60, env=env)
        if got.returncode != 0:
            raise RuntimeError("S3 refused {}: {}".format(key, got.stderr.strip()[:200]))
        done = subprocess.run(["pdftotext", "-layout", path, "-"], capture_output=True,
                              text=True, timeout=60)
        if done.returncode != 0:
            raise RuntimeError("pdftotext failed: {}".format(done.stderr.strip()[:200]))
        return done.stdout


def parse(text):
    """The statement's printed values. Missing parts come back as None."""
    found = {}
    period = re.search(r"Reporting period\s+{} - {}".format(DAY, DAY), text)
    found["period"] = (_printed_day(period.group(1)), _printed_day(period.group(2))) if period else None
    found["rateShown"] = re.search(r"Interest rate as at ", text) is not None
    gross = re.search(r"^\s*Gross rate\s+(-?[\d,]+\.\d\d)%", text, re.M)
    found["gross"] = Decimal(gross.group(1).replace(",", "")) if gross else None
    rate = re.search(r"^\s*AER\s+(-?[\d,]+\.\d\d)%", text, re.M)
    found["aer"] = Decimal(rate.group(1).replace(",", "")) if rate else None
    paid = re.search(r"Interest paid over period\s+" + MONEY, text)
    found["interest"] = _money(*paid.groups()) if paid else None
    closing = re.search(r"Balance as at \d{1,2} \w+ \d{4}\s+" + MONEY, text)
    found["closing"] = _money(*closing.groups()) if closing else None
    found["rows"], found["opening"] = _rows(text.splitlines())
    return found


def _rows(lines):
    """The transaction table's rows, newest first, and the brought-forward balance."""
    columns = None
    rows, opening, block = [], None, []
    in_table = False
    # The renderer can break a row over a page: the first line of its description ends page one
    # and the dated line opens page two. That first line is held and put in front of the next row.
    carried = ""

    def close(block):
        nonlocal opening, carried
        if not block:
            return
        text = " ".join(" ".join(line.split()) for line in block)
        dated = next((line for line in block if re.match(r"\s*" + DAY + r"\s", line)), None)
        if dated is None and "Balance brought forward" not in text:
            carried = (carried + " " + text).strip()
            return
        text = (carried + " " + text).strip()
        carried = ""
        if "Balance brought forward" in text:
            amounts = list(re.finditer(MONEY, text))
            opening = _money(*amounts[-1].groups()) if amounts else None
            return
        if dated is None or columns is None:
            return
        amounts = [(m.start(), _money(*m.groups())) for m in re.finditer(MONEY, dated)]
        if not amounts:
            return
        out = money_in = None
        balance = None
        for start, value in amounts:
            nearest = min(columns, key=lambda c: abs(columns[c] - start))
            if nearest == "out":
                out = value
            elif nearest == "in":
                money_in = value
            else:
                balance = value
        description = re.sub(MONEY, "", re.sub(DAY, "", text, count=1)).strip()
        rows.append({"day": _printed_day(re.match(r"\s*" + DAY, dated).group(1)),
                     "out": out, "in": money_in, "balance": balance,
                     "description": " ".join(description.split())})

    for line in lines:
        header = re.search(r"MONEY OUT\s+MONEY IN\s+BALANCE", line, re.I)
        if header:
            close(block)
            block = []
            upper = line.upper()
            columns = {"out": upper.index("MONEY OUT"), "in": upper.index("MONEY IN"),
                       "balance": upper.index("BALANCE", upper.index("MONEY IN"))}
            in_table = True
            continue
        if not in_table:
            continue
        if re.search(r"Page \d+ of \d+", line) or "Statement Information" in line:
            close(block)
            block = []
            if "Statement Information" in line:
                in_table = False
            continue
        if not line.strip():
            close(block)
            block = []
            if opening is not None:
                break
            continue
        block.append(line)
    close(block)
    return rows, opening


def judge(account, start, end, printed, lines, opening, closing, rate, key):
    """[(rule, detail, expected, actual)] for every part of the statement that is wrong."""
    wrong = []
    where = "statement {} for {} to {}".format(key, start, end)
    if printed["period"] != (start, end):
        wrong.append(("a statement covers the period asked for",
                      "{} prints the period {}".format(where, printed["period"]),
                      (start, end), printed["period"]))
    rate_date = min(end, clock.today())
    if rate is None:
        if printed["rateShown"]:
            wrong.append(("a statement shows a rate only when the product has one",
                          "{} shows a rate section, and no rate covers {}".format(where, rate_date),
                          "no rate section", printed["gross"]))
    else:
        expected = percent(rate)
        if printed["gross"] is None or printed["gross"].compare(expected) != 0:
            wrong.append(("a statement shows the product's gross rate",
                          "{} prints a gross rate of {}%, and the product's reduced gross rate "
                          "on {} is {} ({}%)".format(where, printed["gross"], rate_date, rate,
                                                     expected),
                          "{}%".format(expected), "{}%".format(printed["gross"])))
        wanted = aer(rate, account["period"], rate_date) if account["type"] != "TERM" else None
        if wanted is not None:
            expected = percent(wanted)
            if printed["aer"] is None or abs(printed["aer"] - expected) > PENNY:
                wrong.append(("a statement shows the AER of the product's rate",
                              "{} prints an AER of {}%, and {} paid {} gives an AER of {} ({}%)"
                              .format(where, printed["aer"], rate, account["period"], wanted,
                                      expected),
                              "{}%".format(expected), "{}%".format(printed["aer"])))
    interest = sum((l["amount"] for l in lines if l["type"] == "INTEREST"), ZERO)
    if printed["interest"] is None or printed["interest"].compare(interest) != 0:
        wrong.append(("a statement's interest paid is the interest booked in its period",
                      "{} prints interest paid of {}, and the period's INTEREST rows add up to {}"
                      .format(where, printed["interest"], interest), interest, printed["interest"]))
    if printed["closing"] is None or printed["closing"].compare(closing) != 0:
        wrong.append(("a statement's closing balance is the account's balance at its end date",
                      "{} prints a balance of {}, and the account's balance at {} is {}".format(
                          where, printed["closing"], end, closing), closing, printed["closing"]))
    if printed["opening"] is None or printed["opening"].compare(opening) != 0:
        wrong.append(("a statement's brought-forward balance is the balance before its period",
                      "{} brings forward {}, and the account's balance before {} is {}".format(
                          where, printed["opening"], start, opening), opening, printed["opening"]))
    wanted = sorted((l["day"], "in" if l["amount"] > 0 else "out", abs(l["amount"]))
                    for l in lines)
    shown = sorted((r["day"], "in" if r["in"] is not None else "out",
                    r["in"] if r["in"] is not None else r["out"]) for r in printed["rows"])
    if wanted != shown:
        missing = [w for w in wanted if w not in shown][:5]
        extra = [s for s in shown if s not in wanted][:5]
        wrong.append(("a statement lists each transaction of its period once",
                      "{} lists {} lines and core holds {}; missing {}, not in core {}".format(
                          where, len(shown), len(wanted), missing, extra),
                      len(wanted), len(shown)))
    else:
        # The lines match as a set, so they can be paired in printed order to check each one's
        # description and running balance.
        balance = closing
        for row, line in zip(printed["rows"], lines):
            prefix = DESCRIPTION.get(line["type"])
            if prefix and not row["description"].startswith(prefix):
                wrong.append(("a statement line describes its transaction",
                              "{} describes a {} of {} on {} as '{}'".format(
                                  where, line["type"], line["amount"], line["day"],
                                  row["description"]), prefix, row["description"]))
            if row["day"] == line["day"] and row["balance"] is not None \
                    and row["balance"].compare(balance) != 0:
                wrong.append(("a statement's running balance follows its lines",
                              "{} prints a balance of {} after the line of {} on {}, and the "
                              "closing balance less the later lines is {}".format(
                                  where, row["balance"], line["amount"], line["day"], balance),
                              balance, row["balance"]))
                break
            balance -= line["amount"]
    return wrong


def check(ops, platform_uid, count=2, period_days=14):
    """Statement a sample of the platform's accounts and answer (findings, stats)."""
    end = clock.today() - timedelta(days=1)
    # A document's key holds only its end date, so a statement asked for here that ends on a month
    # end would replace the customer's scheduled monthly statement (finding 39).
    if (end + timedelta(days=1)).day == 1:
        end -= timedelta(days=1)
    start = end - timedelta(days=period_days - 1)
    stats = {"asked": 0, "judged": 0, "skippedMoving": 0, "findings": 0}
    accounts, error = eligible_accounts(platform_uid, start, end, count)
    if accounts is None:
        return [], dict(stats, error=error)
    findings = []
    for account in accounts:
        stats["asked"] += 1
        try:
            before = ledger(account, start, end)
            rate_before = product_rate(account, min(end, clock.today()))
        except RuntimeError as fault:
            stats.setdefault("errors", []).append(str(fault)[:200])
            continue
        key, error = request(ops, account, start, end)
        if key is None:
            if error:
                stats.setdefault("errors", []).append(error)
                continue
            findings.append({"rule": "a statement is produced for an eligible account",
                             "subject": account["account"],
                             "detail": "ops accepted a statement request for the {} account {} "
                                       "for {} to {}, and no generated document appeared in {} "
                                       "seconds".format(account["status"], account["account"],
                                                        start, end, DOCUMENT_WAIT),
                             "expected": "a document", "actual": "none"})
            continue
        try:
            after = ledger(account, start, end)
            rate_after = product_rate(account, min(end, clock.today()))
        except RuntimeError as fault:
            stats.setdefault("errors", []).append(str(fault)[:200])
            continue
        # A row booked into the period, or a rate approved, while the statement rendered leaves
        # no single truth to hold it against.
        if before != after or rate_before != rate_after or after[0] is None:
            stats["skippedMoving"] += 1
            continue
        try:
            printed = parse(text_of(key))
        except (RuntimeError, OSError, subprocess.SubprocessError) as fault:
            stats.setdefault("errors", []).append(str(fault)[:200])
            continue
        stats["judged"] += 1
        lines, opening, closing = after
        for rule, detail, expected, actual in judge(account, start, end, printed, lines, opening,
                                                    closing, rate_after, key):
            findings.append({"rule": rule, "subject": account["account"], "detail": detail,
                             "expected": str(expected), "actual": str(actual)})
    stats["findings"] = len(findings)
    return findings, stats


if __name__ == "__main__":
    import json
    import sys

    from explorer import config, world
    from explorer.client import BearerClient

    settings = config.load("local")
    found, stats = check(BearerClient(settings["ops_base_url"], world.ops_token(settings)),
                         sys.argv[1], count=int(sys.argv[2]) if len(sys.argv) > 2 else 2)
    print(json.dumps(stats, indent=1, default=str))
    for f in found:
        print(json.dumps(f, default=str))
