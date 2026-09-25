"""Generate the Direct MI reports for a bank on the local stack and check what they hold.

    python3 checks/direct_mi.py <bankUid> [--date yyyy-mm-dd] [--json out.json]

The reports are asked for through the ops endpoints, which archive them to
s3://upload/archive/direct-mi/<bankUid>/ without pushing anything to the bank. Each report is
checked against its own rules and against the other two: the monthly flagged payments must agree
with the daily reconciliation on what is still open at month end, and the onboarding counts must
agree with core's customers.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import subprocess
import sys
import time
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import direct_feed  # noqa: E402

RECON_COLUMNS = ["Item Category", "Creation/Entry Date", "Value Date", "Amount",
                 "Account Identifier", "Payment Reference", "Payment ID", "Credit/Debit", "Reason"]
FLAGGED_COLUMNS = ["Reporting Month", "Direction", "Items", "Items Entered",
                   "Unresolved at Month End", "Min Time Held (hrs)", "Average Time Held (hrs)",
                   "Max Time Held (hrs)", "Total Value", "Notes / Outliers"]
ENDPOINTS = {"MI_RECON": "reconciliation", "FLAGGED_PAYMENTS": "flagged-payments",
             "ONBOARDING": "onboarding"}
CORE_DSN = "postgresql://core:password@localhost:5432/core"


def number(value):
    try:
        return Decimal(value) if value not in (None, "") else None
    except InvalidOperation:
        return "bad"


def generate(bank_uid, day):
    from explorer import config, local_auth, world
    from explorer.client import BearerClient
    settings = config.load("local")
    if settings.get("local_auth"):
        local_auth.ensure_serving()
    ops = BearerClient(settings["ops_base_url"], world.ops_token(settings), timeout=300)
    # adapter finds a bank only in the partner file core writes every five minutes, and the
    # schedulers are off locally, so a new bank's files fail with "No partner bank".
    ops.call("POST", "/operations/processor/partners/refresh")
    statuses = {}
    for key, path in ENDPOINTS.items():
        call = ops.call("POST", "/operations/report/direct-mi/{}".format(path),
                        params={"bankUid": bank_uid, "generateTo": day})
        statuses[key] = (call.status, call.body if not call.ok else None)
    ops.close()
    return statuses


def read(path):
    text = path.read_bytes().decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    return (rows[0] if rows else []), [r for r in rows[1:] if any(c.strip() for c in r)]


def check_recon(path, day, out):
    header, rows = read(path)
    if header != RECON_COLUMNS:
        out.add("MI_RECON has the specified columns in order", path.name, header)
        return []
    items = [dict(zip(header, r)) for r in rows]
    seen = set()
    for r in items:
        if r["Item Category"] != "EXCEPTION":
            out.add("every MI_RECON item is an EXCEPTION", path.name, r["Item Category"], r)
        amount = number(r["Amount"])
        if amount in (None, "bad") or amount <= 0:
            out.add("an MI_RECON amount is a positive number", path.name, r["Amount"], r)
        if r["Credit/Debit"] not in ("CREDIT", "DEBIT", "C", "D", "CRDT", "DBIT"):
            out.add("Credit/Debit names a direction", path.name, r["Credit/Debit"], r)
        for column in ("Creation/Entry Date", "Value Date"):
            try:
                if date.fromisoformat(r[column]) > date.fromisoformat(day):
                    out.add("no MI_RECON item is dated after the report date", path.name,
                            "{} {}".format(column, r[column]), r)
            except ValueError:
                out.add("MI_RECON dates are ISO dates", path.name, r[column], r)
        if not r["Reason"].strip():
            out.add("every MI_RECON item says why it is held", path.name, "empty Reason", r)
        key = (r["Payment ID"], r["Credit/Debit"], r["Account Identifier"])
        if r["Payment ID"] and key in seen:
            out.add("an MI_RECON item is listed once", path.name, key, r)
        seen.add(key)
    return items


def check_flagged(path, recon_items, month_end, out):
    header, rows = read(path)
    if header != FLAGGED_COLUMNS:
        out.add("FLAGGED_PAYMENTS has the specified columns in order", path.name, header)
        return
    items = [dict(zip(header, r)) for r in rows]
    directions = sorted(r["Direction"] for r in items)
    if directions != ["CREDIT", "DEBIT"]:
        out.add("FLAGGED_PAYMENTS has one CREDIT row and one DEBIT row", path.name, directions)
    for r in items:
        values = {c: number(r[c]) for c in FLAGGED_COLUMNS[2:9]}
        if "bad" in values.values():
            out.add("FLAGGED_PAYMENTS numbers parse", path.name, r, r)
            continue
        count, entered, open_ = (values["Items"], values["Items Entered"],
                                 values["Unresolved at Month End"])
        if entered is not None and count is not None and entered > count:
            out.add("Items Entered is no more than Items", path.name,
                    "{} entered of {}".format(entered, count), r)
        if open_ is not None and count is not None and open_ > count:
            out.add("Unresolved at Month End is no more than Items", path.name,
                    "{} open of {}".format(open_, count), r)
        low, mid, high = (values["Min Time Held (hrs)"], values["Average Time Held (hrs)"],
                          values["Max Time Held (hrs)"])
        if None not in (low, mid, high) and not low <= mid <= high:
            out.add("Min <= Average <= Max time held", path.name,
                    "{} {} {}".format(low, mid, high), r)
        if count == 0 and any(v not in (None, 0) for v in (low, mid, high, values["Total Value"])):
            out.add("a direction with no items holds no times and no value", path.name, r, r)
        if recon_items is not None and open_ is not None:
            want = sum(1 for i in recon_items
                       if i["Credit/Debit"][:1] == r["Direction"][:1])
            if int(open_) != want:
                out.add("Unresolved at Month End equals the month-end MI_RECON items",
                        path.name, "{} says {}, MI_RECON for {} lists {}".format(
                            r["Direction"], open_, month_end, want), r)


def onboarded_in_core(bank_uid, month):
    sql = ("SELECT count(*) FROM platform_customer pc "
           "JOIN partner_platform pp ON pp.sid = pc.platform_sid "
           "WHERE pp.direct_bank_uid = '{}' AND to_char(pc.created_at AT TIME ZONE "
           "'Europe/London', 'YYYY-MM') = '{}' AND (pc.verification_status = 'ACTIVATED' "
           "OR EXISTS (SELECT 1 FROM platform_customer_status_history h "
           "WHERE h.platform_customer_sid = pc.sid AND h.to_state = 'ACTIVATED'))").format(
        bank_uid, month)
    done = subprocess.run(["psql", CORE_DSN, "-tAc", sql], capture_output=True, text=True,
                          timeout=60)
    return int(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip() else None


def check_onboarding(path, bank_uid, out):
    header, rows = read(path)
    if len(header) != 7:
        out.add("ONBOARDING has seven columns", path.name, header)
        return
    for r in rows:
        month, received, onboarded, late, within, stp = r[:6]
        nums = [number(v) for v in (received, onboarded, late, within, stp)]
        if "bad" in nums:
            out.add("ONBOARDING numbers parse", path.name, r, r)
            continue
        received, onboarded, late, within, stp = nums
        if None not in (received, late) and late > received:
            out.add("Total exceeding target is no more than Total received", path.name, r, r)
        for label, pct in (("% within target", within), ("STP %", stp)):
            if pct is not None and not 0 <= pct <= 100:
                out.add("{} is between 0 and 100".format(label), path.name, r, r)
        if None not in (received, late, within) and received > 0:
            want = (received - late) * 100 / received
            if abs(want - within) > Decimal("0.6"):
                out.add("% within target is (received - exceeding) / received", path.name,
                        "{} reported, {:.2f} from its own counts".format(within, want), r)
        core = onboarded_in_core(bank_uid, month[:7])
        if core is not None and onboarded is not None and int(onboarded) != core:
            out.add("Total onboarded agrees with core's activated customers", path.name,
                    "report {} core {} for {}".format(onboarded, core, month), r)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("bank_uid")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--json")
    args = parser.parse_args()
    out = direct_feed.Findings()
    statuses = generate(args.bank_uid, args.date)
    for key, (status, body) in statuses.items():
        print("  {} generate {}".format(key, status))
        if status >= 500:
            out.add("generating an MI report succeeds", key, "{} {}".format(status, body))
    time.sleep(10)
    paths = {p.name: p for p in direct_feed.fetch(args.bank_uid, "direct-mi")}
    day = args.date
    month_end = None
    recon = paths.get("MI_RECON_{}.csv".format(day))
    recon_items = check_recon(recon, day, out) if recon else None
    if recon is None:
        out.add("the MI_RECON file for the date is archived", "MI_RECON_{}.csv".format(day),
                "not in S3 after generation")
    flagged = sorted(n for n in paths if n.startswith("FLAGGED_PAYMENTS_"))
    if flagged:
        month_end = flagged[-1][len("FLAGGED_PAYMENTS_"):-4]
        month_recon = paths.get("MI_RECON_{}.csv".format(month_end))
        # A report asked for in the middle of a month is snapped to its month end, which has no
        # MI_RECON yet, so the items still open today stand in for the ones open at month end.
        if month_recon:
            month_items = check_recon(month_recon, month_end, direct_feed.Findings())
        else:
            month_items, month_end = recon_items, day
        check_flagged(paths[flagged[-1]], month_items, month_end, out)
    onboarding = sorted(n for n in paths if n.startswith("ONBOARDING_"))
    if onboarding:
        check_onboarding(paths[onboarding[-1]], args.bank_uid, out)
    print("{} MI files; MI_RECON rows {}".format(
        len(paths), len(recon_items) if recon_items is not None else "none"))
    for item in out.items:
        print("  FAIL {}: {} — {}".format(item["rule"], item["file"], item["detail"]))
    print("{} checks failed".format(len(out.items)))
    if args.json:
        Path(args.json).write_text(json.dumps({"files": sorted(paths), "findings": out.items},
                                              indent=1, default=str))
    return 1 if out.items else 0


if __name__ == "__main__":
    sys.exit(main())
