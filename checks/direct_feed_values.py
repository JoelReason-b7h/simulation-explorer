"""Hold the values in a Direct bank's feed files against core and compliance.

    python3 checks/direct_feed_values.py <bankUid> [--days 2] [--limit 600] [--json out.json]

checks/direct_feed.py checks the files against themselves and against each other. This module reads
the already-fetched files in feeds/direct/<bankUid>/ (it never fetches: a fetch moves the files out
of S3) and takes the newest row of each CUSTOMER, ACCOUNT and PRODUCT in the last --days days of
files, plus every TRANSACTION row in them, and recomputes each column from core's tables and from
compliance, by the mapping the collectors, specs and file generators state. It does not call the
feed's own queries.

A row is only judged against core as it stands now when core shows no change to that entity after
the file's extract_window_end_at (the extract time the collectors stamp the file with; they read
the database after it). When core did change after the extract:
- if no later sealed file of that type exists beyond the change plus EXTRACT_GAP, the row is
  "pending": the feed has not run since, so it is skipped and counted;
- otherwise a later run should have re-sent the entity, so the row is judged and a difference is
  reported as a stale row under a rule whose name ends "(stale)".
Balances are left to direct_feed.check_account_balance_against_core (finding 33).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from direct_feed import CACHE, File  # noqa: E402
from explorer.statement_oracle import aer as oracle_aer  # noqa: E402

CORE_DSN = os.environ.get("SIM_CORE_DSN", "postgresql://core:password@localhost:5432/core")
COMPLIANCE_DSN = os.environ.get("SIM_COMPLIANCE_DSN",
                                "postgresql://compliance:password@localhost:5432/compliance")
EXTRACT_GAP = 60
LONDON = "Europe/London"
CHUNK = 500
TITLES = {"Prof": "Professor", "Dame": "Lady"}
TITLE_NAMES = {"mr": "Mr", "mrs": "Mrs", "miss": "Miss", "ms": "Ms", "mx": "Mx", "dr": "Dr",
               "prof": "Professor", "professor": "Professor", "sir": "Sir", "dame": "Lady",
               "lady": "Lady", "lord": "Lord"}
CLIENT_STATUS = {"ACTIVATED": "ACTIVATED", "DEACTIVATED": "DEACTIVATED", "FROZEN": "BLOCKED",
                 "CANCELLED": "CANCELLED", "CLOSED": "CLOSED"}
RISK = {"LOW_RISK": "Low", "MEDIUM_RISK": "Medium", "HIGH_RISK": "High", "UNKNOWN": "NotSet",
        "PROHIBITED": "SensitiveHigh"}
EMPLOYMENT = {"EMPLOYED", "SELF_EMPLOYED", "RETIRED", "STUDENT", "HOUSE_MAKER", "UNEMPLOYED"}
ACCOUNT_STATUS = {"OPEN": "OPEN", "CLOSING": "CLOSED", "CLOSED": "CLOSED", "REQUESTED": "REQUESTED",
                  "CANCELLED": "CANCELLED"}
CLOSURE = {"MOVING_FUNDS_TO_ISA": "MOVING_FUNDS_TO_ISA_PRODUCT",
           "BALANCE_BELOW_MINIMUM": "BALANCE_BELOW_MINIMUM_AMOUNT",
           "NEED_FUNDS_FOR_OTHER_PURPOSE": "NEED_MONEY_FOR_ANOTHER_PURPOSE",
           "UNHAPPY_WITH_INTEREST_RATE": "UNHAPPY_WITH_INTEREST_RATE",
           "UNHAPPY_WITH_SERVICE": "UNHAPPY_WITH_SERVICE",
           "UNHAPPY_WITH_PRODUCT_FEATURES": "UNHAPPY_WITH_PRODUCT_FEATURES",
           "NO_LONGER_NEEDED": "NO_LONGER_USE_OR_NEED_ACCOUNT",
           "NO_LONGER_ELIGIBLE": "NO_LONGER_ELIGIBLE",
           "OPENED_WRONG_ACCOUNT_BY_MISTAKE": "OPENED_WRONG_ACCOUNT_BY_MISTAKE"}
PRODUCT_TYPE = {"INSTANT": "Call", "NOTICE": "Notice", "TERM": "Fixed", "SAYE": "Fixed"}
PRODUCT_STATE = {"ACTIVE": "Open", "CLOSED": "Closed", "SOFT_CLOSED": "Soft_closed"}
FREQUENCY = {"END_OF_DAY": "DAILY", "END_OF_MONTH": "MONTHLY", "START_OF_CALENDAR_MONTH": "MONTHLY",
             "END_OF_CALENDAR_MONTH": "MONTHLY", "MONTHLY_PAYOUT": "MONTHLY",
             "AT_MATURITY": "AT_MATURITY", "AT_WITHDRAWAL": "AT_MATURITY"}


def psql(dsn, sql, timeout=240):
    env = dict(os.environ, PGOPTIONS="-c default_transaction_read_only=on")
    done = subprocess.run(["psql", dsn, "-tA", "-v", "ON_ERROR_STOP=1", "-c", sql],
                          capture_output=True, text=True, timeout=timeout, env=env)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip()[:400])
    return [line for line in done.stdout.splitlines() if line.strip()]


def jrows(dsn, sql):
    return [json.loads(line, parse_float=Decimal) for line in psql(dsn, sql)]


def arr(uids):
    return "ARRAY[{}]::uuid[]".format(",".join("'{}'".format(u) for u in uids))


def chunks(items, n=CHUNK):
    items = list(items)
    for i in range(0, len(items), n):
        yield items[i:i + n]


def text(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def num(value):
    try:
        return Decimal(value) if value not in (None, "") else None
    except InvalidOperation:
        return None


def same(column, expected, actual):
    """Numbers compare by value (compareTo), everything else as text."""
    if column in NUMERIC and expected not in (None, "") and actual not in (None, ""):
        e, a = num(expected), num(actual)
        return e is not None and a is not None and e == a
    return text(expected) == text(actual)


NUMERIC = {"Salary", "GrossInterestRate", "MinBalanceAmount", "MaxBalanceAmount",
           "MaxAvailableDepositAmount", "AmountOnNotice", "AccruedInterestAmount", "Amount",
           "UpdatedBalance"}


def stamp(epoch):
    return datetime.fromtimestamp(int(Decimal(epoch)), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def london_date(epoch):
    return psql(CORE_DSN, "SELECT (to_timestamp({}) AT TIME ZONE '{}')::date".format(epoch, LONDON))[0]


class Run:
    def __init__(self, bank_uid, days, limit):
        self.bank_uid, self.days, self.limit = bank_uid, days, limit
        self.findings = []
        self.stats = defaultdict(Counter)
        self.extract = {}
        self.latest_sealed = {}

    def add(self, rule, subject, detail, expected, actual, stale=False):
        self.findings.append({"rule": rule + (" (stale)" if stale else ""), "subject": subject,
                              "detail": detail, "expected": text(expected), "actual": text(actual)})

    def load_extracts(self):
        rows = psql(CORE_DSN, "SELECT f.file_name, f.file_type, extract(epoch FROM f.extract_window_end_at), "
                              "f.business_effective_date FROM investec_file f JOIN partner_bank b ON "
                              "b.sid = f.bank_sid WHERE b.uid = '{}' AND f.control_total_sha256 IS NOT NULL "
                              "AND f.file_name IS NOT NULL".format(self.bank_uid))
        for line in rows:
            name, kind, at, business = line.split("|")
            self.extract[name] = (Decimal(at), business)
            if kind not in self.latest_sealed or Decimal(at) > self.latest_sealed[kind][0]:
                self.latest_sealed[kind] = (Decimal(at), business)

    ENTRY = {"CUSTOMER": ("investec_file_customer_entry", "customer_sid", "platform_customer"),
             "ACCOUNT": ("investec_file_account_entry", "account_sid", "customer_product_account"),
             "PRODUCT": ("investec_file_product_entry", "product_sid", "bank_product")}

    def newest_entry(self, entity, uids):
        """uid -> extract instant of the newest sealed file that carries an entry for it. A file the
        local cache has not fetched yet is still in S3, and its rows are the newest ones."""
        table, column, parent = self.ENTRY[entity]
        found = {}
        for part in chunks(uids):
            for r in jrows(CORE_DSN, "SELECT json_build_object('uid', x.uid, 'at', extract(epoch FROM "
                           "max(f.extract_window_end_at))) FROM {t} e JOIN investec_file f ON f.sid = e.file_sid "
                           "AND f.file_type = '{e}'::investec_file_type AND f.control_total_sha256 IS NOT NULL "
                           "JOIN {p} x ON x.sid = e.{c} WHERE x.uid = ANY({u}) GROUP BY x.uid".format(
                               t=table, e=entity, p=parent, c=column, u=arr(part))):
                found[r["uid"]] = r["at"]
        return found

    def superseded(self, entity, file_name, uid, newest):
        at = self.extract.get(file_name, (None,))[0]
        return at is not None and uid in newest and newest[uid] > at

    def verdict(self, entity, file_name, changed_at):
        """judged / stale / pending / unknown for an entity row whose newest change is changed_at."""
        if file_name not in self.extract:
            return "unknown"
        at = self.extract[file_name][0]
        if changed_at is None or Decimal(changed_at) <= at:
            return "judged"
        latest = self.latest_sealed.get(entity, (Decimal(0), None))[0]
        return "stale" if latest > Decimal(changed_at) + EXTRACT_GAP else "pending"


def newest_rows(bank_uid, days, limit):
    folder = CACHE / "direct" / bank_uid
    paths = sorted(folder.glob("*.csv"), key=lambda p: p.name.split("_", 1)[-1])
    if not paths:
        raise SystemExit("no files in {}".format(folder))
    newest = paths[-1].name.split("_", 1)[-1][:8]
    horizon = (datetime.strptime(newest, "%Y%m%d") - timedelta(days=days)).strftime("%Y%m%d")
    wanted = [p for p in paths if p.name.split("_", 1)[-1][:8] >= horizon]
    by_entity = {"CUSTOMER": {}, "ACCOUNT": {}, "PRODUCT": {}}
    transactions = []
    ids = {"CUSTOMER": "CustomerId", "ACCOUNT": "AccountId", "PRODUCT": "ProductId"}
    for p in wanted:
        f = File(p)
        if f.entity in by_entity:
            for r in f.rows:
                by_entity[f.entity][r[ids[f.entity]]] = (r, f)
        elif f.entity == "TRANSACTION":
            transactions.extend((r, f) for r in f.rows)
    for entity, rows in by_entity.items():
        by_entity[entity] = dict(list(rows.items())[-limit:])
    return by_entity, transactions[-limit * 5:], len(wanted)


# ---------------------------------------------------------------------------------------- CUSTOMER

def wrap_lines(line1, line2):
    """InvestecCustomerSpec.wrapAddressLines: 40 characters a line."""
    def cut(v):
        return v if v is None or len(v) <= 40 else v[:40]
    if len(line1) <= 40:
        return line1, cut(line2)
    if line2 is None or not line2.strip():
        space = line1.rfind(" ", 0, 41)
        split = space if space > 0 else 40
        head, tail = line1[:split].rstrip(), line1[split:].lstrip()
        return head, (cut(tail) if tail else None)
    return cut(line1), cut(line2)


def gb_parts(identifier):
    """AccountIdentifier.getGbSortCode / getGbAccountNumber, or (None, None) when not a GB account."""
    if not identifier:
        return None, None
    kind, value = identifier.get("type"), identifier.get("value") or ""
    country = identifier.get("countryCode")
    if kind == "GB_SCAN":
        short = value
    elif kind == "GB_BBAN":
        short = value[4:22]
    elif kind == "IBAN" and country == "GB":
        short = value[4:22][4:]
    else:
        return None, None
    return short[:6], short[6:]


CORE_CUSTOMER_SQL = """
SELECT json_build_object(
  'cust', pc.uid, 'person', pp.uid, 'title', pp.title, 'first', pp.first_name, 'middle', pp.middle_name,
  'last', pp.last_name, 'dob', pp.date_of_birth, 'country', a.country, 'nat1', pp.nationality,
  'nat2', pp.second_nationality, 'nat3', pp.third_nationality, 'nino', pp.nino, 'email', pp.email,
  'phone', pp.phone_number, 'l1', a.address_line_1, 'l2', a.address_line_2, 'town', a.address_line_3,
  'county', a.address_line_4, 'post', a.post_code, 'status', pc.verification_status,
  'start', (pc.created_at AT TIME ZONE '{tz}')::date, 'end', (pc.closed_at AT TIME ZONE '{tz}')::date,
  'emp', pp.employment_status, 'salary', pp.annual_income, 'industry', pp.industry,
  'fscs', extract(epoch FROM pc.fscs_acknowledged_at), 'tags', pc.tags, 'dist', ppl.uid,
  'distname', ppl.legal_name,
  'tax', (SELECT json_agg(json_build_array(t.tax_country, t.tax_code) ORDER BY t.created_at, t.sid)
          FROM tax_residency t WHERE t.platform_person_sid = pp.sid AND t.tax_country <> 'GBR'),
  'nominated', (SELECT pa.account_identifier FROM cash_account_nominated_account_link canal
                JOIN payee_account pa ON pa.sid = canal.payee_account_sid
                WHERE canal.customer_sid = pc.sid AND canal.currency = 'GBP' AND canal.is_active
                ORDER BY canal.updated_at DESC, canal.sid DESC LIMIT 1),
  'c_person', extract(epoch FROM pp.updated_at),
  'c_status', (SELECT extract(epoch FROM max(h.transitioned_at)) FROM platform_customer_status_history h
               WHERE h.platform_customer_sid = pc.sid),
  'c_closed', extract(epoch FROM pc.closed_at),
  'c_tax', (SELECT extract(epoch FROM max(t.created_at)) FROM tax_residency t
            WHERE t.platform_person_sid = pp.sid),
  'c_nom', (SELECT extract(epoch FROM max(l.updated_at)) FROM cash_account_nominated_account_link l
            WHERE l.customer_sid = pc.sid))
