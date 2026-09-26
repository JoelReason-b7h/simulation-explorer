"""Stands up one cohort from a freshly launched, empty stack.

A cohort is a bank, one INSTANT product on it, and a POOLED direct platform bound to that bank,
with the OAuth client linked and accruals scheduled. §9 of the design wants a bank per cohort
because the business date is per bank, which is what lets one cohort sit at day 3 while another
walks to day 33.

Adapted from the Bruno flow at
~/Documents/bruno/collections/exchange/flows/investec-direct-run6, which was verified against dev.
Two differences, both because a freshly launched local stack has no platforms at all:

- The nominated account goes in the platform creation body rather than in a later update. On dev
  the update exists to avoid recreating a platform, because creating one steals the shared OAuth
  client from whichever platform held it. Here nothing holds it.
- The three steps that mint and bind a dedicated OAuth client are dropped, for the same reason:
  creating the platform links the shared test client, and that is the client the harness uses.
"""

from __future__ import annotations

import json
import subprocess
import uuid
import os
import sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from explorer import config, webhooks, world
from explorer.client import BearerClient

BANK_NAME = "Harness Direct Bank"
PLATFORM_NAME = "Harness Direct Platform"
PRODUCT_NAME = "Harness Instant Access"
TERM_PRODUCT_NAME = "Harness One Year Term"
SHORT_TERM_PRODUCT_NAME = "Harness One Month Term"
NOTICE_PRODUCT_NAME = "Harness Two Day Notice"
VIRTUAL_ACCOUNT_BATCH = 2000
VIRTUAL_ACCOUNT_REDRAWS = 20


def today():
    return date.today().isoformat()


def bank_body():
    return {
        "tradingName": BANK_NAME,
        "legalName": BANK_NAME,
        "incorporationCountry": "UK",
        "registeredRegulators": [
            {"regulatorType": "FCA", "registrationNumber": "111222"},
            {"regulatorType": "PRA", "registrationNumber": "333444"},
        ],
        "endOfDay": "19:00:00",
        "timeZoneCode": "LONDON",
        "contactName": "Harness Contact",
        "contactEmail": "perf@test.com",
        "contactPhone": "7000000000",
        "integrationType": "EMAIL",
        "depositProtectionScheme": "FSCS",
        "hexColour": "#0066CC",
        "logoUrl": "https://bondsmith.co.uk/logo.svg",
        "gracePeriod": 0,
        "clearingConnector": "INVESTEC",
        "registeredAddress": {
            "addressLine1": "1 Investec Way", "postCode": "EC2A 1AA", "country": "GB"},
        "nominatedAccount": {
            "accountName": "INVESTEC acc",
            "currency": "GBP",
            "accountHolderAddress": {
                "addressLine1": "1 Investec Way", "postCode": "EC2A 1AA", "country": "GB"},
            "accountDetails": {
                "accountDetailType": "IBAN_SWIFT", "countryCode": "GB",
                "accountIdentifier": "GB50MIDL40025099999999", "bankIdentifier": "MIDLGB22"},
        },
        "holidayCalendarType": "ENGLAND",
        "isDirect": True,
    }


def bank_config_body(bank_uid):
    return {
        "bankUid": bank_uid,
        "referenceType": "BONDSMITH",
        "interestFormulaType": "INVESTEC_DIRECT",
        "maturityDateFormulaType": "BONDSMITH",
        "aggregateAcrossProducts": False,
        "netInstantProductDues": True,
        "netNoticeProductDues": False,
        "residualInterestEnabled": False,
    }


