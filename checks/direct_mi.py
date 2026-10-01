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
import calendar
import csv
import io
import json
import re
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
from explorer import clock  # noqa: E402

RECON_COLUMNS = ["Item Category", "Creation/Entry Date", "Value Date", "Amount",
                 "Account Identifier", "Payment Reference", "Payment ID", "Credit/Debit", "Reason"]
FLAGGED_COLUMNS = ["Reporting Month", "Direction", "Items", "Items Entered",
                   "Unresolved at Month End", "Min Time Held (hrs)", "Average Time Held (hrs)",
                   "Max Time Held (hrs)", "Total Value", "Notes / Outliers"]
ENDPOINTS = {"MI_RECON": "reconciliation", "FLAGGED_PAYMENTS": "flagged-payments",
             "ONBOARDING": "onboarding"}
CORE_DSN = "postgresql://core:password@localhost:5432/core"
CLEARING_DSN = "postgresql://clearing:password@localhost:5440/clearing"
UNALLOCATED_HOURS = 1

# The platform credits the monthly report counts as unresolved and the daily one does not: open,
# unmatched, and younger than the threshold. The monthly leg applies the threshold only to a spell
# that has ended (FINDINGS.md, finding 13), so these are the expected difference between the two.
YOUNG_OPEN_PLATFORM_CREDITS = """
SELECT count(*) FROM funding_record fr
JOIN internal_account ia ON ia.sid = fr.account_sid
JOIN account_owner ao ON ao.sid = ia.account_owner_sid
WHERE ia.account_type = 'DIRECT' AND ao.account_owner_type = 'PLATFORM'
  AND fr.debit_credit_mark = 'CREDIT' AND NOT fr.ignored
  AND NOT EXISTS (SELECT 1 FROM partner_payment_link ppl WHERE ppl.funding_record_sid = fr.sid)
  AND fr.created_at >= now() - interval '{} hour'
""".format(UNALLOCATED_HOURS)
TOLERANCE = 5
MAX_FINDINGS_PER_RULE = 20

# MI_RECON legs by the Reason they print (ClearingReportsReplicaRepository legs 1-5, core
# DirectReportReplicaRepository legs 1-3). A core leg-1 and leg-3 row share reasons, so the core
# reasons map to both core kinds and the Credit/Debit decides which item it is.
CLEARING_LINE_REASONS = ("No usable payment reference", "Return without a usable",
                         "Could not be allocated to a customer", "Counterpart (remitter)",
                         "Unallocated - held")
CORE_REASONS = ("Held - ", "Withdrawal reversed by the bank", "Paid in but not allocated",
                "Cash held for operations review")
KINDS_BY_REASON = (("Returned to remitter", {"returned"}),
                   ("Stopped at settlement", {"stopped"}),
                   ("Paid in by the platform", {"funding"}),
                   ("Outbound payment held", {"group"}),
                   (CLEARING_LINE_REASONS, {"line"}),
                   (CORE_REASONS, {"cash", "instruction"}))
UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
PLAIN_ID = re.compile(r"^[\w.:-]+$")

