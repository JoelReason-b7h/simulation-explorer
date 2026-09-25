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
from collections import defaultdict
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


def aws(*args, timeout=120):
    env = dict(os.environ)
    env.update(AWS_ENV)
    return subprocess.run(["aws", "--endpoint-url", S3, *args], capture_output=True, text=True,
                          timeout=timeout, env=env)


def fetch(bank_uid, family="direct"):
    """Copy the bank's files into feeds/<family>/<bankUid>/ and return their paths, oldest first."""
    target = CACHE / family / bank_uid
    target.mkdir(parents=True, exist_ok=True)
    done = aws("s3", "sync", "s3://{}/archive/{}/{}/".format(BUCKET, family, bank_uid),
               str(target), "--quiet")
    if done.returncode != 0:
        raise SystemExit("could not read s3://{}/archive/{}/{}/: {}".format(
            BUCKET, family, bank_uid, done.stderr.strip()[:300]))
    return sorted(target.glob("*.csv"), key=lambda p: p.name.split("_", 1)[-1])


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
            delivered[f.entity][f.business_date].add(ident)
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
                accrued_by_date[f.business_date][r["AccountId"]] = money(
                    r.get("AccruedInterestAmount"))
                balance_by_date[f.business_date][r["AccountId"]] = money(r.get("AccountBalance"))
        if f.entity == "TRANSACTION":
            ordered = sorted(f.rows, key=lambda r: (r.get("ValueDate", ""),
                                                    r.get("BookingDateTime", "")))
            for r in ordered:
                account = r.get("AccountId")
                if account not in seen["ACCOUNT"]:
                    out.add("a transaction's account was sent first", f.name,
                            "transaction {} names account {}".format(r.get("TransactionId"),
                                                                     account), r)
                amount, updated = money(r.get("Amount")), money(r.get("UpdatedBalance"))
                daily_transactions[r.get("ValueDate")] += 1
                if account in last_balance:
                    step = updated - last_balance[account]
                    if abs(step) != abs(amount):
                        out.add("UpdatedBalance is the previous UpdatedBalance plus the amount",
                                f.name, "account {}: {} -> {} for amount {} ({})".format(
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
    for f in recons:
        if not f.rows:
            out.add("RECON carries one row", f.name, "no rows")
            continue
        r = f.rows[0]
        day = r.get("BusinessEffectiveDate")
        if day != f.business_date:
            out.add("the RECON row is for the HDR's business date", f.name,
                    "row {} HDR {}".format(day, f.business_date), r)
        if previous and day <= previous:
            out.add("RECON moves forward one business date at a time", f.name,
                    "{} follows {}".format(day, previous), r)
        previous = day
        expected = {
            "TotalCustomerCount": len(cumulative(delivered["CUSTOMER"], day)),
            "TotalAccountCount": len(cumulative(delivered["ACCOUNT"], day)),
            "TotalProductCount": len(cumulative(delivered["PRODUCT"], day)),
            "DailyTransactionCount": daily_transactions.get(day, 0),
        }
        for column, want in expected.items():
            if int(r.get(column) or 0) != want:
                out.add("{} agrees with the feed files up to its date".format(column), f.name,
                        "RECON says {}, the files give {}".format(r.get(column), want), r)
        balances, accrued = {}, {}
        for d in sorted(accrued_by_date):
            if d <= day:
                accrued.update(accrued_by_date[d])
        want_accrued = sum(accrued.values(), Decimal(0)).quantize(PENNY)
        if money(r.get("TotalAccruedInterest")) != want_accrued:
            out.add("TotalAccruedInterest is the sum of the latest AccruedInterestAmount",
                    f.name, "RECON says {}, the ACCOUNT files give {}".format(
                        r.get("TotalAccruedInterest"), want_accrued), r)
        latest = {}
        for g in files:
            if g.entity == "TRANSACTION":
                for t in sorted(g.rows, key=lambda t: (t.get("ValueDate", ""),
                                                       t.get("BookingDateTime", ""))):
                    if t.get("ValueDate", "") <= day:
                        latest[t["AccountId"]] = money(t.get("UpdatedBalance"))
        balances = sum(latest.values(), Decimal(0)).quantize(PENNY)
        if money(r.get("TotalBalanceAtEndOfDay")) != balances:
            out.add("TotalBalanceAtEndOfDay is the sum of each account's latest UpdatedBalance",
                    f.name, "RECON says {}, the TRANSACTION files give {}".format(
                        r.get("TotalBalanceAtEndOfDay"), balances), r)


def check_account_against_transactions(files, out):
    """An ACCOUNT row's balance is the latest UpdatedBalance sent for it up to that date."""
    latest = {}
    events = []
    for f in files:
        if f.entity == "TRANSACTION":
            for t in f.rows:
                events.append((t.get("ValueDate", ""), 0, t.get("BookingDateTime", ""), "T", t, f))
        elif f.entity == "ACCOUNT":
            for a in f.rows:
                events.append((f.business_date, 1, "", "A", a, f))
    for _, _, _, kind, row, f in sorted(events, key=lambda e: e[:3]):
        if kind == "T":
            latest[row["AccountId"]] = money(row.get("UpdatedBalance"))
        elif row["AccountId"] in latest and money(row.get("AccountBalance")) != latest[
                row["AccountId"]]:
            out.add("an ACCOUNT balance is the latest UpdatedBalance sent for it", f.name,
                    "account {} reads {}, its last transaction left {}".format(
                        row["AccountId"], row.get("AccountBalance"), latest[row["AccountId"]]),
                    row)


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
    statuses = []
    for _ in range(times):
        for event in ("DIRECT_DATA_FEED", "DIRECT_DATA_RECON"):
            call = ops.call("POST", "/operations/batch/processor/bank/{}/{}/sync".format(
                bank_uid, event))
            statuses.append("{} {}".format(event, call.status))
    ops.close()
    return statuses


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