FROM platform_customer pc
JOIN platform_person pp ON pp.sid = pc.platform_person_id
LEFT JOIN address a ON a.sid = pp.address_sid
JOIN partner_platform ppl ON ppl.sid = pc.platform_sid
WHERE pc.uid = ANY({uids})
"""

# The risk CASE is PersonComplianceProfileRepository's: a customer override that is newer than the
# person's own override and the person's KYC update wins over the person's risk status.
COMPLIANCE_SQL = """
WITH ids AS (SELECT unnest({uids}) AS uid),
persons AS (SELECT p.* FROM person p JOIN ids ON ids.uid = p.person_uid),
cust AS (
  SELECT DISTINCT ON (p.sid) p.sid AS person_sid, c.customer_uid, c.risk_status, c.updated_at
  FROM persons p JOIN customer_person_link cpl ON cpl.person_sid = p.sid
  JOIN customer c ON c.sid = cpl.customer_sid ORDER BY p.sid, c.sid),
k AS (
  SELECT kyc_data ->> 'entityUid' AS entity, kyc_data ->> 'type' AS type, check_status, created_at,
         updated_at AT TIME ZONE 'UTC' AS updated_utc, updated_at
  FROM kyc_check
  WHERE kyc_data ->> 'entityUid' IN (SELECT uid::text FROM ids UNION SELECT customer_uid::text FROM cust))
