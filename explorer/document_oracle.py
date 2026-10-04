"""A document oracle for the scheduled monthly statements and the tax certificates of Direct customers.

statement_oracle holds one statement's numbers against core. This module holds the documents
themselves against the code that creates them, from the customer_document rows and the PDFs:

- Scheduled statements (StatementGenerationService -> CustomerDocumentService.createStubsForPlatform).
  One stub per (customer, product account) of the platform whose direct_customer_account is OPEN,
  CLOSING or CLOSED and whose platform_customer is ACTIVATED (CustomerActionPolicy
  GENERATE_CUSTOMER_DOCUMENT; findCustomerAccountDetailsByPlatformAndDirectAccount), stubbed with
  the period fromDate = lastRaised of the schedule (UTC date) and toDate = end of that month, and
  fromDate = today - 1 month on the first run. Right period for a run in month M+1 is the calendar
  month M. The stub is inserted ON CONFLICT (document_key) DO NOTHING, and the key holds the
  platform, the customer reference and a name made of the product alias and the account reference,
  so two accounts of one customer with the same alias and no reference collide and the second gets
  no statement.
- Per month with statements: a document for every account that was eligible when the run stubbed
  (account status now eligible and last changed before the run, customer ACTIVATED at the run from
  platform_customer_status_history), no second document for an account and month, the period equal
  to the calendar month, no gap or overlap with the previous month's document, and no document for
  an account or customer the query leaves out.
- A sample is rendered through ops (a stub renders on the first presigned-URL request), its PDF is
  parsed and judged with statement_oracle over the document's own period, and the
  CUSTOMER_DOCUMENT_CREATED webhooks naming the document are counted.
- Closure tax certificates: CustomerDocumentService.createClosureTaxCertificateStubs is called only
  by TrustCustomerClosureStrategy. DirectCustomerClosureStrategy.createClosureDocuments is a no-op,
  so a closed Direct customer is expected to have no certificate. The module reports how many
  closed customers there are, and parses any ANNUAL_TAX_SUMMARY document (the Direct template is
  statement/annual-interest-certificate.html), holding its interest against the INTEREST rows of
  the customer's NONE/GBP customer account with value dates in the document's own tax-year period.

Everything here reads through read-only SQL and through the ops presigned-URL call.
"""

from __future__ import annotations

import random
import re
import subprocess
import time
from calendar import monthrange
from datetime import date, datetime, timedelta
from decimal import Decimal

from explorer import clock
from explorer.interest_oracle import ZERO, _d, _day, _psql
from explorer.statement_oracle import (aer, judge, ledger, parse, percent, product_rate, text_of)  # noqa: F401

ELIGIBLE_ACCOUNT = ("OPEN", "CLOSING", "CLOSED")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MONEY = r"(-?)£(-?[\d,]+\.\d\d)"
DAY = r"(\d\d/\d\d/\d{4})"
# Findings of one rule in one batch. The first carries the evidence, the rest are counted in stats.
LIMIT = 20
# DirectCustomerClosureStrategy.createClosureDocuments produces nothing. Set True to hold Direct
# closures to the Trust behaviour (SAV-11141 follow-up) and report each closed customer without one.
REQUIRE_DIRECT_CLOSURE_CERTIFICATE = False


def _uuid(text):
    if not UUID.match(text or ""):
        raise ValueError("not a uuid: {!r}".format(text))
    return text


def _month_end(day):
    return day.replace(day=monthrange(day.year, day.month)[1])


def _first_of_month(day):
    return day.replace(day=1)


def _previous_month_end(day):
    return _first_of_month(day) - timedelta(days=1)


def _finding(rule, subject, detail, expected, actual):
    return {"rule": rule, "subject": subject, "detail": detail,
            "expected": str(expected), "actual": str(actual)}


def tax_year_start(day):
    """TaxYearCalculator.currentTaxYearStart: 6 April on or before the date."""
    start = date(day.year, 4, 6)
    return start if day >= start else date(day.year - 1, 4, 6)


def _platforms(platform_uid):
    if platform_uid:
        return [_uuid(platform_uid)], None
    rows, error = _psql("SELECT uid FROM partner_platform WHERE direct_bank_uid IS NOT NULL AND live")
    if rows is None:
        return None, error
    return [r[0] for r in rows], None


