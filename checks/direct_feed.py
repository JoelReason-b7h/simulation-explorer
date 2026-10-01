"""Check every Direct data feed and RECON file a bank has on the local stack.

    python3 checks/direct_feed.py <bankUid> [--generate N] [--json out.json]

--generate asks core for the feed and then the RECON N times first, because RECON moves one business
date per run and the feed holds until the RECON before it is sealed.

Each value the files report is checked against a second way to get it: a trailer against the rows,
a STAT row against its column, the hash against the bytes, a RECON total against the files it
summarises, an UpdatedBalance against the one before it plus the amount, an ACCOUNT balance
against the TRANSACTION file, and every reference against the file that should introduce it.
Exit status 1 means at least one check failed; the findings name the file and the row.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

S3 = "http://localhost:4566"
BUCKET = "upload"
CACHE = ROOT / "feeds"
AWS_ENV = {"AWS_ACCESS_KEY_ID": "secret", "AWS_SECRET_ACCESS_KEY": "secret",
           "AWS_DEFAULT_REGION": "eu-west-2", "AWS_PAGER": ""}
DELTA = ("CUSTOMER", "ACCOUNT", "PRODUCT", "TRANSACTION")
STAT_SUMS = {"ACCOUNT": ("AccountBalanceTotal", "AccountBalance"),
             "TRANSACTION": ("AmountTotal", "Amount")}
STAT_DISTINCT = {"CUSTOMER": ("DistinctCustomerIdCount", "CustomerId"),
                 "PRODUCT": ("DistinctProductIdCount", "ProductId")}
PENNY = Decimal("0.01")
EXTRACT_GAP = Decimal(60)


def aws(*args, timeout=120):
    env = dict(os.environ)
    env.update(AWS_ENV)
    return subprocess.run(["aws", "--endpoint-url", S3, *args], capture_output=True, text=True,
                          timeout=timeout, env=env)


def fetch(bank_uid, family="direct"):
    """Copy the bank's files into feeds/<family>/<bankUid>/ and return their paths, oldest first."""
    target = CACHE / family / bank_uid
    target.mkdir(parents=True, exist_ok=True)
    # A move, not a copy. LocalStack keeps every S3 object in memory, and eleven hours of feed and
    # MI files took it past its 640m limit: the kernel killed it and every SQS consumer lost its
    # queues. The files already fetched stay in the local cache, so the checks still see them.
    done = aws("s3", "mv", "s3://{}/archive/{}/{}/".format(BUCKET, family, bank_uid),
               str(target), "--recursive", "--quiet")
    if done.returncode != 0:
        raise SystemExit("could not read s3://{}/archive/{}/{}/: {}".format(
            BUCKET, family, bank_uid, done.stderr.strip()[:300]))
    # One feed run writes its files under one timestamp, and a file may name only what an earlier
    # file of the same run introduced, so ties go in the order the entities depend on each other.
    rank = {"PRODUCT": 0, "CUSTOMER": 1, "ACCOUNT": 2, "TRANSACTION": 3, "RECON": 4}
    return sorted(target.glob("*.csv"), key=lambda p: (p.name.split("_", 1)[-1],
                                                       rank.get(p.name.split("_", 1)[0], 9)))


class File:
    def __init__(self, path):
        self.path = path
        self.name = path.name
        self.raw = path.read_bytes()
        records = list(csv.reader(io.StringIO(self.raw.decode("utf-8"), newline="")))
        self.hdr = records[0]
        self.trl = records[-1]
        self.header = records[1] if len(records) > 2 else []
        body = records[2:-1]
        self.rows = [dict(zip(self.header, r)) for r in body if r and r[0] != "STAT"]
        self.stats = {r[1]: r[2] for r in body if r and r[0] == "STAT"}
        self.entity = self.hdr[1] if len(self.hdr) > 1 else "?"
        self.business_date = self.hdr[4] if len(self.hdr) > 4 else "?"
        self.extract_type = self.hdr[7] if len(self.hdr) > 7 else "?"


class Findings:
    def __init__(self):
        self.items = []

    def add(self, rule, file, detail, row=None):
        self.items.append({"rule": rule, "file": file, "detail": detail, "row": row})