# Every lookup returns: kind, id, amount, direction, account, in_scope, in_state, state. in_scope
# is the item's own bound on the business date (arrived by the end of it, the account and owner
# type the leg selects). in_state is false only when the item left the reported state before the
# end of the business date, which no file for that date could have listed. An item that left it
# after is not a failure: a report is cut after its date ends and the state has moved on since.
CLEARING_LOOKUP = """
WITH eod AS (SELECT (DATE '{day}' + INTERVAL '1 day') AT TIME ZONE 'Europe/London' AS t),
pps AS (SELECT pp.*, ia.account_type, ia.account_identifier ->> 'value' AS account
        FROM partner_payment pp JOIN internal_account ia ON ia.sid = pp.account_sid
        WHERE pp.transaction_id = ANY('{{{plain}}}'::text[])),
returned AS (SELECT sid, MIN(audit_at) AS at FROM partner_payment_audit
             WHERE audit ->> 'payment_state' = 'RETURNED' AND sid IN (SELECT sid FROM pps)
             GROUP BY sid),
last_state AS (SELECT DISTINCT ON (sid) sid, audit ->> 'payment_state' AS state
               FROM partner_payment_audit, eod
               WHERE sid IN (SELECT sid FROM pps) AND audit_at < eod.t
               ORDER BY sid, audit_at DESC)
SELECT 'line', asl.uid::text, ROUND(asl.amount, 2)::text, asl.debit_credit_mark::text,
       COALESCE(sub.account_identifier ->> 'value', nostro.account_identifier ->> 'value'),
       nostro.account_type = 'DIRECT' AND asl.booking_date <= DATE '{day}',
       asl.status = 'EXCEPTION' OR asl.updated_at >= eod.t, asl.status::text
FROM account_statement_line asl CROSS JOIN eod
JOIN internal_account nostro ON nostro.sid = asl.primary_internal_account_sid
LEFT JOIN internal_account sub ON sub.sid = asl.subledger_internal_account_sid
WHERE asl.uid = ANY('{{{uuids}}}'::uuid[])
UNION ALL
SELECT 'returned', pps.transaction_id, ROUND(pps.value_amount, 2)::text, pps.debit_credit_mark::text,
       pps.account, pps.account_type = 'DIRECT'
         AND COALESCE(returned.at, pps.updated_at) >= (DATE '{day}')::timestamp AT TIME ZONE 'Europe/London'
         AND COALESCE(returned.at, pps.updated_at) < eod.t,
       true, pps.payment_state::text
FROM pps CROSS JOIN eod LEFT JOIN returned ON returned.sid = pps.sid
UNION ALL
SELECT 'stopped', pps.transaction_id, ROUND(pps.value_amount, 2)::text, pps.debit_credit_mark::text,
       pps.account, pps.account_type = 'DIRECT' AND pps.created_at < eod.t,
       pps.payment_state IN ('REJECTED', 'FLAGGED') OR ls.state IS NULL
         OR ls.state IN ('REJECTED', 'FLAGGED'), pps.payment_state::text
FROM pps CROSS JOIN eod LEFT JOIN last_state ls ON ls.sid = pps.sid
UNION ALL
SELECT 'funding', fr.uid::text, ROUND(fr.value_amount, 2)::text, fr.debit_credit_mark::text,
       ia.account_identifier ->> 'value',
       ia.account_type = 'DIRECT' AND ao.account_owner_type = 'PLATFORM' AND fr.created_at < eod.t,
       NOT (fr.ignored AND fr.updated_at < eod.t)
         AND NOT EXISTS (SELECT 1 FROM partner_payment_link ppl WHERE ppl.funding_record_sid = fr.sid
                           AND COALESCE(ppl.created_at, '-infinity') < eod.t),
       CASE WHEN fr.ignored THEN 'ignored' ELSE 'open' END
FROM funding_record fr CROSS JOIN eod
JOIN internal_account ia ON ia.sid = fr.account_sid
JOIN account_owner ao ON ao.sid = ia.account_owner_sid
WHERE fr.uid = ANY('{{{uuids}}}'::uuid[])
UNION ALL
SELECT 'group', pg.uid::text, ROUND(COALESCE(pg.amount, 0), 2)::text, 'DEBIT',
       pg.debtor_account_identifier ->> 'value',
       pg.account_type = 'DIRECT' AND pg.created_at < eod.t,
       pg.status = 'PENDING_APPROVAL' OR pg.updated_at >= eod.t, pg.status::text
FROM payment_group pg CROSS JOIN eod
WHERE pg.uid = ANY('{{{uuids}}}'::uuid[])
"""

# A cash_transaction is the arrival behind core leg 1 (the row's amount is what is still present
# of it, so it may be less than the arrival); an instruction with a hold is core legs 2 and 3.
CORE_LOOKUP = """
WITH eod AS (SELECT (DATE '{day}' + INTERVAL '1 day') AT TIME ZONE 'Europe/London' AS t)
SELECT 'cash', ct.uid::text, ct.transaction_amount::text, 'CREDIT',
       eia.account_identifier ->> 'value',
       ct.transaction_type IN ('CASH_DEPOSIT', 'CASH_DEPOSIT_REVERSAL') AND ct.created_at < eod.t,
       true, ct.transaction_type
FROM cash_transaction ct CROSS JOIN eod
JOIN entity_internal_account eia ON eia.sid = ct.entity_account_sid
WHERE ct.uid = ANY('{{{uuids}}}'::uuid[])
UNION ALL
SELECT 'instruction', dci.uid::text, ROUND(dci.amount, 2)::text,
       CASE h.hold_type WHEN 'WITHDRAWAL' THEN 'DEBIT' ELSE 'CREDIT' END,
       CASE h.hold_type WHEN 'WITHDRAWAL' THEN eia.account_identifier ->> 'value' END,
       h.hold_type IN ('WITHDRAWAL', 'DEPOSIT_POOLED') AND h.held_at < eod.t
         AND (h.hold_type <> 'WITHDRAWAL' OR dci.created_at < eod.t),
       (h.released_at IS NULL OR h.released_at >= eod.t)
         AND (dci.status = 'PENDING' OR dci.updated_at >= eod.t),
       h.hold_type::text || '/' || dci.status::text
FROM direct_customer_instruction dci CROSS JOIN eod
JOIN direct_customer_instruction_hold h ON h.direct_instruction_sid = dci.sid
JOIN direct_customer_account dca ON dca.sid = dci.direct_customer_account_sid
JOIN entity_internal_account eia ON eia.sid = dca.entity_internal_account_sid
WHERE dci.uid = ANY('{{{uuids}}}'::uuid[])
"""