def _documents(platform, kind):
    """The platform's customer_document rows of a type as dicts, or (None, error)."""
    rows, error = _psql("""
      SELECT d.uid, d.document_key, d.document_name, d.document_date, d.generated, d.created_at,
             d.parameters->>'fromDate', d.parameters->>'toDate', d.parameters->>'productAccountUid',
             pc.uid, pc.external_id, d.parameters IS NOT NULL
      FROM customer_document d
      JOIN platform_customer pc ON pc.sid = d.customer_sid
      JOIN partner_platform pp ON pp.sid = pc.platform_sid
      WHERE pp.uid = '{platform}' AND d.document_type = '{kind}'
      """.format(platform=_uuid(platform), kind=kind), timeout=120)
    if rows is None:
        return None, error
    keys = ("uid", "key", "name", "date", "generated", "created", "from", "to", "account",
            "customer", "reference", "params")
    docs = []
    for row in rows:
        doc = dict(zip(keys, row))
        doc["date"] = _day(doc["date"])
        doc["from"] = _day(doc["from"])
        doc["to"] = _day(doc["to"])
        doc["generated"] = doc["generated"] == "t"
        doc["params"] = doc["params"] == "t"
        docs.append(doc)
    return docs, None


def _accounts_at(platform, run_at):
    """Every product account of the platform with what decides its statement at the run instant."""
    rows, error = _psql("""
      SELECT cpa.uid, pc.uid, pc.external_id, dca.account_reference, plp.alias, dca.status,
             dca.updated_at <= '{run}'::timestamptz, cpa.created_at::date, ca.tax_wrapper, ca.currency,
             COALESCE((SELECT h.to_state::text FROM platform_customer_status_history h
                       WHERE h.platform_customer_sid = pc.sid AND h.transitioned_at <= '{run}'::timestamptz
                       ORDER BY h.transitioned_at DESC, h.sid DESC LIMIT 1),
                      (SELECT h.from_state::text FROM platform_customer_status_history h
                       WHERE h.platform_customer_sid = pc.sid AND h.transitioned_at > '{run}'::timestamptz
                       ORDER BY h.transitioned_at, h.sid LIMIT 1),
                      pc.verification_status::text)
      FROM direct_customer_account dca
      JOIN customer_product_account cpa ON cpa.sid = dca.customer_product_account_sid
      JOIN customer_account ca ON ca.sid = cpa.customer_account_sid
      JOIN platform_customer pc ON pc.sid = ca.platform_customer_sid
      JOIN partner_platform pp ON pp.sid = pc.platform_sid
      JOIN platform_product plp ON plp.sid = cpa.platform_product_sid
      WHERE pp.uid = '{platform}'
      """.format(platform=_uuid(platform), run=run_at), timeout=120)
    if rows is None:
        return None, error
    keys = ("account", "customer", "external", "reference", "alias", "status", "settled",
            "opened", "wrapper", "currency", "customerAt")
    accounts = {}
    for row in rows:
        entry = dict(zip(keys, row))
        entry["settled"] = entry["settled"] == "t"
        entry["opened"] = _day(entry["opened"])
        accounts[entry["account"]] = entry
    return accounts, None


def _sanitise_name_part(text):
    cleaned = re.sub(r"\s+", " ", re.sub(r"[/\\\x00-\x1f\x7f]", " ", text or "")).strip()
    return cleaned or None


def _statement_suffix(account):
    parts = [_sanitise_name_part(account["alias"]), _sanitise_name_part(account["reference"])]
    return " ".join(p for p in parts if p)


def expected_name(account, end):
    """CustomerStatementDocumentName.forProductAccount for a Direct account."""
    isa = "ISA " if account["wrapper"] == "ISA" else ""
    suffix = _statement_suffix(account)
    base = "Monthly {}Statement {} ({})".format(isa, end, account["currency"])
    return "{} - {}.pdf".format(base, suffix) if suffix else base + ".pdf"