def money(value):
    return Decimal(value or "0")


def day(value):
    """The date part of a feed date. ValueDate is sent as 2026-10-05T00:00:00Z and a business date
    as 2026-10-05, so compared as strings a transaction sorted after its own day's ACCOUNT file."""
    return (value or "")[:10]


def check_envelope(f, out):
    if not f.raw.endswith(b"\r\n"):
        out.add("every line ends in CRLF, the last one too", f.name, "the file ends without CRLF")
    lines = f.raw.split(b"\r\n")
    if any(b"\n" in line for line in lines[:-1]):
        out.add("every line ends in CRLF, the last one too", f.name, "a bare LF appears")
    if f.hdr[0] != "HDR" or f.trl[0] != "TRL":
        out.add("the file opens with HDR and closes with TRL", f.name,
                "first {} last {}".format(f.hdr[:1], f.trl[:1]))
        return
    if f.hdr[2] != f.name or f.trl[2] != f.name:
        out.add("HDR and TRL name the file they are in", f.name,
                "HDR {} TRL {}".format(f.hdr[2], f.trl[2]))
    if f.trl[1] != f.entity:
        out.add("HDR and TRL name the same entity", f.name, "{} vs {}".format(f.entity, f.trl[1]))
    body = f.raw.rstrip(b"\r\n")
    hashed = body[:body.rfind(b",") + 1]
    if hashlib.sha256(hashed).hexdigest() != f.trl[-1]:
        out.add("ControlTotal1 is the SHA-256 of every byte before it", f.name,
                "TRL says {}, the bytes hash to {}".format(
                    f.trl[-1], hashlib.sha256(hashed).hexdigest()))
    count = int(f.trl[3])
    if count != len(f.rows):
        out.add("DataRecordCount counts the DTL rows", f.name,
                "TRL says {}, the file has {}".format(count, len(f.rows)))
    if f.entity in DELTA:
        inserts = sum(1 for r in f.rows if r.get("ChangeType") == "INSERT")
        updates = sum(1 for r in f.rows if r.get("ChangeType") == "UPDATE")
        others = [r for r in f.rows if r.get("ChangeType") not in ("INSERT", "UPDATE")]
        if (int(f.trl[4]), int(f.trl[5])) != (inserts, updates):
            out.add("InsertRecordCount and UpdateRecordCount count the rows by ChangeType",
                    f.name, "TRL says {}+{}, rows are {}+{}".format(
                        f.trl[4], f.trl[5], inserts, updates))
        if count != int(f.trl[4]) + int(f.trl[5]):
            out.add("DataRecordCount is InsertRecordCount plus UpdateRecordCount", f.name,
                    "{} != {} + {}".format(count, f.trl[4], f.trl[5]))
        for r in others[:5]:
            out.add("ChangeType is INSERT or UPDATE", f.name, r.get("ChangeType"), r)
        if f.extract_type != "DELTA":
            out.add("a feed file is a DELTA extract", f.name, f.extract_type)
    if f.entity in STAT_SUMS:
        stat, column = STAT_SUMS[f.entity]
        total = sum((money(r.get(column)) for r in f.rows), Decimal(0)).quantize(
            PENNY, ROUND_HALF_UP)
        if stat not in f.stats:
            out.add("the STAT row is present", f.name, stat)
        elif money(f.stats[stat]) != total:
            out.add("{} is the sum of {}".format(stat, column), f.name,
                    "STAT says {}, the rows sum to {}".format(f.stats[stat], total))
    if f.entity in STAT_DISTINCT:
        stat, column = STAT_DISTINCT[f.entity]
        distinct = len({r.get(column) for r in f.rows})
        if stat not in f.stats:
            out.add("the STAT row is present", f.name, stat)
        elif int(f.stats[stat]) != distinct:
            out.add("{} counts the distinct {}".format(stat, column), f.name,
                    "STAT says {}, the rows hold {}".format(f.stats[stat], distinct))
    if f.entity == "RECON" and (f.stats or f.extract_type != "FULL"):
        out.add("RECON is a FULL extract with no STAT rows", f.name,
                "{} with {} STAT rows".format(f.extract_type, len(f.stats)))