def product_body(bank_uid):
    return {
        "externalId": "harness-instant-a",
        "name": PRODUCT_NAME,
        "bankUid": bank_uid,
        "productType": "INSTANT",
        "exoticType": "NONE",
        "currency": "GBP",
        "accountHolderTypes": ["INDIVIDUAL"],
        "depositRequirement": {"min": 1, "max": 1000000},
        "interestFeature": {"payoutPeriod": "END_OF_DAY", "interestCutOffTime": "18:00:00"},
        "periodFeature": {
            "noticePeriod": 0, "termPeriod": 0, "coolOffPeriod": "NONE",
            "sayePeriod": None, "earlyWithdrawalPenaltyDays": None},
        "maximumAvailable": 10000000,
        "productLiterature": {
            "tandcUrl": "https://bondsmith.co.uk/tnc",
            "brochureUrl": "https://bondsmith.co.uk/brochure"},
        "rateDetail": {"grossRate": 0.0365, "startDate": today()},
        "productAvailability": {"availableFrom": today(), "availableUntil": None},
        "taxWrappers": ["NONE"],
        "useBankDefault": True,
    }


def term_product_body(bank_uid):
    """A one-year TERM product beside the INSTANT one.

    Without it the cohort holds only an INSTANT product, and every maturity action is refused with
    "Destination account can only be set for term accounts" however long the run goes. A TERM
    product also brings maturity and rollover within reach, which no INSTANT account can show.

    TERM rejects the INSTANT payout cadence: InternalInterestPayoutPeriodType lists AT_MATURITY,
    ANNUAL_PAYOUT, MONTHLY_PAYOUT and ANNUAL_COMPOUNDING for TERM, and END_OF_DAY for INSTANT and
    NOTICE only, so sending END_OF_DAY answers "Product type TERM does not support payout period".
    """
    body = product_body(bank_uid)
    body["externalId"] = "harness-term-a"
    body["name"] = TERM_PRODUCT_NAME
    body["productType"] = "TERM"
    # termPeriod counts MONTHS, not days: PeriodFeatureRequest declares it as "Period for number
    # of months". Setting 365 made a thirty-year term, and core then built a day-by-day interest
    # projection running to 2049 on every account read, which slowed the run to one trial a minute.
    body["periodFeature"] = {
        "noticePeriod": 0, "termPeriod": 12, "coolOffPeriod": "NONE",
        "sayePeriod": None, "earlyWithdrawalPenaltyDays": None}
    body["interestFeature"] = {"payoutPeriod": "AT_MATURITY", "interestCutOffTime": "18:00:00"}
    body["rateDetail"] = {"grossRate": 0.045, "startDate": today()}
    return body


def short_term_product_body(bank_uid):
    """A one-month TERM product, so maturity is reachable inside a run.

    The one-year product cannot mature: the run advances the bank about thirty business days in
    ten minutes and a twelve-month term needs roughly 365. A TERM account also cannot be closed —
    "Only Instant or Notice accounts can be closed" — so without a short term the whole maturity
    path, and every state past it, is unreachable however long the run goes.
    """
    body = term_product_body(bank_uid)
    body["externalId"] = "harness-term-short"
    body["name"] = SHORT_TERM_PRODUCT_NAME
    body["periodFeature"] = {
        "noticePeriod": 0, "termPeriod": 1, "coolOffPeriod": "NONE",
        "sayePeriod": None, "earlyWithdrawalPenaltyDays": None}
    return body


def notice_product_body(bank_uid):
    """A two-day NOTICE product, so a notice withdrawal and a notice closure are reachable.

    ProductTypeValidator refuses a notice period below one day. The due date is core's own date
    plus the notice period, and nothing in the run moves core's clock, so NoticeFallsDue brings a
    withdrawal due rather than waiting two real days for it.
    """
    body = product_body(bank_uid)
    body["externalId"] = "harness-notice-a"
    body["name"] = NOTICE_PRODUCT_NAME
    body["productType"] = "NOTICE"
    body["periodFeature"] = {
        "noticePeriod": 2, "termPeriod": 0, "coolOffPeriod": "NONE",
        "sayePeriod": None, "earlyWithdrawalPenaltyDays": None}
    body["rateDetail"] = {"grossRate": 0.04, "startDate": today()}
    return body