def statement_findings(platform, docs, schedule, stats):
    """Month-level findings for the platform's scheduled statements."""
    findings = []
    scheduled = [d for d in docs if d["params"] and d["from"] and d["to"]]
    batches = {}
    for doc in scheduled:
        batches.setdefault(doc["to"], []).append(doc)
    stats["months"] = stats.get("months", 0) + len(batches)
    stats["scheduledDocuments"] = stats.get("scheduledDocuments", 0) + len(scheduled)
    today = clock.today()

    by_account = {}
    for doc in scheduled:
        by_account.setdefault(doc["account"], []).append(doc)

    for end in sorted(batches):
        batch = batches[end]
        where = "platform {} statements to {}".format(platform, end)
        month_start = _first_of_month(end)
        wrong_period = [d for d in batch if d["from"] != month_start or d["to"] != _month_end(end)]
        for doc in wrong_period[:LIMIT]:
            findings.append(_finding(
                "a scheduled statement covers one whole calendar month",
                doc["account"],
                "{} has a document {} with period {} to {}, and the calendar month of {} runs {} to {}"
                .format(where, doc["uid"], doc["from"], doc["to"], end, month_start, _month_end(end)),
                "{} to {}".format(month_start, _month_end(end)),
                "{} to {}".format(doc["from"], doc["to"])))
        stats["wrongPeriod"] = stats.get("wrongPeriod", 0) + len(wrong_period)
        if end >= today:
            findings.append(_finding(
                "a scheduled statement covers a closed period",
                platform, "{} ends on or after today ({})".format(where, today), "< today", end))
        off_date = [d for d in batch if d["date"] != d["to"]]
        for doc in off_date[:LIMIT]:
            findings.append(_finding(
                "a statement's document date is the end of its period", doc["account"],
                "{} has document {} dated {} for a period ending {}".format(
                    where, doc["uid"], doc["date"], doc["to"]), doc["to"], doc["date"]))

        counts = {}
        for doc in batch:
            counts.setdefault(doc["account"], []).append(doc)
        for account, group in counts.items():
            if len(group) > 1:
                stats["duplicates"] = stats.get("duplicates", 0) + 1
                findings.append(_finding(
                    "an account has one statement for a month", account,
                    "{} has {} documents for account {}: {}".format(
                        where, len(group), account, [(d["uid"], d["name"]) for d in group]),
                    1, len(group)))

        run_at = min(d["created"] for d in batch)
        accounts, error = _accounts_at(platform, run_at)
        if accounts is None:
            stats.setdefault("errors", []).append(error)
            continue
        present = set(counts)
        named = {}
        for doc in docs:
            if doc["date"] == end and not doc["account"]:
                named.setdefault((doc["customer"], doc["name"]), doc)
        for doc in batch:
            account = accounts.get(doc["account"])
            if account is None:
                continue
            if account["status"] in ("REQUESTED", "CANCELLED"):
                findings.append(_finding(
                    "a statement exists only for an OPEN, CLOSING or CLOSED account", doc["account"],
                    "{} has document {} for an account that is {} now".format(
                        where, doc["uid"], account["status"]), "no document", doc["uid"]))
            if account["customerAt"] != "ACTIVATED":
                findings.append(_finding(
                    "a statement exists only for an ACTIVATED customer", doc["account"],
                    "{} has document {} and customer {} was {} when the run stubbed at {}".format(
                        where, doc["uid"], doc["customer"], account["customerAt"], run_at),
                    "no document", doc["uid"]))
            if account["opened"] and account["opened"] > end:
                stats["beforeOpening"] = stats.get("beforeOpening", 0) + 1
                findings.append(_finding(
                    "a statement covers a period in which the account existed", doc["account"],
                    "{} has document {} for period {} to {}, and the account was created on {} "
                    "(the stub query has no creation-date filter)".format(
                        where, doc["uid"], doc["from"], doc["to"], account["opened"]),
                    "no document", doc["uid"]))

        expected = [a for a in accounts.values()
                    if a["status"] in ELIGIBLE_ACCOUNT and a["settled"] and a["customerAt"] == "ACTIVATED"]
        uncertain = [a for a in accounts.values()
                     if a["status"] in ELIGIBLE_ACCOUNT and not a["settled"] and a["customerAt"] == "ACTIVATED"]
        stats["expectedAccounts"] = stats.get("expectedAccounts", 0) + len(expected)
        stats["uncertainAccounts"] = stats.get("uncertainAccounts", 0) + len(uncertain)
        missing = []
        for account in expected:
            if account["account"] in present:
                continue
            if (account["customer"], expected_name(account, end)) in named:
                stats["coveredByOpsDocument"] = stats.get("coveredByOpsDocument", 0) + 1
                continue
            missing.append(account)
        stats["missing"] = stats.get("missing", 0) + len(missing)
        for account in missing[:LIMIT]:
            rival = next((d for d in batch if d["customer"] == account["customer"]
                          and d["account"] != account["account"]
                          and d["name"] == expected_name(account, end)), None)
            reason = ("its document_key equals that of document {} (account {}), so the stub insert "
                      "did nothing on conflict".format(rival["uid"], rival["account"])
                      if rival else "no document row exists for it in the batch")
            findings.append(_finding(
                "every eligible account has a statement for the month", account["account"],
                "{} has no document for account {} (customer {}, {} {}, {}); {}".format(
                    where, account["account"], account["external"], account["alias"],
                    account["reference"] or "no account reference", account["status"], reason),
                "a document", "none"))

        previous_end = _previous_month_end(end)
        for account, group in by_account.items():
            here = next((d for d in group if d["to"] == end), None)
            before = next((d for d in group if d["to"] == previous_end), None)
            if here and before and before["to"] + timedelta(days=1) != here["from"]:
                findings.append(_finding(
                    "a statement starts the day after the previous statement ends", account,
                    "account {}: the statement to {} runs from {}, and the one before it ends {}".format(
                        account, end, here["from"], before["to"]),
                    before["to"] + timedelta(days=1), here["from"]))

    if schedule and schedule.get("last_raised"):
        raised = schedule["last_raised"]
        wanted = _previous_month_end(raised)
        if wanted not in batches:
            rows, error = _psql("""
              SELECT count(*) FROM direct_customer_account dca
              JOIN customer_product_account cpa ON cpa.sid = dca.customer_product_account_sid
              JOIN customer_account ca ON ca.sid = cpa.customer_account_sid
              JOIN platform_customer pc ON pc.sid = ca.platform_customer_sid
              JOIN partner_platform pp ON pp.sid = pc.platform_sid
              WHERE pp.uid = '{platform}' AND dca.status IN ('OPEN', 'CLOSING', 'CLOSED')
                AND pc.verification_status = 'ACTIVATED' AND cpa.created_at < '{raised}'
              """.format(platform=_uuid(platform), raised=raised.isoformat()))
            eligible = int(rows[0][0]) if rows else 0
            if eligible:
                findings.append(_finding(
                    "a STATEMENTS run produces the statements of the month before it", platform,
                    "the STATEMENTS schedule of platform {} last raised {} and no statement ends {}; "
                    "{} accounts were eligible".format(platform, raised, wanted, eligible),
                    "documents ending {}".format(wanted), "none"))
    return findings