def check_history(files, out):
    """The checks that need every file the bank has sent, in order."""
    seen = {e: set() for e in DELTA}
    ids = {"CUSTOMER": "CustomerId", "ACCOUNT": "AccountId", "PRODUCT": "ProductId",
           "TRANSACTION": "TransactionId"}
    # What each file introduced, by business date, for the RECON totals.
    delivered = {e: defaultdict(set) for e in DELTA}
    last_balance = {}
    latest_shipped = {}
    chains = defaultdict(list)
    balance_by_date = defaultdict(dict)
    daily_transactions = defaultdict(int)
    sign_by_type = defaultdict(set)
    accrued_by_date = defaultdict(dict)
    customers_of_account = {}
    recons = []
    for f in files:
        if f.entity == "RECON":
            recons.append(f)
            continue
        if f.entity not in DELTA:
            continue
        key = ids[f.entity]
        for r in f.rows:
            ident = r.get(key)
            first = ident not in seen[f.entity]
            # A TRANSACTION row is a new fact every time; only the other three are re-sent.
            if f.entity != "TRANSACTION":
                if first and r.get("ChangeType") != "INSERT":
                    out.add("an entity's first appearance is an INSERT", f.name,
                            "{} {} first appears as {}".format(f.entity, ident,
                                                                r.get("ChangeType")), r)
                if not first and r.get("ChangeType") != "UPDATE":
                    out.add("a later appearance is an UPDATE", f.name,
                            "{} {} appears again as {}".format(f.entity, ident,
                                                                r.get("ChangeType")), r)
            elif not first:
                out.add("a transaction is sent once", f.name, "TransactionId {}".format(ident), r)
            seen[f.entity].add(ident)
            delivered[f.entity][day(f.business_date)].add(ident)
        if f.entity == "ACCOUNT":
            for r in f.rows:
                customers_of_account[r["AccountId"]] = r.get("CustomerId")
                if r.get("CustomerId") not in seen["CUSTOMER"]:
                    out.add("an account's customer was sent first", f.name,
                            "account {} names customer {}".format(r["AccountId"],
                                                                  r.get("CustomerId")), r)
                if r.get("ProductId") not in seen["PRODUCT"]:
                    out.add("an account's product was sent first", f.name,
                            "account {} names product {}".format(r["AccountId"],
                                                                 r.get("ProductId")), r)
                accrued_by_date[day(f.business_date)][r["AccountId"]] = money(
                    r.get("AccruedInterestAmount"))
                balance_by_date[day(f.business_date)][r["AccountId"]] = money(
                    r.get("AccountBalance"))
        if f.entity == "TRANSACTION":
            for r in f.rows:
                account = r.get("AccountId")
                if account not in seen["ACCOUNT"]:
                    out.add("a transaction's account was sent first", f.name,
                            "transaction {} names account {}".format(r.get("TransactionId"),
                                                                     account), r)
                daily_transactions[day(r.get("ValueDate"))] += 1
                # A file is for one business date, so a row value-dated before an earlier
                # file's date arrived late: the account's history as sent had a gap until now.
                if day(r.get("ValueDate")) < latest_shipped.get(account, ""):
                    out.add("a transaction arrives no later than the file for its value date",
                            f.name, "account {}: {} value-dated {} after {} was shipped".format(
                                account, r.get("TransactionId"), day(r.get("ValueDate")),
                                latest_shipped[account]), r)
                latest_shipped[account] = max(latest_shipped.get(account, ""),
                                              day(r.get("ValueDate")))
                chains[account].append((day(r.get("ValueDate")), r.get("BookingDateTime", ""),
                                        r, f.name))
    # The balance chain is read in value-date order across every file, because a row that arrives
    # late still took its place in the account's history when it was booked.
    for account, rows in chains.items():
        for _, _, r, name in sorted(rows, key=lambda e: (e[0], e[1])):
            amount, updated = money(r.get("Amount")), money(r.get("UpdatedBalance"))
            if account in last_balance:
                step = updated - last_balance[account]
                if abs(step) != abs(amount):
                    out.add("UpdatedBalance is the previous UpdatedBalance plus the amount",
                            name, "account {}: {} -> {} for amount {} ({})".format(
                                account, last_balance[account], updated, amount,
                                r.get("TransactionType")), r)
                elif amount != 0:
                    sign_by_type[r.get("TransactionType")].add(
                        "same" if step == amount else "opposite")
            last_balance[account] = updated
    for kind, signs in sign_by_type.items():
        if len(signs) > 1:
            out.add("each TransactionType moves the balance one way", "all TRANSACTION files",
                    "{} moved the balance with and against its amount".format(kind))
    check_recons(recons, delivered, daily_transactions, accrued_by_date, files, out)