SELECT json_build_object(
  'person', p.person_uid, 'pep', p.pep_check_status,
  'risk', CASE WHEN co.changed IS NOT NULL
                AND (greatest(po.changed, p.kyc_checks_updated_at) IS NULL
                     OR co.changed > greatest(po.changed, p.kyc_checks_updated_at))
               THEN cust.risk_status ELSE p.risk_status END,
  'vuln', p.vulnerable_client_flag,
  'kyc', (SELECT k.updated_at::date FROM k WHERE k.entity = p.person_uid::text
          AND k.check_status = 'COMPLETED' ORDER BY k.updated_at DESC LIMIT 1),
  'c_comp', extract(epoch FROM greatest(p.kyc_checks_updated_at, cust.updated_at,
        (SELECT max(greatest(k.created_at, k.updated_utc)) FROM k
         WHERE k.entity IN (p.person_uid::text, cust.customer_uid::text)))))
FROM persons p
LEFT JOIN cust ON cust.person_sid = p.sid
LEFT JOIN LATERAL (SELECT max(k.created_at) AS changed FROM k WHERE k.entity = p.person_uid::text
                   AND k.type = 'PersonOverrideKycCheckData') po ON true
LEFT JOIN LATERAL (SELECT max(k.created_at) AS changed FROM k WHERE k.entity = cust.customer_uid::text
                   AND k.type = 'CustomerOverrideKycCheckData') co ON true