def _account_row(account_uid):
    rows, error = _psql("""
      SELECT cpa.uid, pc.uid, cpa.sid, dca.account_reference, dca.status, bp.product_type,
             bp.interest_feature_realisation_period, plp.sid, bp.sid
      FROM customer_product_account cpa
      JOIN direct_customer_account dca ON dca.customer_product_account_sid = cpa.sid
      JOIN customer_account ca ON ca.sid = cpa.customer_account_sid
      JOIN platform_customer pc ON pc.sid = ca.platform_customer_sid
      JOIN platform_product plp ON plp.sid = cpa.platform_product_sid
      JOIN bank_product bp ON bp.sid = plp.product_sid
      WHERE cpa.uid = '{}'""".format(_uuid(account_uid)))
    if not rows:
        return None
    keys = ("account", "customer", "sid", "reference", "status", "type", "period", "pp", "bp")
    return dict(zip(keys, rows[0]))


def _announcements(document_uid):
    rows, error = _psql("""
      SELECT count(*) FROM platform_webhook_event
      WHERE event_type = 'CUSTOMER_DOCUMENT_CREATED'
        AND payload LIKE '%"documentId":"{}"%'""".format(_uuid(document_uid)))
    return int(rows[0][0]) if rows else None


def _generated(document_uid):
    rows, error = _psql("SELECT generated, document_key FROM customer_document WHERE uid = '{}'"
                        .format(_uuid(document_uid)))
    return (rows[0][0] == "t", rows[0][1]) if rows else (None, None)