def platform_body(bank_uid):
    return {
        "tradingName": PLATFORM_NAME + os.environ.get("SIM_PLATFORM_SUFFIX", ""),
        "legalName": PLATFORM_NAME + os.environ.get("SIM_PLATFORM_SUFFIX", ""),
        # The contact's wire field is `accountManager`, not `name`: PartnerContactRequest maps it
        # onto an internal field called name, and the generated ops template shows the getter.
        "accountManagerContact": {
            "email": "perf@test.com", "phone": "7000000001", "accountManager": "Harness Contact"},
        "endOfDay": "17:00:00",
        "timeZoneCode": "LONDON",
        "integrationRequest": {
            "platformIntegrationType": "EXTERNAL_API", "fileIntegrationType": None},
        "transferType": "POOLED",
        "onboardType": "UNVERIFIED",
        "saveAsYouEarn": False,
        "registeredAddress": {
            "addressLine1": "1 Platform Way", "postCode": "EC2A1AA", "country": "GB"},
        "connectorType": "INVESTEC",
        # Without this the platform is created and returns a uid, but
        # PartnerPlatformAccountsService.createPlatformPaymentAccounts iterates an empty list and
        # no internal account is made. The deposit then never lands, and on a local stack it fails
        # silently rather than with "Unable to find GBP Internal Account for Platform".
        "nominatedAccounts": [{
            "accountName": "Harness Pooled Platform Nominated",
            "currency": "GBP",
            "accountHolderAddress": {
                "addressLine1": "1 Platform Way", "postCode": "EC2A 1AA", "country": "GB"},
            "accountDetails": {
                "accountDetailType": "IBAN_SWIFT", "countryCode": "GB",
                "accountIdentifier": "GB29NWBK60161331926819", "bankIdentifier": "NWBKGB2L"},
        }],
        "holidayCalendarType": "ENGLAND",
        "matchByReference": True,
        "directBankUid": bank_uid,
    }


def access_body(bank_uid, platform_uid, product_uid):
    """A zero fee is valid, and it keeps the interest arithmetic readable."""
    return {
        "bankUid": bank_uid,
        "feeDetailRequest": {
            "amount": 0, "type": "INTEREST", "startDate": today(),
            "alias": "Harness no fee", "platformUid": platform_uid, "productUid": product_uid},
    }


def schedule_body():
    """`scheduledTime` is in the bank's own zone while `nextRaisedDate` is UTC, so the two differ
    by the London offset — 20:00 London is 19:00Z under BST and 20:00Z under GMT. It is a datetime
    rather than a date; a plain date is rejected with "Invalid value" on that field. The first
    firing is tomorrow so the schedule cannot race the manual accrual calls that drive a run today.
    """
    fire_at = datetime.combine(date.today() + timedelta(days=1), time(20, 0))
    in_utc = fire_at.replace(tzinfo=ZoneInfo("Europe/London")).astimezone(ZoneInfo("UTC"))
    return {
        "scheduledTime": ["20:00:00"],
        "timeZoneCode": "LONDON",
        "frequencyType": "DAILY",
        "includeHolidays": True,
        "nextRaisedDate": in_utc.strftime("%Y-%m-%dT%H:%M:%S"),
    }


SHARED_TEST_CLIENT = "5ldvheuf83ic4pftapi5p5ntp8"
CORE_DSN = os.environ.get("SIM_CORE_DSN", "postgresql://core:password@localhost:5432/core")