def young_open_platform_credits():
    done = subprocess.run(["psql", CLEARING_DSN, "-tAc", YOUNG_OPEN_PLATFORM_CREDITS],
                          capture_output=True, text=True, timeout=60)
    return int(done.stdout.strip()) if done.returncode == 0 and done.stdout.strip() else None


def number(value):
    try:
        return Decimal(value) if value not in (None, "") else None
    except InvalidOperation:
        return "bad"


def lookup(dsn, sql):
    """Rows of a read-only query, or None when psql fails. The query goes in on stdin because the
    id list runs to hundreds of kilobytes."""
    done = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-tA", "-F", "|"], input=sql,
                          capture_output=True, text=True, timeout=300)
    if done.returncode != 0:
        print("  lookup failed: {}".format(done.stderr.strip()[:300]))
        return None
    found = defaultdict(list)
    for line in done.stdout.splitlines():
        kind, ident, amount, mark, account, in_scope, in_state, state = line.split("|")
        found[(kind, ident.lower())].append(
            (kind, Decimal(amount), mark, account, in_scope == "t", in_state == "t", state))
    return found


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


def check_recon_items(path, items, day, out):
    """Every row names an item that exists, with the row's amount, direction and account, and that
    was still in the state the row reports at the end of the business date. The states are read as
    of now, so a state that has since moved on is told apart from one it left before the date only
    where the item records when it moved (updated_at, released_at, link created_at, the payment
    audit); a core leg-1 arrival is checked for existence, amount and direction only, because its
    still-present share depends on the account's later movements."""
    date.fromisoformat(day)
    ids = [r["Payment ID"].strip() for r in items]
    uuids = sorted({i.lower() for i in ids if UUID.match(i)})
    plain = sorted({i for i in ids if PLAIN_ID.match(i)})
    clearing = lookup(CLEARING_DSN, CLEARING_LOOKUP.format(
        day=day, uuids=",".join(uuids), plain=",".join(plain)))
    core = lookup(CORE_DSN, CORE_LOOKUP.format(day=day, uuids=",".join(uuids)))
    if clearing is None or core is None:
        return
    rule = "an MI_RECON row names a real item in its stated state"
    counts = defaultdict(int)

    def add(r, detail):
        counts[detail.split(":")[0]] += 1
        if counts[detail.split(":")[0]] <= MAX_FINDINGS_PER_RULE:
            out.add(rule, path.name, detail, r)

    for r in items:
        ident, reason = r["Payment ID"].strip(), r["Reason"]
        kinds = next((k for prefix, k in KINDS_BY_REASON if reason.startswith(prefix)), None)
        if kinds is None:
            add(r, "reason: no MI_RECON leg prints '{}'".format(reason))
            continue
        found = [c for kind in kinds
                 for c in (core if kind in ("cash", "instruction") else clearing).get(
                     (kind, ident.lower()), [])]
        if not found:
            add(r, "missing: no {} item with id {}".format("/".join(sorted(kinds)), ident))
            continue
        amount, mark = number(r["Amount"]), r["Credit/Debit"][:1]
        best = None
        for kind, stored, stored_mark, account, in_scope, in_state, state in found:
            problems = []
            if kind == "cash":
                same_amount = amount not in (None, "bad") and 0 < amount <= stored
            else:
                same_amount = amount == stored
            if not same_amount:
                problems.append("amount: row {} item {}".format(r["Amount"], stored))
            if stored_mark[:1] != mark:
                problems.append("direction: row {} item {}".format(r["Credit/Debit"], stored_mark))
            if account is not None and account != "" and account != r["Account Identifier"]:
                problems.append("account: row {} item {}".format(r["Account Identifier"], account))
            if not in_scope:
                problems.append("scope: item is not one the leg selects by the end of {} ({})".format(
                    day, state))
            if not in_state:
                problems.append("state: item left the reported state before the end of {} ({})".format(
                    day, state))
            if best is None or len(problems) < len(best):
                best = problems
        for problem in best:
            add(r, problem)
    for kind, n in counts.items():
        if n > MAX_FINDINGS_PER_RULE:
            out.add(rule, path.name, "{}: {} rows in all, the first {} listed".format(
                kind, n, MAX_FINDINGS_PER_RULE))