"""


def customer_expected(c, comp):
    """The CUSTOMER columns core and compliance give, or a reason the row cannot be in a feed."""
    if comp is None:
        return None, "no compliance profile"
    status = CLIENT_STATUS.get(c["status"])
    if c["status"] == "PENDING":
        return None, "clientStatus PENDING is excluded"
    pep = {"NO_MATCH": "false", "TRUE_MATCH": "true"}.get(comp["pep"])
    risk = RISK.get(comp["risk"])
    title = TITLE_NAMES.get((c["title"] or "").strip().lower())
    line1, line2 = wrap_lines(c["l1"] or "", c["l2"])
    tax = c["tax"] or []
    t1 = tax[0] if len(tax) > 0 else [None, None]
    t2 = tax[1] if len(tax) > 1 else [None, None]
    sort, number = gb_parts(c["nominated"])
    tags = c["tags"] if isinstance(c["tags"], dict) else {}
    flat = {k: v for k, v in tags.items() if v is None or isinstance(v, (str, int, float, bool))}
    salary = num(c["salary"])
    e = {
        "Title": title, "FirstName": c["first"], "MiddleName": c["middle"], "LastName": c["last"],
        "DateOfBirth": c["dob"], "CountryOfResidence": c["country"], "Nationality1": c["nat1"],
        "Nationality2": c["nat2"], "Nationality3": c["nat3"], "TaxResidency1": t1[0], "TIN1": t1[1],
        "TIN1UnavailableDeclared": None if t1[0] is None else (t1[1] is None),
        "TaxResidency2": t2[0], "TIN2": t2[1],
        "TIN2UnavailableDeclared": None if t2[0] is None else (t2[1] is None),
        "NationalInsuranceNumber": c["nino"], "Email": c["email"], "MobileNumber": c["phone"],
        "KYCApprovedDate": comp["kyc"], "RelationshipStartDate": c["start"],
        "RelationshipEndDate": c["end"],
        "EmploymentStatus": c["emp"] if c["emp"] in EMPLOYMENT else None,
        "Salary": None if salary is None else salary.quantize(Decimal("0.01"), ROUND_HALF_UP),
        "PEPExposure": pep, "ClientStatus": status, "AddressLine1": line1, "AddressLine2": line2,
        "Town": c["town"], "County": c["county"], "PostalCode": c["post"], "Country": c["country"],
        "NominatedAccountNumber": number, "NominatedSortCode": sort, "RiskRating": risk,
        "VulnerableClientFlag": comp["vuln"],
        "FSCSAcceptedDate": None if c["fscs"] is None else stamp(c["fscs"]),
        "DistributorId": c["dist"], "DistributorName": c["distname"],
        "ClientMetaData": json.dumps(flat, separators=(",", ":")) if flat else None,
    }
    return e, None


COMPLIANCE_COLUMNS = {"KYCApprovedDate", "PEPExposure", "RiskRating", "VulnerableClientFlag"}


def check_customers(run, rows):
    entity = "CUSTOMER"
    uids = list(rows)
    core, comp = {}, {}
    for part in chunks(uids):
        for c in jrows(CORE_DSN, CORE_CUSTOMER_SQL.format(tz=LONDON, uids=arr(part))):
            core[c["cust"]] = c
    persons = [c["person"] for c in core.values()]
    for part in chunks(persons):
        for p in jrows(COMPLIANCE_DSN, COMPLIANCE_SQL.format(uids=arr(part))):
            comp[p["person"]] = p
    newest = run.newest_entry(entity, uids)
    for uid, (row, f) in rows.items():
        s = run.stats[entity]
        s["rows"] += 1
        if run.superseded(entity, f.name, uid, newest):
            s["superseded_in_s3"] += 1
            continue
        c = core.get(uid)
        if c is None:
            run.add("a CUSTOMER row has a platform_customer in core", "CUSTOMER " + uid + " " + f.name,
                    "no platform_customer", "a row", "none")
            s["findings_rows"] += 1
            continue
        p = comp.get(c["person"])
        changed = max([float(c[k]) for k in ("c_person", "c_status", "c_closed", "c_tax", "c_nom", "fscs")
                       if c.get(k) is not None] +
                      ([float(p["c_comp"])] if p and p.get("c_comp") is not None else []) or [None],
                      default=None)
        verdict = run.verdict(entity, f.name, changed)
        s[verdict] += 1
        if verdict in ("pending", "unknown"):
            continue
        expected, why = customer_expected(c, p)
        if expected is None:
            run.add("a CUSTOMER row is for a customer the collector would send",
                    "CUSTOMER " + uid + " " + f.name, why, "no row", "a row", verdict == "stale")
            s["findings_rows"] += 1
            continue
        bad = False
        for column, want in expected.items():
            got = row.get(column, "")
            if column == "ClientMetaData":
                try:
                    ok = (json.loads(got) if got else None) == (json.loads(want) if want else None)
                except ValueError:
                    ok = False
            else:
                ok = same(column, want, got)
            s["columns"] += 1
            if not ok:
                bad = True
                note = ""
                if verdict == "stale" and column in COMPLIANCE_COLUMNS:
                    note = " (compliance fields are left out of CustomerCanonicaliserV1's hash, so a" \
                           " compliance change does not re-send the row)"
                run.add("CUSTOMER {} equals core".format(column), "CUSTOMER {} {}".format(uid, f.name),
                        "feed {} vs core now{}".format(column, note), text(want), text(got),
                        verdict == "stale")
        s["findings_rows"] += 1 if bad else 0


# ----------------------------------------------------------------------------------------- ACCOUNT

CORE_ACCOUNT_SQL = """
SELECT json_build_object(
  'acct', cpa.uid, 'cust', pc.uid, 'prod', bp.uid, 'ptype', bp.product_type, 'status', dca.status,
  'ident', eia.account_identifier, 'upd', extract(epoch FROM dca.updated_at),
  'status_eff', (dca.updated_at AT TIME ZONE '{tz}')::date,
  'open', (cpa.created_at AT TIME ZONE '{tz}')::date, 'reason', dca.closure_reason,
  'tcs', extract(epoch FROM dca.terms_and_conditions_accepted_at), 'due', cpam.due_date,
  'c_mat', extract(epoch FROM cpam.updated_at),
  'notice_total', (SELECT sum(dci.amount) FILTER (WHERE dci.instruction_type = 'WITHDRAWAL'
                   AND dcan.processed_at IS NULL)
                   FROM direct_customer_account_notice dcan
                   JOIN direct_customer_instruction dci ON dci.sid = dcan.direct_instruction_sid
                   WHERE dci.direct_customer_account_sid = dca.sid),
  'notice_date', (SELECT (max(dcan.created_at) FILTER (WHERE dcan.processed_at IS NULL)
                          AT TIME ZONE '{tz}')::date
                  FROM direct_customer_account_notice dcan
                  JOIN direct_customer_instruction dci ON dci.sid = dcan.direct_instruction_sid
                  WHERE dci.direct_customer_account_sid = dca.sid),
  'c_notice', (SELECT extract(epoch FROM max(greatest(dcan.created_at, dcan.processed_at, dci.updated_at)))
               FROM direct_customer_account_notice dcan
               JOIN direct_customer_instruction dci ON dci.sid = dcan.direct_instruction_sid
               WHERE dci.direct_customer_account_sid = dca.sid),
  'c_instr', (SELECT extract(epoch FROM max(greatest(dci.created_at, dci.updated_at)))
              FROM direct_customer_instruction dci WHERE dci.direct_customer_account_sid = dca.sid),
  'close_request', (SELECT (min(dci.created_at) AT TIME ZONE '{tz}')::date FROM direct_customer_instruction dci
                    WHERE dci.direct_customer_account_sid = dca.sid AND dci.full_balance_withdrawal),
  'c_accrual', (SELECT extract(epoch FROM max(ia.created_at)) FROM interest_accrual ia
                WHERE ia.customer_product_account_sid = cpa.sid),
  'c_realised', (SELECT extract(epoch FROM max(ir.created_at)) FROM interest_realised ir
                 WHERE ir.customer_product_account_sid = cpa.sid))