def own_client(platform_uid):
    """Give the new platform a client of its own, so several platforms can run at once.

    Outside prod, creating a direct platform upserts core's `direct-model-test-clientId` onto the
    new platform ON CONFLICT (client_id), so the shared client only ever reaches the newest one.
    Renaming the row frees the shared id for the next platform, and the harness's own token server
    signs a token for any client id. Returns the client id to put in the token.
    """
    uuid.UUID(platform_uid)
    client_id = "sim-{}".format(platform_uid)
    sql = ("UPDATE platform_client_link SET client_id = '{0}' WHERE client_id = '{1}' "
           "AND platform_sid = (SELECT sid FROM partner_platform WHERE uid = '{2}') "
           "RETURNING client_id").format(client_id, SHARED_TEST_CLIENT, platform_uid)
    done = subprocess.run(["psql", CORE_DSN, "-tA", "-v", "ON_ERROR_STOP=1", "-c", sql],
                          capture_output=True, text=True, timeout=60)
    renamed = client_id in done.stdout
    print("  {} {:44} {}".format("ok " if renamed else "REJ", "give the platform its own client",
                                 client_id if renamed else done.stderr.strip()[:300]))
    if not renamed:
        raise SystemExit(1)
    return client_id


def require_cop(platform_uid):
    """Turn Confirmation of Payee on for the platform.

    NominatedAccountVerificationDispatcher.dispatch returns on its first line unless the platform's
    cop_verification_required is set, so with it unset no nominated account is ever checked and
    AddUnverifiableNominatedAccount proves nothing. The flag lives on custom_platform_config rather
    than on the platform, and the ops creation body has no field for it, so it is set the way the
    acceptance suite sets it, in CoreRepository.setPlatformCopVerificationRequired. The config row
    already exists: creating the platform upserts it, which is how matchByReference lands.

    It also decides the state clearing gives the account. InternalCustomerAccountRequestBuilder
    reads requireVerification as cop_verification_required OR require_external_account_verification,
    so this one flag is what leaves external_account UNVERIFIED until CoP answers, which is what
    integrity.payments_to_unverified_payees reads.

    Turning it on changes every customer the run makes, not only the unverifiable ones: an ordinary
    name still answers MATCH, but the answer is asynchronous, so there is now a window between
    creating a customer and their payee being usable.
    """
    uuid.UUID(platform_uid)
    sql = ("UPDATE custom_platform_config SET cop_verification_required = true "
           "WHERE platform_sid = (SELECT sid FROM partner_platform WHERE uid = '{}') "
           "RETURNING cop_verification_required").format(platform_uid)
    done = subprocess.run(["psql", CORE_DSN, "-tA", "-v", "ON_ERROR_STOP=1", "-c", sql],
                          capture_output=True, text=True, timeout=60)
    required = "t" in done.stdout.split()
    print("  {} {:44} {}".format("ok " if required else "REJ",
                                 "require Confirmation of Payee",
                                 "on" if required else done.stderr.strip()[:300] or "no config row"))
    if not required:
        raise SystemExit(1)
    return required


def subscribe_webhooks(ops, platform_uid):
    """Subscribe the platform to every Direct webhook, for the run's webhook accounting.

    Never fails the standup: two failed standups in a row wipe the stack, and a run without
    webhooks still explores. A refusal is printed and the run's accounting then finds no
    subscription and says so.
    """
    try:
        done, refused = webhooks.subscribe(ops, platform_uid)
        readback = webhooks.subscribed(ops, platform_uid)
    except Exception as fault:  # noqa: BLE001 - any fault here is logged and skipped
        print("  SKIP {:44} {}".format("subscribe webhooks", repr(fault)[:300]))
        return
    if refused or readback is None or len(readback) != len(webhooks.EVENT_TYPES):
        print("  SKIP {:44} {} subscribed, {} read back, refused: {}".format(
            "subscribe webhooks", len(done), "none" if readback is None else len(readback),
            "; ".join(refused)[:400] or "none"))
        return
    print("  ok  {:44} {} event types at {}".format(
        "subscribe webhooks", len(readback), webhooks.url_for(platform_uid)))


def uid_from(body):
    if isinstance(body, str):
        return body.strip('"')
    if isinstance(body, dict):
        for field in ("uid", "bankUid", "platformUid", "productUid", "approvalUid"):
            if body.get(field):
                return body[field]
    return None