def _render(ops, doc, stats):
    """Ask ops for the presigned URL, which renders a stub. Answers (ok, error)."""
    call = ops.call("GET", "/operations/statement/customer/document/{}".format(doc["uid"]))
    if not getattr(call, "ok", False):
        return False, "ops answered {} to the document request for {}".format(
            getattr(call, "status", "?"), doc["uid"])
    body = getattr(call, "body", None)
    if not isinstance(body, dict) or not body.get("presignedUrl"):
        return False, "ops answered 200 for {} with no presignedUrl".format(doc["uid"])
    return True, None


def sampled_statements(ops, docs, sample, stats):
    """Render, parse and judge a sample of the scheduled statements and count their webhooks."""
    findings = []
    pool = [d for d in docs if d["params"] and d["account"] and d["from"] and d["to"]
            and d["to"] < clock.today()]
    stubs = [d for d in pool if not d["generated"]]
    random.shuffle(stubs)
    rest = [d for d in pool if d["generated"]]
    random.shuffle(rest)
    for doc in (stubs[:max(sample - 1, 1)] + rest)[:sample]:
        stats["sampled"] = stats.get("sampled", 0) + 1
        was_stub = not doc["generated"]
        before = _announcements(doc["uid"])
        account = _account_row(doc["account"])
        if account is None:
            stats.setdefault("errors", []).append("no account row for {}".format(doc["account"]))
            continue
        try:
            ledger_before = ledger(account, doc["from"], doc["to"])
            rate_before = product_rate(account, min(doc["to"], clock.today()))
        except RuntimeError as fault:
            stats.setdefault("errors", []).append(str(fault)[:200])
            continue
        if was_stub:
            ok, error = _render(ops, doc, stats)
            if not ok:
                stats.setdefault("errors", []).append(error)
                continue
            stats["rendered"] = stats.get("rendered", 0) + 1
        generated, key = _generated(doc["uid"])
        if was_stub and not generated:
            findings.append(_finding(
                "rendering a stub marks its document generated", doc["account"],
                "document {} was still generated=false after ops answered its presigned-URL request"
                .format(doc["uid"]), "generated", "stub"))
            continue
        time.sleep(3 if was_stub else 0)
        after = _announcements(doc["uid"])
        if after is None or after == 0 or (was_stub and after != 1):
            note = "; it had {} before the render".format(before) if was_stub else ""
            findings.append(_finding(
                "a document is announced by one CUSTOMER_DOCUMENT_CREATED webhook", doc["account"],
                "document {} ({}) has {} CUSTOMER_DOCUMENT_CREATED webhooks naming it{}".format(
                    doc["uid"], doc["name"], after, note), 1, after))
        elif after > 1:
            stats["reannounced"] = stats.get("reannounced", 0) + 1
        try:
            ledger_after = ledger(account, doc["from"], doc["to"])
            rate_after = product_rate(account, min(doc["to"], clock.today()))
        except RuntimeError as fault:
            stats.setdefault("errors", []).append(str(fault)[:200])
            continue
        if ledger_before != ledger_after or rate_before != rate_after or ledger_after[0] is None:
            stats["skippedMoving"] = stats.get("skippedMoving", 0) + 1
            continue
        try:
            printed = parse(text_of(key))
        except (RuntimeError, OSError, subprocess.SubprocessError) as fault:
            stats.setdefault("errors", []).append(str(fault)[:200])
            continue
        start, end = doc["from"], doc["to"]
        if not was_stub and printed["period"] and printed["period"] != (start, end):
            # A statement generated later for the same account and end date reuses the document_key
            # (the key holds the end date only), so its PDF replaces the scheduled one in S3 while
            # the row keeps the scheduled period. The PDF is judged over the period it prints.
            findings.append(_finding(
                "a document's PDF covers the period recorded on its row", doc["account"],
                "document {} records {} to {} and its PDF prints {} to {}; another statement for "
                "the same account and end date replaced it under the same document_key".format(
                    doc["uid"], start, end, printed["period"][0], printed["period"][1]),
                "{} to {}".format(start, end),
                "{} to {}".format(*printed["period"])))
            start, end = printed["period"]
            try:
                ledger_after = ledger(account, start, end)
                rate_after = product_rate(account, min(end, clock.today()))
            except RuntimeError as fault:
                stats.setdefault("errors", []).append(str(fault)[:200])
                continue
            if ledger_after[0] is None:
                continue
        stats["judged"] = stats.get("judged", 0) + 1
        lines, opening, closing = ledger_after
        for rule, detail, expected, actual in judge(account, start, end, printed,
                                                    lines, opening, closing, rate_after, key):
            findings.append(_finding(rule, doc["account"], detail, expected, actual))
    return findings