FROM customer_product_account cpa
JOIN direct_customer_account dca ON dca.customer_product_account_sid = cpa.sid
JOIN entity_internal_account eia ON eia.sid = dca.entity_internal_account_sid
JOIN customer_account ca ON ca.sid = cpa.customer_account_sid
JOIN platform_customer pc ON pc.sid = ca.platform_customer_sid
JOIN platform_product pp ON pp.sid = cpa.platform_product_sid
JOIN bank_product bp ON bp.sid = pp.product_sid
LEFT JOIN customer_product_account_maturity cpam ON cpam.customer_product_account_sid = cpa.sid
WHERE cpa.uid = ANY({uids})
"""

# The latest accrual not yet realised as of the file's business date; the customer pot's
# running_accrual, and its value date, are what the feed calls accrued interest.
ACCRUAL_SQL = """
SELECT json_build_object('acct', cpa.uid, 'running', iam.running_accrual, 'vd', la.value_date,
  'fallback', greatest(DATE '{bd}' - 1, (cpa.created_at AT TIME ZONE '{tz}')::date))
FROM customer_product_account cpa
LEFT JOIN LATERAL (
  SELECT ia.sid, ia.value_date FROM interest_accrual ia
  LEFT JOIN interest_realised ir ON ir.sid = ia.realised_interest_sid
  WHERE ia.customer_product_account_sid = cpa.sid AND ia.value_date <= DATE '{bd}'
    AND (ia.realised_interest_sid IS NULL OR ir.value_date > DATE '{bd}')
  ORDER BY ia.value_date DESC, ia.created_at DESC, ia.sid DESC LIMIT 1) la ON true