def step(ops, label, method, path, body=None):
    call = ops.call(method, path, json_body=body)
    mark = "ok " if call.ok else "REJ"
    print("  {} {:44} {}".format(mark, label, call.status))
    if not call.ok:
        print("      {}".format(json.dumps(call.body)[:400]))
        raise SystemExit(1)
    return call


def main():
    settings = config.load("local")
    ops = BearerClient(settings["ops_base_url"], world.ops_token(settings))
    print("standing up a cohort on {}".format(settings["ops_base_url"]))

    # A fleet puts several platforms on one bank, so the platforms race on the bank's business
    # date, its accrual run and its pool of accounts. The first standup makes the bank and its
    # products; each later one names them and makes only its own platform.
    reuse = os.environ.get("SIM_REUSE_COHORT")
    if reuse:
        known = json.loads(reuse)
        bank_uid, product_uid = known["bankUid"], known["productUid"]
        term_product_uid, short_term_uid = known["termProductUid"], known["shortTermProductUid"]
        notice_uid = known["noticeProductUid"]
        print("      reusing bankUid {}".format(bank_uid))
        return stand_up_platform(ops, settings, bank_uid, product_uid, term_product_uid,
                                 short_term_uid, notice_uid)

    bank = step(ops, "create direct bank", "POST", "/operations/proposals/banks", bank_body())
    bank_uid = uid_from(bank.body)
    print("      bankUid {}".format(bank_uid))

    step(ops, "set the INVESTEC_DIRECT interest formula", "POST",
         "/operations/own/setup/bank/{}/config".format(bank_uid), bank_config_body(bank_uid))

    proposal = step(ops, "propose the INSTANT product", "POST",
                    "/operations/proposals/banks/{}/products".format(bank_uid),
                    product_body(bank_uid))
    approval_uid = uid_from(proposal.body)

    approved = step(ops, "approve the product", "POST",
                    "/operations/approvals/{}/accept".format(approval_uid), {})
    product_uid = uid_from(approved.body)
    print("      bankProductUid {}".format(product_uid))

    term_proposal = step(ops, "propose the TERM product", "POST",
                         "/operations/proposals/banks/{}/products".format(bank_uid),
                         term_product_body(bank_uid))
    term_product_uid = uid_from(
        step(ops, "approve the TERM product", "POST",
             "/operations/approvals/{}/accept".format(uid_from(term_proposal.body)), {}).body)
    print("      termProductUid {}".format(term_product_uid))

    short_proposal = step(ops, "propose the one month TERM product", "POST",
                          "/operations/proposals/banks/{}/products".format(bank_uid),
                          short_term_product_body(bank_uid))
    short_term_uid = uid_from(
        step(ops, "approve the one month TERM product", "POST",
             "/operations/approvals/{}/accept".format(uid_from(short_proposal.body)), {}).body)
    print("      shortTermProductUid {}".format(short_term_uid))

    notice_proposal = step(ops, "propose the two day NOTICE product", "POST",
                           "/operations/proposals/banks/{}/products".format(bank_uid),
                           notice_product_body(bank_uid))
    notice_uid = uid_from(
        step(ops, "approve the two day NOTICE product", "POST",
             "/operations/approvals/{}/accept".format(uid_from(notice_proposal.body)), {}).body)
    print("      noticeProductUid {}".format(notice_uid))

    return stand_up_platform(ops, settings, bank_uid, product_uid, term_product_uid,
                             short_term_uid, notice_uid, first=True)