def parse_certificate(text):
    """The values an annual interest certificate or tax summary prints, None where missing.

    The Direct template (statement/annual-interest-certificate.html) prints the document date under
    the banner, 'during the tax year ending d MMMM yyyy', and one table row of the date and three
    amounts: gross interest, income tax deducted, actual amount received. The Trust template
    prints 'dd MMM yyyy' for the year end and the document date as yyyy-mm-dd.
    """
    found = {}
    ending = re.search(r"tax year ending\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
    found["ending"] = None
    if ending:
        for pattern in ("%d %B %Y", "%d %b %Y"):
            try:
                found["ending"] = datetime.strptime(" ".join(ending.groups()), pattern).date()
                break
            except ValueError:
                continue
    row = re.search(r"(?:{}|(\d{{4}}-\d\d-\d\d))\s+{}\s+{}\s+{}".format(DAY, MONEY, MONEY, MONEY), text)
    found["row"] = None
    if row:
        groups = row.groups()
        printed = groups[0] or groups[1]
        day = (datetime.strptime(printed, "%d/%m/%Y") if groups[0]
               else datetime.strptime(printed, "%Y-%m-%d")).date()

        def money(sign, digits):
            value = Decimal(digits.replace(",", ""))
            return -value if sign == "-" else value
        found["row"] = {"day": day, "gross": money(*groups[2:4]), "tax": money(*groups[4:6]),
                        "received": money(*groups[6:8])}
    found["title"] = re.search(r"Annual (Interest Certificate|Tax Summary)", text) is not None
    return found


def _certificate_interest(customer_uid, start, end):
    rows, error = _psql("""
      SELECT COALESCE(sum(t.customer_amount), 0)
      FROM account_transaction t
      JOIN customer_account ca ON ca.sid = t.customer_account_sid
      WHERE ca.platform_customer_sid = (SELECT sid FROM platform_customer WHERE uid = '{}')
        AND ca.currency = 'GBP' AND ca.tax_wrapper = 'NONE'
        AND t.transaction_type = 'INTEREST' AND t.value_date BETWEEN '{}' AND '{}'
      """.format(_uuid(customer_uid), start, end))
    return _d(rows[0][0]) if rows else None


def certificate_findings(platform, ops, sample, stats):
    """Closure certificate presence for closed customers, and the interest on a sample of certificates."""
    findings = []
    closed, error = _psql("""
      SELECT pc.uid, pc.external_id, pc.closed_at::date FROM platform_customer pc
      JOIN partner_platform pp ON pp.sid = pc.platform_sid
      WHERE pp.uid = '{}' AND pc.verification_status = 'CLOSED'""".format(_uuid(platform)))
    if closed is None:
        stats.setdefault("errors", []).append(error)
        return findings
    docs, error = _documents(platform, "ANNUAL_TAX_SUMMARY")
    if docs is None:
        stats.setdefault("errors", []).append(error)
        return findings
    stats["closedCustomers"] = stats.get("closedCustomers", 0) + len(closed)
    stats["certificates"] = stats.get("certificates", 0) + len(docs)
    by_customer = {}
    for doc in docs:
        by_customer.setdefault(doc["customer"], []).append(doc)
    without = [c for c in closed if c[0] not in by_customer]
    stats["closedWithoutCertificate"] = stats.get("closedWithoutCertificate", 0) + len(without)
    if REQUIRE_DIRECT_CLOSURE_CERTIFICATE:
        for uid, external, closed_at in without[:LIMIT]:
            findings.append(_finding(
                "a closed customer has a closure tax certificate", uid,
                "customer {} closed on {} has no ANNUAL_TAX_SUMMARY document".format(external, closed_at),
                "a document", "none"))
    # Rows made by ops carry no parameters: the period is the tax year ending on the document date.
    for doc in docs:
        if not doc["params"]:
            doc["to"] = doc["date"]
            doc["from"] = tax_year_start(doc["date"])
    pool = list(docs)
    random.shuffle(pool)
    for doc in pool[:sample]:
        stats["certificatesSampled"] = stats.get("certificatesSampled", 0) + 1
        subject = doc["customer"]
        if doc["from"] != tax_year_start(doc["to"]):
            findings.append(_finding(
                "a tax certificate starts on the 6 April before its end date", subject,
                "document {} runs {} to {}".format(doc["uid"], doc["from"], doc["to"]),
                tax_year_start(doc["to"]), doc["from"]))
        if not doc["generated"]:
            ok, error = _render(ops, doc, stats)
            if not ok:
                stats.setdefault("errors", []).append(error)
                continue
        generated, key = _generated(doc["uid"])
        try:
            printed = parse_certificate(text_of(key))
        except (RuntimeError, OSError, subprocess.SubprocessError) as fault:
            stats.setdefault("errors", []).append(str(fault)[:200])
            continue
        expected = _certificate_interest(doc["customer"], doc["from"], doc["to"])
        stats["certificatesJudged"] = stats.get("certificatesJudged", 0) + 1
        where = "certificate {} for customer {} over {} to {}".format(
            doc["uid"], doc["reference"], doc["from"], doc["to"])
        row = printed["row"]
        if row is None or printed["ending"] is None:
            findings.append(_finding(
                "a tax certificate prints its table and tax year end", subject,
                "{} has no readable interest row or tax-year-ending sentence".format(where),
                "a row", "none"))
            continue
        if printed["ending"] != doc["to"]:
            findings.append(_finding(
                "a tax certificate names the tax year end it covers", subject,
                "{} says the tax year ends {}".format(where, printed["ending"]), doc["to"],
                printed["ending"]))
        if expected is not None:
            for label, value in (("gross interest", row["gross"]), ("amount received", row["received"])):
                if value.compare(expected.quantize(Decimal("0.01"))) != 0:
                    findings.append(_finding(
                        "a tax certificate's interest is the INTEREST booked in its period", subject,
                        "{} prints {} of {}, and the customer's INTEREST rows with value dates in "
                        "the period add up to {}".format(where, label, value, expected),
                        expected.quantize(Decimal("0.01")), value))
        if row["tax"] != ZERO:
            findings.append(_finding(
                "a tax certificate shows no tax deducted", subject,
                "{} prints income tax deducted of {}".format(where, row["tax"]), ZERO, row["tax"]))
    return findings


def check(ops, platform_uid=None, sample=3):
    """Hold the platform's scheduled documents against the code and answer (findings, stats)."""
    stats = {"platforms": 0}
    platforms, error = _platforms(platform_uid)
    if platforms is None:
        return [], dict(stats, error=error)
    findings = []
    for platform in platforms:
        stats["platforms"] += 1
        docs, error = _documents(platform, "MONTHLY_STATEMENT")
        if docs is None:
            stats.setdefault("errors", []).append(error)
            continue
        rows, error = _psql("""
          SELECT e.last_raised FROM platform_event_schedule e
          JOIN partner_platform pp ON pp.sid = e.platform_sid
          WHERE pp.uid = '{}' AND e.event_type = 'STATEMENTS'""".format(_uuid(platform)))
        schedule = {"last_raised": _day(rows[0][0])} if rows and rows[0][0] else None
        findings += statement_findings(platform, docs, schedule, stats)
        findings += sampled_statements(ops, docs, sample, stats)
        findings += certificate_findings(platform, ops, sample, stats)
    stats["findings"] = len(findings)
    return findings, stats


if __name__ == "__main__":
    import json
    import sys

    from explorer import config, world
    from explorer.client import BearerClient

    settings = config.load("local")
    found, stats = check(BearerClient(settings["ops_base_url"], world.ops_token(settings)),
                         sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != "-" else None,
                         sample=int(sys.argv[2]) if len(sys.argv) > 2 else 3)
    print(json.dumps(stats, indent=1, default=str))
    for f in found:
        print(json.dumps(f, default=str))