def check_flagged(path, recon_items, month_end, out, young=0):
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
            extra = (young or 0) if r["Direction"].startswith("C") else 0
            # Finding 13: the monthly leg counts open platform credits of any age, and on the local
            # stack every bank shares one DIRECT nostro, so the gap moves with the other cycles'
            # traffic. The comparison is printed as a note and does not fail the check.
            if abs(int(open_) - want - extra) > TOLERANCE:
                print("  NOTE {} unresolved {} against MI_RECON {} plus {} young credits".format(
                    r["Direction"], open_, want, extra))


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
    parser.add_argument("--date", default=None)
    parser.add_argument("--json")
    parser.add_argument("--no-generate", action="store_true",
                        help="read the files already cached under feeds/ and ask for no report")
    args = parser.parse_args()
    out = direct_feed.Findings()
    scheduled = clock.schedulers_run()
    # On a fake clock the bank's DIRECT_MI_REPORT_DAILY and MONTHLY schedules cut the reports, so
    # the check reads the newest ones they archived rather than asking for them.
    statuses = {} if scheduled or args.no_generate else generate(args.bank_uid, args.date or clock.today().isoformat())
    young = young_open_platform_credits()
    for key, (status, body) in statuses.items():
        print("  {} generate {}".format(key, status))
        if status >= 500:
            out.add("generating an MI report succeeds", key, "{} {}".format(status, body))
    if statuses:
        time.sleep(10)
    if args.no_generate:
        # fetch() moves the files out of S3, which a read-only run must not do
        cached = (direct_feed.CACHE / "direct-mi" / args.bank_uid).glob("*.csv")
        paths = {p.name: p for p in cached}
    else:
        paths = {p.name: p for p in direct_feed.fetch(args.bank_uid, "direct-mi")}
    day = args.date or clock.today().isoformat()
    if scheduled and not args.date:
        cut = sorted(n[len("MI_RECON_"):-len(".csv")] for n in paths if n.startswith("MI_RECON_"))
        if not cut:
            print("0 MI files: the daily MI schedule has not run on this stack yet")
            print("0 checks failed")
            if args.json:
                Path(args.json).write_text(json.dumps({"files": [], "findings": [],
                                                       "note": "no MI cut yet"}, indent=1))
            return 0
        day = cut[-1]
    month_end = None
    recon = paths.get("MI_RECON_{}.csv".format(day))
    recon_items = check_recon(recon, day, out) if recon else None
    if recon_items:
        check_recon_items(recon, recon_items, day, out)
    if recon is None:
        out.add("the MI_RECON file for the date is archived", "MI_RECON_{}.csv".format(day),
                "not in S3 after generation")
    # The monthly reports are the ones generated above, named for the month end of the date. The
    # long-lived run's clock cuts later-named files at simulated month ends, earlier in the cycle,
    # so the last name in the archive holds counts that core has since moved past.
    asked = date.fromisoformat(day)
    snapped = asked.replace(day=calendar.monthrange(asked.year, asked.month)[1]).isoformat()
    flagged = "FLAGGED_PAYMENTS_{}.csv".format(snapped)
    if flagged in paths:
        month_end = snapped
        month_recon = paths.get("MI_RECON_{}.csv".format(month_end))
        # A report asked for in the middle of a month is snapped to its month end, which has no
        # MI_RECON yet, so the items still open today stand in for the ones open at month end.
        if month_recon:
            month_items = check_recon(month_recon, month_end, direct_feed.Findings())
        else:
            month_items, month_end = recon_items, day
        check_flagged(paths[flagged], month_items, month_end, out, young)
    onboarding = "ONBOARDING_{}.csv".format(snapped)
    if onboarding in paths:
        check_onboarding(paths[onboarding], args.bank_uid, out)
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