def stand_up_platform(ops, settings, bank_uid, product_uid, term_product_uid, short_term_uid,
                      notice_uid, first=False):
    platform = step(ops, "create the POOLED direct platform", "POST",
                    "/operations/platforms", platform_body(bank_uid))
    platform_uid = uid_from(platform.body)
    print("      platformUid {}".format(platform_uid))

    require_cop(platform_uid)

    step(ops, "grant product access with a zero fee", "POST",
         "/operations/own/access", access_body(bank_uid, platform_uid, product_uid))

    step(ops, "grant TERM product access with a zero fee", "POST",
         "/operations/own/access", access_body(bank_uid, platform_uid, term_product_uid))

    step(ops, "grant one month TERM access with a zero fee", "POST",
         "/operations/own/access", access_body(bank_uid, platform_uid, short_term_uid))

    step(ops, "grant NOTICE product access with a zero fee", "POST",
         "/operations/own/access", access_body(bank_uid, platform_uid, notice_uid))

    if first:
        step(ops, "schedule accruals and realisations", "POST",
             "/operations/banks/{}/schedules/ACCRUALS_AND_REALISATIONS".format(bank_uid),
             schedule_body())

    # The bank mints the virtual account IBANs, so they must exist there before clearing claims
    # them. Skip this and the clearing preload still succeeds, but the bank does not know the IBAN,
    # so a payment into it books against the master account and the statement line ends at
    # EXCEPTION with no subledger attributed.
    # Each account the run opens claims one of these, and clearing throws on every delivery of
    # AccountRequested once none is AVAILABLE. Ten ran out 70 minutes into a long run. The bank
    # draws each batch's numbers at random without making them distinct, so one large batch hits
    # its own duplicate IBAN and fails whole; small batches rarely do, and a failed one is redrawn.
    wanted = int(os.environ.get("SIM_VIRTUAL_ACCOUNTS", "100000")) if first else int(
        os.environ.get("SIM_VIRTUAL_ACCOUNTS_PER_PLATFORM", "0"))
    hsb = BearerClient(settings["hsb_base_url"], timeout=300.0)
    opened, redrawn = 0, 0
    while opened < wanted:
        amount = min(VIRTUAL_ACCOUNT_BATCH, wanted - opened)
        call = hsb.call("POST", "/hsb/accounts/virtual", json_body={
            "amount": amount,
            "realAccountType": "DIRECT",
            "currency": "GBP",
            "connectorType": "INVESTEC",
            "taxWrapperType": "DEFAULT",
        })
        if call.ok:
            opened += amount
            continue
        redrawn += 1
        if redrawn > VIRTUAL_ACCOUNT_REDRAWS:
            print("  REJ {:44} {}".format("open virtual accounts at the bank", call.status))
            print("      {} of {} opened: {}".format(opened, wanted, json.dumps(call.body)[:300]))
            raise SystemExit(1)
    print("  ok  {:44} {} opened, {} batches redrawn".format(
        "open virtual accounts at the bank", opened, redrawn))
    hsb.close()

    client_id = own_client(platform_uid)
    subscribe_webhooks(ops, platform_uid)
    # adapter learns banks and platforms only from the partner file core writes every five
    # minutes, and that scheduler is off locally.
    step(ops, "refresh the partner file adapter reads", "POST",
         "/operations/processor/partners/refresh")

    # Creating the platform records a request in core for its own internal account, left at status
    # INACTIVE, and clearing gets the PLATFORM account_owner row but no account. The schedulers
    # that would fill the pool carry a @SchedulerLock and are off locally, so the harness asks.
    # Without this the whole funding sequence returns 200 at every step and the money never lands.
    step(ops, "preload the Investec virtual accounts", "POST",
         "/account/preload/investec/auto-preload")

    print()
    print("  cohort ready")
    print("    bankUid     {}".format(bank_uid))
    print("    platformUid {}".format(platform_uid))
    print("    productUid  {}".format(product_uid))
    print("    termProductUid {}".format(term_product_uid))
    print("    shortTermProductUid {}".format(short_term_uid))
    print("    noticeProductUid {}".format(notice_uid))
    print("    clientId    {}".format(client_id))
    print()
    ops.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