LEFT JOIN interest_accrual_amount iam ON iam.interest_accrual_sid = la.sid AND iam.pot_type = 'CUSTOMER'
WHERE cpa.uid = ANY({uids})
"""


def check_accounts(run, rows):
    entity = "ACCOUNT"
    uids = list(rows)
    core = {}
    for part in chunks(uids):
        for a in jrows(CORE_DSN, CORE_ACCOUNT_SQL.format(tz=LONDON, uids=arr(part))):
            core[a["acct"]] = a
    by_date = defaultdict(list)
    for uid, (row, f) in rows.items():
        by_date[f.business_date[:10]].append(uid)
    accrual = {}
    for bd, ids in by_date.items():
        for part in chunks(ids):
            for r in jrows(CORE_DSN, ACCRUAL_SQL.format(bd=bd, tz=LONDON, uids=arr(part))):
                accrual[(r["acct"], bd)] = r
    newest = run.newest_entry(entity, uids)
    for uid, (row, f) in rows.items():
        s = run.stats[entity]
        s["rows"] += 1
        if run.superseded(entity, f.name, uid, newest):
            s["superseded_in_s3"] += 1
            continue
        a = core.get(uid)
        if a is None:
            run.add("an ACCOUNT row has an account in core", "ACCOUNT " + uid + " " + f.name,
                    "no direct_customer_account", "a row", "none")
            s["findings_rows"] += 1
            continue
        changed = max([float(a[k]) for k in ("upd", "c_mat", "c_notice", "c_instr", "c_accrual", "c_realised", "tcs")
                       if a.get(k) is not None], default=None)
        verdict = run.verdict(entity, f.name, changed)
        s[verdict] += 1
        if verdict in ("pending", "unknown"):
            continue
        bd = f.business_date[:10]
        if verdict == "stale":
            bd = run.latest_sealed[entity][1] or bd
            accrual.setdefault((uid, bd), None)
        acc = accrual.get((uid, bd))
        if acc is None and verdict == "stale":
            acc = jrows(CORE_DSN, ACCRUAL_SQL.format(bd=bd, tz=LONDON, uids=arr([uid])))[0]
            accrual[(uid, bd)] = acc
        running = num(acc["running"]) if acc else None
        accrued = (running if running is not None else Decimal(0)).quantize(Decimal("0.01"), ROUND_HALF_EVEN)
        sort, number = gb_parts(a["ident"])
        closing = a["status"] in ("CLOSING", "CLOSED")
        expected = {
            "CustomerId": a["cust"], "ProductId": a["prod"], "AccountNumber": number, "SortCode": sort,
            "AmountOnNotice": (num(a["notice_total"]) or Decimal(0)) if a["ptype"] == "NOTICE" else None,
            "AccruedInterestAmount": accrued,
            "AccruedInterestDate": (acc["vd"] if acc and acc["vd"] else acc["fallback"]) if acc else None,
            "StatusCode": ACCOUNT_STATUS.get(a["status"]), "StatusEffectiveDate": a["status_eff"],
            "AccountOpenDate": a["open"], "AccountCloseDate": a["status_eff"] if closing else None,
            "ClosureReason": CLOSURE.get(a["reason"]) if a["reason"] else None,
            "MaturityDate": a["due"], "NoticeRequestedDate": a["notice_date"],
            "TermsAndConditionsAcceptedDate": None if a["tcs"] is None else stamp(a["tcs"]),
        }
        bad = False
        for column, want in expected.items():
            got = row.get(column, "")
            s["columns"] += 1
            if not same(column, want, got):
                bad = True
                run.add("ACCOUNT {} equals core".format(column), "ACCOUNT {} {}".format(uid, f.name),
                        "feed {} vs core as of business date {}".format(column, bd),
                        text(want), text(got), verdict == "stale")
        # direct_customer_account.updated_at is not set by a status update, so the feed's close date
        # and status date (both read from it) cannot move with the status. The close request, the
        # full-balance withdrawal instruction, is dated independently.
        if closing and a["close_request"] and row.get("StatusCode") == "CLOSED":
            s["close_dates_checked"] += 1
            for column in ("AccountCloseDate", "StatusEffectiveDate"):
                if row.get(column) and row[column] < a["close_request"]:
                    bad = True
                    run.add("ACCOUNT {} is on or after the close request".format(column),
                            "ACCOUNT {} {}".format(uid, f.name),
                            "core dates the close request (full-balance withdrawal instruction) {}; "
                            "AccountOpenDate {}".format(a["close_request"], row.get("AccountOpenDate")),
                            ">= " + a["close_request"], row[column])
        s["findings_rows"] += 1 if bad else 0


# ----------------------------------------------------------------------------------------- PRODUCT

CORE_PRODUCT_SQL = """
SELECT json_build_object(
  'prod', bp.uid, 'ptype', bp.product_type, 'cur', bp.currency, 'name', bp.name, 'gross', prv.gross_rate,
  'aer', prv.aer_rate, 'min', bp.deposit_requirement_min, 'max', bp.deposit_requirement_max,
  'term', bptf.term_period, 'notice', bpnf.notice_period, 'state', bp.current_state,
  'maxavail', bp.maximum_available, 'freq', bp.interest_feature_realisation_period,
  'start', bp.product_availability_from, 'end', bp.product_availability_to,
  'upd', extract(epoch FROM bp.updated_at),
  'c_rate', (SELECT extract(epoch FROM max(r.created_at)) FROM rate_detail r WHERE r.bank_product_sid = bp.sid),
  'rates', (SELECT json_agg(json_build_array(r.start_date, r.end_date, r.live)) FROM rate_detail r
            WHERE r.bank_product_sid = bp.sid),
  'today', (now() AT TIME ZONE '{tz}')::date,
  'formula', coalesce(cbc.interest_formula_type::text, 'BONDSMITH'))