def cumulative(by_date, upto):
    found = set()
    for day, ids in by_date.items():
        if day <= upto:
            found |= ids
    return found


def check_recons(recons, delivered, daily_transactions, accrued_by_date, files, out):
    previous = None
    # Sorted across files, not within each: a late INTEREST row sent after an account closed
    # otherwise overwrites the closing withdrawal's 0.00 and fails every RECON from then on.
    transactions = sorted((t for g in files if g.entity == "TRANSACTION" for t in g.rows),
                          key=lambda t: (day(t.get("ValueDate")), t.get("BookingDateTime", "")))
    for f in recons:
        if not f.rows:
            out.add("RECON carries one row", f.name, "no rows")
            continue
        r = f.rows[0]
        recon_day = day(r.get("BusinessEffectiveDate"))
        if recon_day != day(f.business_date):
            out.add("the RECON row is for the HDR's business date", f.name,
                    "row {} HDR {}".format(recon_day, f.business_date), r)
        if previous and recon_day <= previous:
            out.add("RECON moves forward one business date at a time", f.name,
                    "{} follows {}".format(recon_day, previous), r)
        previous = recon_day
        expected = {
            "TotalCustomerCount": len(cumulative(delivered["CUSTOMER"], recon_day)),
            "TotalAccountCount": len(cumulative(delivered["ACCOUNT"], recon_day)),
            "TotalProductCount": len(cumulative(delivered["PRODUCT"], recon_day)),
            "DailyTransactionCount": daily_transactions.get(recon_day, 0),
        }
        for column, want in expected.items():
            if int(r.get(column) or 0) != want:
                out.add("{} agrees with the feed files up to its date".format(column), f.name,
                        "RECON says {}, the files give {}".format(r.get(column), want), r)
        balances, accrued = {}, {}
        for d in sorted(accrued_by_date):
            if d <= recon_day:
                accrued.update(accrued_by_date[d])
        want_accrued = sum(accrued.values(), Decimal(0)).quantize(PENNY)
        if money(r.get("TotalAccruedInterest")) != want_accrued:
            out.add("TotalAccruedInterest is the sum of the latest AccruedInterestAmount",
                    f.name, "RECON says {}, the ACCOUNT files give {}".format(
                        r.get("TotalAccruedInterest"), want_accrued), r)
        latest = {}
        for t in transactions:
            if day(t.get("ValueDate")) > recon_day:
                break
            latest[t["AccountId"]] = money(t.get("UpdatedBalance"))
        balances = sum(latest.values(), Decimal(0)).quantize(PENNY)
        if money(r.get("TotalBalanceAtEndOfDay")) != balances:
            out.add("TotalBalanceAtEndOfDay is the sum of each account's latest UpdatedBalance",
                    f.name, "RECON says {}, the TRANSACTION files give {}".format(
                        r.get("TotalBalanceAtEndOfDay"), balances), r)


def check_account_against_transactions(files, out):
    """An ACCOUNT row's balance is the latest UpdatedBalance, by value date up to its business date,
    among the transactions sent so far. The product picks the latest by value date from every row
    the platform holds, and a run's TRANSACTION file reaches the platform with its ACCOUNT file."""
    sent = defaultdict(list)
    for stamp in sorted({f.name.split("_", 1)[-1] for f in files}):
        run = [f for f in files if f.name.split("_", 1)[-1] == stamp]
        for f in run:
            if f.entity == "TRANSACTION":
                for t in f.rows:
                    sent[t["AccountId"]].append((day(t.get("ValueDate")), t.get("BookingDateTime", ""),
                                                 money(t.get("UpdatedBalance"))))
        for f in run:
            for a in (f.rows if f.entity == "ACCOUNT" else []):
                due = [t for t in sent[a["AccountId"]] if t[0] <= day(f.business_date)]
                top = max((t[:2] for t in due), default=None)
                # Rows booked in the same second have no order, so any of them may be the latest.
                left = {t[2] for t in due if t[:2] == top} or {Decimal(0)}
                if money(a.get("AccountBalance")) not in left:
                    out.add("an ACCOUNT balance is the latest UpdatedBalance sent for it", f.name,
                            "account {} reads {}, its last transaction sent left {}".format(
                                a["AccountId"], a.get("AccountBalance"),
                                "/".join(str(x) for x in sorted(left))), a)


CORE_DSN = "postgresql://core:password@localhost:5432/core"


def core_rows(sql):
    done = subprocess.run(["psql", CORE_DSN, "-tA", "-F", "\t", "-c", sql], capture_output=True,
                          text=True, timeout=60)
    if done.returncode != 0:
        return None
    return [line.split("\t") for line in done.stdout.splitlines() if line.strip()]


def files_in_core(bank_uid):
    rows = core_rows("SELECT count(*) FROM investec_file f JOIN partner_bank b ON b.sid = f.bank_sid "
                     "WHERE b.uid = '{}'".format(bank_uid))
    return int(rows[0][0]) if rows else None


def check_against_core(files, bank_uid, out):
    """Each file's trailer against the row core keeps for it, which core wrote from the same run."""
    rows = core_rows("SELECT f.file_name, f.data_record_count, f.insert_record_count, "
                     "f.update_record_count, coalesce(f.control_total_sha256, '') "
                     "FROM investec_file f JOIN partner_bank b ON b.sid = f.bank_sid "
                     "WHERE b.uid = '{}'".format(bank_uid))
    if rows is None:
        out.add("core's file rows can be read", "investec_file", "psql failed")
        return
    known = {}
    for r in rows:
        if r[0]:
            known.setdefault(r[0], []).append(r)
    for name, same in known.items():
        sealed = [r for r in same if r[4]]
        if len(sealed) > 1:
            # The archive key is the file name, and the name carries the extract time to the
            # second, so the later file replaced the earlier one in S3 and at the bank.
            out.add("no two sealed files share one name", name,
                    "{} sealed files, counts {}".format(len(sealed), [r[1] for r in sealed]))

    def counts(row, f):
        want = [row[1], row[2], row[3]] if f.entity in DELTA else [row[1]]
        return [str(int(x or 0)) for x in want]

    for f in files:
        same = known.get(f.name)
        if not same:
            out.add("every archived file has a row in core", f.name, "no investec_file row")
            continue
        have = [f.trl[3], f.trl[4], f.trl[5]] if f.entity in DELTA else [f.trl[3]]
        have = [str(int(x or 0)) for x in have]
        if not any(counts(r, f) == have and (not r[4] or r[4] == f.trl[-1]) for r in same):
            out.add("the trailer equals a file core sealed under that name", f.name,
                    "file {} {}, core {}".format(have, f.trl[-1][:12],
                                                [(counts(r, f), r[4][:12]) for r in same]))
    archived = {f.name for f in files}
    for name, same in known.items():
        if any(r[4] for r in same) and name not in archived:
            out.add("every sealed file is archived", name, "sealed in core, not in S3")