FROM bank_product bp
JOIN partner_bank pb ON pb.sid = bp.bank_sid
LEFT JOIN custom_bank_config cbc ON cbc.bank_sid = pb.sid
LEFT JOIN product_rate_view prv ON prv.product_sid = bp.sid
LEFT JOIN bank_product_term_feature bptf ON bptf.sid = bp.sid AND bptf.product_type = bp.product_type
LEFT JOIN bank_product_notice_feature bpnf ON bpnf.sid = bp.sid AND bpnf.product_type = bp.product_type
WHERE bp.uid = ANY({uids})
"""


def check_products(run, rows):
    entity = "PRODUCT"
    core = {}
    for part in chunks(list(rows)):
        for p in jrows(CORE_DSN, CORE_PRODUCT_SQL.format(tz=LONDON, uids=arr(part))):
            core[p["prod"]] = p
    newest = run.newest_entry(entity, list(rows))
    for uid, (row, f) in rows.items():
        s = run.stats[entity]
        s["rows"] += 1
        if run.superseded(entity, f.name, uid, newest):
            s["superseded_in_s3"] += 1
            continue
        p = core.get(uid)
        if p is None:
            run.add("a PRODUCT row has a bank_product in core", "PRODUCT " + uid + " " + f.name,
                    "no bank_product", "a row", "none")
            s["findings_rows"] += 1
            continue
        changed = max([float(p[k]) for k in ("upd", "c_rate") if p.get(k) is not None], default=None)
        at = run.extract.get(f.name, (None, None))[0]
        if at is not None:
            # product_rate_view takes the live rate for CURRENT_DATE, so a rate whose range starts or
            # ends between the extract's day and today changes the reading with no row written.
            extract_day = date.fromisoformat(london_date(at))
            today = date.fromisoformat(p["today"])
            for start, end, live in p["rates"] or []:
                if live and ((start and extract_day < date.fromisoformat(start) <= today) or
                             (end and extract_day <= date.fromisoformat(end) < today)):
                    changed = max(changed or 0, float(at) + 1)
        verdict = run.verdict(entity, f.name, changed)
        s[verdict] += 1
        if verdict in ("pending", "unknown"):
            continue
        term = "{}M".format(p["term"]) if p["term"] is not None else (
            "{}D".format(p["notice"]) if p["notice"] is not None else None)
        expected = {
            "ProductType": PRODUCT_TYPE.get(p["ptype"]), "Currency": p["cur"], "ProductName": p["name"],
            "GrossInterestRate": p["gross"], "AER": p["aer"], "MinBalanceAmount": p["min"],
            "MaxBalanceAmount": p["max"], "TermLength": term, "StatusCode": PRODUCT_STATE.get(p["state"]),
            "MaxAvailableDepositAmount": p["maxavail"], "InterestFrequency": FREQUENCY.get(p["freq"]),
            "ProductStartDate": p["start"], "ProductEndDate": p["end"],
            "InterestDayCountType": "ACTUAL_365_FIXED",
        }
        bad = False
        for column, want in expected.items():
            got = row.get(column, "")
            s["columns"] += 1
            if not same(column, want, got):
                bad = True
                run.add("PRODUCT {} equals core".format(column), "PRODUCT {} {}".format(uid, f.name),
                        "feed {} vs core now".format(column), text(want), text(got), verdict == "stale")
        gross, aer = num(row.get("GrossInterestRate")), num(row.get("AER"))
        s["units"] += 1
        if gross is not None and aer is not None:
            if not (Decimal(0) <= gross < Decimal(1)) or not (Decimal(0) <= aer < Decimal(1)):
                bad = True
                run.add("PRODUCT rates are written as fractions", "PRODUCT {} {}".format(uid, f.name),
                        "GrossInterestRate and AER should both be below 1", "fractions < 1",
                        "gross {} AER {}".format(gross, aer))
            elif aer < gross:
                bad = True
                run.add("PRODUCT AER is not below the gross rate", "PRODUCT {} {}".format(uid, f.name),
                        "AER below gross", "AER >= gross", "gross {} AER {}".format(gross, aer))
            recomputed = oracle_aer(gross, p["freq"], date.fromisoformat(p["today"]))
            if recomputed is None:
                s["aer_not_mirrored"] += 1
            else:
                s["aer_recomputed"] += 1
                if abs(recomputed - aer) > Decimal("0.0001"):
                    bad = True
                    run.add("PRODUCT AER equals the compounding of its gross rate",
                            "PRODUCT {} {}".format(uid, f.name),
                            "statement_oracle.aer({}, {}) at 4 dp".format(gross, p["freq"]),
                            recomputed, aer)
        s["findings_rows"] += 1 if bad else 0


# ------------------------------------------------------------------------------------- TRANSACTION

CORE_TRANSACTION_SQL = """
SELECT json_build_object('tx', at.uid, 'acct', cpa.uid, 'type', at.transaction_type,
  'instr', dci.instruction_type, 'amount', at.customer_amount, 'ref', at.payment_reference,
  'bal', at.updated_product_account_balance, 'vd', at.value_date,
  'created', extract(epoch FROM at.created_at))