def check_feed_keeps_up(bank_uid, out):
    """RECON is sealed for every date up to the bank's last completed one, once each."""
    rows = core_rows("SELECT f.business_effective_date, count(*) FROM investec_file f "
                     "JOIN partner_bank b ON b.sid = f.bank_sid WHERE b.uid = '{}' "
                     "AND f.file_type = 'RECON' AND f.control_total_sha256 IS NOT NULL "
                     "GROUP BY 1 ORDER BY 1".format(bank_uid))
    position = feed_position(bank_uid)
    if rows is None or position is None:
        out.add("core's RECON rows and bank business date can be read", "investec_file", "psql failed")
        return
    sealed = {date.fromisoformat(r[0]): int(r[1]) for r in rows}
    for d, count in sealed.items():
        if count > 1:
            out.add("every business day has one sealed RECON", "RECON " + d.isoformat(),
                    "{} sealed RECON files".format(count))
    if sealed:
        d = min(sealed)
        while d < max(sealed):
            d += timedelta(days=1)
            if d not in sealed:
                out.add("every business day has one sealed RECON", "RECON " + d.isoformat(),
                        "no sealed RECON between {} and {}".format(min(sealed), max(sealed)))
    # The feed and RECON both skip while accruals-and-realisations is RUNNING or FAILED, so a
    # lag then is the product holding, not the feed falling behind.
    accruals = core_rows("SELECT bes.status FROM bank_event_scheduling bes JOIN partner_bank b ON "
                         "b.sid = bes.bank_sid WHERE b.uid = '{}' AND bes.event_type = "
                         "'ACCRUALS_AND_REALISATIONS'".format(bank_uid))
    if accruals and accruals[0][0] in ("RUNNING", "FAILED"):
        return
    last_completed = date.fromisoformat(position[1]) - timedelta(days=1)
    if sealed and (last_completed - max(sealed)).days > 1:
        out.add("the feed's RECON keeps up with the bank's business date", "RECON " + max(sealed).isoformat(),
                "latest sealed RECON {}, bank's last completed business date {}".format(
                    max(sealed), last_completed))


def check_account_balance_against_core(files, bank_uid, out):
    """An ACCOUNT balance is core's product account balance as the collector reads it: the latest
    account_transaction by value date up to the file's date."""
    extracts = core_rows("SELECT f.file_name, extract(epoch FROM f.extract_window_end_at) "
                         "FROM investec_file f JOIN partner_bank b ON b.sid = f.bank_sid "
                         "WHERE b.uid = '{}' AND f.file_type = 'ACCOUNT' AND "
                         "f.control_total_sha256 IS NOT NULL".format(bank_uid))
    history = core_rows("SELECT cpa.uid, at.value_date, extract(epoch FROM at.created_at), "
                        "at.updated_product_account_balance FROM account_transaction at "
                        "JOIN customer_product_account cpa ON cpa.sid = at.customer_product_account_sid "
                        "JOIN direct_customer_account dca ON dca.customer_product_account_sid = cpa.sid "
                        "JOIN platform_product pp ON pp.sid = cpa.platform_product_sid "
                        "JOIN bank_product bp ON bp.sid = pp.product_sid "
                        "JOIN partner_bank b ON b.sid = bp.bank_sid WHERE b.uid = '{}' "
                        "ORDER BY at.value_date, at.created_at, at.sid".format(bank_uid))
    if extracts is None or history is None:
        out.add("core's account transactions can be read", "account_transaction", "psql failed")
        return
    extract_at = {r[0]: Decimal(r[1]) for r in extracts}
    by_account = defaultdict(list)
    for uid, value_date, created_at, balance in history:
        by_account[uid].append((value_date, Decimal(created_at), money(balance)))

    def held(rows, value_date, cutoff):
        core = Decimal(0)
        for vd, created_at, balance in rows:
            if vd <= value_date and created_at <= cutoff:
                core = balance
        return core

    for f in files:
        if f.entity != "ACCOUNT" or f.name not in extract_at:
            continue
        for row in f.rows:
            rows = by_account.get(row["AccountId"], [])
            at = extract_at[f.name]
            core = held(rows, day(f.business_date), at)
            if money(row.get("AccountBalance")) == core:
                continue
            # The collector reads after the stamp, and the stack's clock runs ten times real time.
            later = {held(rows, day(f.business_date), c) for _, c, _ in rows if at < c <= at + EXTRACT_GAP}
            note = (" - counts a transaction created after the extract, finding 33"
                    if money(row.get("AccountBalance")) in later else "")
            out.add("an ACCOUNT file's balance equals core's at its extract", f.name,
                    "account {} reads {}, core held {} at the extract{}".format(
                        row["AccountId"], row.get("AccountBalance"), core, note), row)