FROM account_transaction at
JOIN customer_product_account cpa ON cpa.sid = at.customer_product_account_sid
LEFT JOIN direct_instruction_subject dis ON dis.sid = at.balance_change_subject_id
LEFT JOIN direct_customer_instruction dci ON dci.sid = dis.instruction_sid
WHERE at.uid = ANY({uids})
"""


def transaction_type(kind, instruction):
    """InvestecTransactionSpec.mapTransactionType."""
    if kind in ("INTEREST", "ADJUSTMENT", "INCOME"):
        return "Interest"
    if kind == "SAVINGS_DEPOSIT":
        if instruction is None:
            return "Deposit"
        return {"PRODUCT_TRANSFER_IN": "Transfer_In", "ROLLOVER": "Transfer_In", "MATURITY": "Maturity",
                "DEPOSIT": "Deposit", "WITHDRAWAL": "Deposit", "PRODUCT_TRANSFER_OUT": "Deposit",
                "RETURN": "Deposit"}.get(instruction)
    if kind in ("SAVINGS_WITHDRAWAL", "FEES", "PLATFORM_FEES", "MATURITY"):
        default = "Maturity" if kind == "MATURITY" else "Withdrawn"
        if instruction is None:
            return default
        return {"WITHDRAWAL": "Withdrawn", "PRODUCT_TRANSFER_OUT": "Transfer_Out",
                "ROLLOVER": "Transfer_Out", "RETURN": "Return", "MATURITY": "Maturity",
                "DEPOSIT": default, "PRODUCT_TRANSFER_IN": default}.get(instruction)
    return None


def check_transactions(run, rows):
    entity = "TRANSACTION"
    core = {}
    for part in chunks([r["TransactionId"] for r, _ in rows]):
        for t in jrows(CORE_DSN, CORE_TRANSACTION_SQL.format(uids=arr(part))):
            core[t["tx"]] = t
    s = run.stats[entity]
    for row, f in rows:
        uid = row["TransactionId"]
        s["rows"] += 1
        t = core.get(uid)
        subject = "TRANSACTION {} {}".format(uid, f.name)
        if t is None:
            run.add("a TRANSACTION row has an account_transaction in core", subject, "no row", "a row", "none")
            s["findings_rows"] += 1
            continue
        s["judged"] += 1
        if t["type"] == "FEES":
            run.add("a TRANSACTION file leaves out FEES rows", subject,
                    "the collector selects transaction_type <> 'FEES'", "no row", "FEES row sent")
        ref = (t["ref"] if t["ref"] is not None else "NONREF")[:18]
        expected = {
            "AccountId": t["acct"], "TransactionType": transaction_type(t["type"], t["instr"]),
            "Amount": num(t["amount"]), "PaymentReference": ref, "UpdatedBalance": num(t["bal"]),
            "ValueDate": t["vd"] + "T00:00:00Z", "BookingDateTime": stamp(t["created"]),
        }
        bad = False
        for column, want in expected.items():
            got = row.get(column, "")
            s["columns"] += 1
            if not same(column, want, got):
                bad = True
                run.add("TRANSACTION {} equals core".format(column), subject,
                        "core type {} instruction {}".format(t["type"], t["instr"]), text(want), text(got))
        amount = num(row.get("Amount"))
        if amount is not None:
            s["signs"] += 1
            if t["type"] == "SAVINGS_DEPOSIT" and amount < 0 or t["type"] == "SAVINGS_WITHDRAWAL" and amount > 0:
                bad = True
                run.add("a TRANSACTION amount has the sign of its type", subject,
                        "core type {} instruction {}".format(t["type"], t["instr"]),
                        "deposit >= 0, withdrawal <= 0", amount)
        if t["type"] == "PLATFORM_FEES":
            s["platform_fees_sent"] += 1
        if t["type"] in ("SAVINGS_DEPOSIT", "SAVINGS_WITHDRAWAL") and t["instr"] is None:
            s["no_instruction"] += 1
        s["findings_rows"] += 1 if bad else 0


# ------------------------------------------------------------------------------------------- driver

def check(bank_uid, limit=600, days=2):
    """Returns (findings, stats); findings are {rule, subject, detail, expected, actual}."""
    run = Run(bank_uid, days, limit)
    run.load_extracts()
    by_entity, transactions, files = newest_rows(bank_uid, days, limit)
    check_customers(run, by_entity["CUSTOMER"])
    check_accounts(run, by_entity["ACCOUNT"])
    check_products(run, by_entity["PRODUCT"])
    check_transactions(run, transactions)
    stats = {"files_read": files, "days": days, "limit": limit,
             "entities": {k: dict(v) for k, v in run.stats.items()}}
    by_rule = Counter(f["rule"] for f in run.findings)
    stats["findings_by_rule"] = dict(by_rule)
    return run.findings, stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("bank_uid")
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--limit", type=int, default=600)
    parser.add_argument("--json")
    parser.add_argument("--show", type=int, default=3, help="examples to print per rule")
    args = parser.parse_args()
    findings, stats = check(args.bank_uid, args.limit, args.days)
    print(json.dumps(stats, indent=1))
    shown = Counter()
    for f in findings:
        shown[f["rule"]] += 1
        if shown[f["rule"]] <= args.show:
            print("FAIL {rule}: {subject} - {detail}\n   expected {expected!r} actual {actual!r}".format(**f))
    print("{} files".format(stats.get("files", "?")))
    print("{} checks failed".format(len(findings)))
    if args.json:
        Path(args.json).write_text(json.dumps({"stats": stats, "findings": findings}, indent=1))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