def feed_position(bank_uid):
    """The newest TRANSACTION file's business date and the bank's business date, as text."""
    rows = core_rows("SELECT (SELECT max(f.business_effective_date) FROM investec_file f "
                     "WHERE f.bank_sid = b.sid AND f.file_type = 'TRANSACTION'), bbd.business_date "
                     "FROM partner_bank b JOIN bank_business_date bbd ON bbd.bank_sid = b.sid "
                     "WHERE b.uid = '{}'".format(bank_uid))
    return tuple(rows[0][:2]) if rows else None


def generate(bank_uid, times):
    from explorer import config, local_auth, world
    from explorer.client import BearerClient
    settings = config.load("local")
    if settings.get("local_auth"):
        local_auth.ensure_serving()
    ops = BearerClient(settings["ops_base_url"], world.ops_token(settings), timeout=300)
    # adapter finds a bank only in the partner file core writes every five minutes, and the
    # schedulers are off locally, so a new bank's files fail with "No partner bank".
    ops.call("POST", "/operations/processor/partners/refresh")
    # The feed ships one business date a run, and a run of the harness moves the bank many dates
    # on, so a fixed number of rounds left the feed months behind and RECON, which waits for the
    # feed to reach its target date, never ran. Ask until three rounds in a row write nothing.
    statuses = {}
    still = 0
    before = feed_position(bank_uid)
    caught = 0
    for _ in range(times):
        world.mark_feed_files_sent(bank_uid)
        for event in ("DIRECT_DATA_FEED", "DIRECT_DATA_RECON"):
            call = ops.call("POST", "/operations/batch/processor/bank/{}/{}/sync".format(
                bank_uid, event))
            key = "{} {}".format(event, call.status)
            statuses[key] = statuses.get(key, 0) + 1
            # A file is named by its extract time to the second, so two runs inside one second
            # write the same name and the later replaces the earlier (see the name check).
            time.sleep(1.1)
        # A run reads a transaction as unsent until adapter seals the file that carries it, so
        # the next run waits for the seal or it sends the same transactions again (finding 9).
        for _ in range(30):
            unsealed = core_rows("SELECT count(*) FROM investec_file f JOIN partner_bank b ON "
                                 "b.sid = f.bank_sid WHERE b.uid = '{}' AND f.file_name IS NOT "
                                 "NULL AND f.control_total_sha256 IS NULL".format(bank_uid))
            if not unsealed or unsealed[0][0] == "0":
                break
            time.sleep(1)
        # Stop on the feed's date, not on its file count. Once the feed reaches the bank's open
        # date every run still writes a set of files for that date, so counting files never saw
        # three quiet rounds and all 400 rounds ran, about seventeen minutes between cycles.
        # One more round after the feed catches up, so RECON can seal the day before it.
        after = feed_position(bank_uid)
        caught = caught + 1 if after and after[0] and after[0] == after[1] else 0
        if caught >= 2:
            break
        still = still + 1 if after == before else 0
        before = after
        if still >= 3:
            break
    ops.close()
    return ["{} x{}".format(k, v) for k, v in statuses.items()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("bank_uid")
    parser.add_argument("--generate", type=int, default=0)
    parser.add_argument("--json")
    args = parser.parse_args()
    if args.generate:
        print("generated: {}".format(", ".join(generate(args.bank_uid, args.generate))))
    paths = fetch(args.bank_uid)
    files = [File(p) for p in paths]
    out = Findings()
    for f in files:
        check_envelope(f, out)
    check_history(files, out)
    check_account_against_transactions(files, out)
    check_against_core(files, args.bank_uid, out)
    check_feed_keeps_up(args.bank_uid, out)
    check_account_balance_against_core(files, args.bank_uid, out)
    by_entity = defaultdict(int)
    for f in files:
        by_entity[f.entity] += 1
    print("{} files: {}".format(len(files), dict(by_entity)))
    for item in out.items:
        print("  FAIL {}: {} — {}".format(item["rule"], item["file"], item["detail"]))
    print("{} checks failed".format(len(out.items)))
    if args.json:
        Path(args.json).write_text(json.dumps({"files": [f.name for f in files],
                                               "findings": out.items}, indent=1))
    return 1 if out.items else 0


if __name__ == "__main__":
    sys.exit(main())
