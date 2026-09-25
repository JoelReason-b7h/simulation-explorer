"""The tenant actions, and what each one needs before it can be built.

`needs` is the set of identifiers the run must already hold. That says which actions are
constructible from what exists, which is a weaker claim than which will succeed — and that is the
point, because the API rejecting a call is an observation to keep rather than a failure.

Bodies are seeded from the perimeter records under
apps/savings-exchange/api/public-api/direct/direct-api-common, not from the published spec, because
the spec's request shapes have drifted from the service.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def past_instant():
    return (datetime.utcnow() - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Action:
    def __init__(self, name, method, path, needs=(), body=None, entity="account"):
        self.name = name
        self.method = method
        self.path = path
        self.needs = set(needs)
        self.body = body
        self.entity = entity

    def can_build(self, held):
        return self.needs <= set(k for k, v in held.items() if v)

    def build(self, held, mint):
        path = self.path.format(**held)
        body = self.body(held, mint) if self.body else None
        return self.method, path, body

    def __repr__(self):
        return "<action {}>".format(self.name)


# The compliance simulator reads the surname and nothing else: DefaultCheckResponseProcessor sets
# IDV to PASS for "pass", PEP to TRUE_MATCH for "pep", SANCTION to TRUE_MATCH for "sanc", IDV to
# INCONCLUSIVE for "idv" and ADVERSE MEDIA to INCONCLUSIVE for "advm". Every customer used to be
# called "Pass", so every customer activated and the run never saw a customer that compliance had
# stopped. Pass still wins most of the draw, because a cohort where nobody activates can fund
# nothing and the run would spend itself on rejections.
SURNAMES = ("Pass", "Pass", "Pass", "Pep", "Sanc", "Idv", "Pass", "Advm")


def surname_for(reference):
    return SURNAMES[sum(ord(c) for c in reference[-5:]) % len(SURNAMES)]


def customer_body(held, mint):
    reference = mint("cust", 128)
    return {
        "accountHolderType": "INDIVIDUAL",
        "customerReference": reference,
        "fscsAcknowledgedAt": past_instant(),
        "nominatedAccounts": [{
            "accountName": "nominated account",
            "currency": "GBP",
            "accountHolderAddress": {
                "addressLine1": "123 Example Street", "addressLine2": "Flat 4B",
                "town": "London", "county": "Greater London",
                "postCode": "AB12 3CD", "country": "GBR",
            },
            "ukAccountDetails": {"sortCode": "100000", "accountNumber": "41610008"},
        }],
        "person": {
            "title": "MR", "firstName": "Sim",
            "lastName": "Explorer {}".format(surname_for(reference)),
            "dateOfBirth": "1995-03-07",
            "address": {
                "addressLine1": "123 Example Street", "addressLine2": "Flat 4B",
                "town": "London", "county": "Greater London",
                "postCode": "E54HNB", "country": "GBR",
            },
            "nationality": ["GBR"], "sourceOfFunds": "INSURANCE",
            "email": "{}@example.com".format(mint("sim", 40)),
            "phoneNumber": "07783746574", "annualIncome": "50000.00",
            "industry": "TECHNOLOGY_SOFTWARE_DEVELOPMENT", "nino": "XP332310B",
            "isVulnerable": False, "vulnerableDescription": "",
        },
    }


def open_account_body(held, mint):
    body = {
        "productId": held["productId"],
        "accountReference": mint("acct", 18),
        "termsAndConditionsAcceptedAt": past_instant(),
    }
    # A fixed-term account is opened for a stated amount, and leaving it out is refused with
    # "An amount is required when opening a fixed-term account". An instant account takes no
    # amount at all, so the field only goes in for TERM.
    if held.get("productType") == "TERM":
        body["amount"] = "50.00"
    return body


def batch_body(held, mint):
    return {
        # Two references, not one: batchReference is for idempotency and paymentReference is what
        # the funding payment quotes. The published spec merges them into one field.
        "batchReference": mint("batch", 36),
        "paymentReference": mint("p", 16),
        "totalPaymentRequired": "3.00",
        "allocations": [{
            "customerId": held["customerId"],
            "accountReference": held["accountReference"],
            "instructionReference": mint("i", 36),
            "instructionType": "DEPOSIT",
            "productId": held["productId"],
            "amount": "3.00",
        }],
    }


# Every amount was the same number, so nothing ever sat on a boundary. These are the shapes worth
# trying against a balance: a penny, a round pound, an odd fraction, and more than is there.
AMOUNTS = ("0.01", "1.00", "3.00", "7.33", "0.50", "99.99")


def amount_for(reference):
    """An amount chosen from the minted reference, so it varies per call and still replays."""
    return AMOUNTS[sum(ord(c) for c in reference[-4:]) % len(AMOUNTS)]


def maturity_body(held, mint):
    """Where the money goes when the account matures — the only product this cohort has."""
    return {"productId": held["productId"]}


def withdraw_body(held, mint):
    """`instructionRequestType` is WITHDRAW, not WITHDRAWAL.

    Two enums carry these names: ExternalDirectInstructionRequestType is WITHDRAW/TRANSFER and
    governs this request, while InstructionType on the batch allocation and the reads is
    DEPOSIT/WITHDRAWAL. Sending WITHDRAWAL here is rejected.
    """
    reference = mint("w", 36)
    return {
        "instructionRequestType": "WITHDRAW",
        "productId": held["productId"],
        "amount": amount_for(reference),
        "instructionReference": reference,
    }


def nominated_account_body(held, mint):
    # The account sits under a `nominatedAccount` field rather than at the top level; a flat body
    # is rejected with "Unrecognized field" on the first key the service does not know.
    return {
        "nominatedAccount": {
            "accountName": "second nominated account",
            "currency": "GBP",
            "accountHolderAddress": {
                "addressLine1": "1 Other Road", "addressLine2": "Flat 2",
                "town": "London", "county": "Greater London",
                "postCode": "AB12 3CD", "country": "GBR",
            },
            "ukAccountDetails": {"sortCode": "100000", "accountNumber": "41610008"},
        }
    }


# hot-sauce-bank decides the Confirmation of Payee answer from the creditor name and nothing else,
# and the creditor name is this account's `accountName`: DirectBankAccountMapper puts it on
# BankAccount.accountHolderName, ConfirmationOfPayeeExecutor sends that as `creditorName`, and
# PaymentPrevalidationController matches these substrings against the uppercased name before
# falling through to MATCH. Steering used to key on the account number too, and the acceptance
# suite builds nominated accounts from a uniformly random IBAN, so that tripped on an ordinarily
# named customer about once in a thousand (SAV-10743). The name is the only trigger left, and it
# is deterministic.
COP_TRIGGERS = (
    # The name belongs to nobody at that account.
    ("NOMATCH", "NOTMATCH"),
    # Near enough that the bank answers with the name it holds instead.
    ("CLOSEMATCH", "CLOSEMATCH"),
    # The two mock errors, which reach core as CopOutcome.FAILURE rather than as an answer.
    ("PPVE400", "FAILURE"),
    ("PPVE500", "FAILURE"),
)


def cop_trigger_for(reference):
    """A trigger chosen from the minted reference, so a run walks all four and still replays."""
    return COP_TRIGGERS[sum(ord(c) for c in reference[-4:]) % len(COP_TRIGGERS)]


def unverifiable_nominated_account_body(held, mint):
    """The same account as AddNominatedAccount, named so Confirmation of Payee cannot pass it.

    All four outcomes land the account in AWAITING_REVIEW rather than VERIFIED:
    NominatedAccountVerificationService.applyCopOutcome maps CLOSEMATCH, NOTMATCH and FAILURE onto
    the one state, and only MATCH reaches VERIFIED. That is the state
    DirectAccountValidator.validateNominatedAccountIsUsable refuses a withdrawal from, and the
    state integrity.payments_to_unverified_payees is written against.

    A customer holds one active nominated account per currency
    (findNominatedAccountIdentifierForCustomer takes customer and currency and returns one), so
    this replaces the payee the customer had rather than sitting beside it, and every withdrawal
    placed after it must be refused until an operator reviews the account.
    """
    body = nominated_account_body(held, mint)
    trigger, _ = cop_trigger_for(mint("cop", 24))
    body["nominatedAccount"]["accountName"] = "{} nominated account".format(trigger)
    return body


CORE_SIDE = [
    Action("CreateCustomer", "POST", "/direct/v1/customers",
           body=customer_body, entity="customer"),
    Action("ReadCustomer", "GET", "/direct/v1/customers/{customerId}",
           needs=["customerId"], entity="customer"),
    Action("ReadBalances", "GET", "/direct/v1/customers/{customerId}/balances",
           needs=["customerId"], entity="customer"),
    Action("AddNominatedAccount", "PATCH", "/direct/v1/customers/{customerId}/nominated-account",
           needs=["customerId"], body=nominated_account_body, entity="customer"),
    # The same call with a name the bank simulator refuses, so the customer ends up with a payee
    # that never verified. Needs the platform's cop_verification_required set, which
    # standup_cohort.require_cop does; without it the dispatcher returns before it calls anything
    # and this is just AddNominatedAccount under another name.
    Action("AddUnverifiableNominatedAccount", "PATCH",
           "/direct/v1/customers/{customerId}/nominated-account",
           needs=["customerId"], body=unverifiable_nominated_account_body, entity="customer"),
    Action("OpenAccount", "POST", "/direct/v1/customers/{customerId}/accounts",
           needs=["customerId", "productId"], body=open_account_body),
    Action("ReadAccount", "GET", "/direct/v1/customers/{customerId}/accounts/{accountId}",
           needs=["customerId", "accountId"]),
    Action("ReadInstructions", "GET", "/direct/v1/customers/{customerId}/instructions",
           needs=["customerId"], entity="customer"),
    # Built by the driver rather than from `held`, because one batch spans several customers and
    # `held` only ever sees the one the driver is standing on.
    Action("PlaceBatchPayment", "POST", "/direct/v1/batches",
           needs=["customerId", "accountReference", "productId"], body=batch_body, entity="batch"),
    # Pays part, all or more than what a batch still owes. Paying and settling are separate, so
    # several payments can stack against one batch and a payment can race a cancellation.
    Action("PayBatchPart", "POST", "/direct/v1/batches",
           needs=["batchPaymentReference"], entity="batch"),
    # Runs the settlement sweep. It moves every cohort's money, so it belongs to the world rather
    # than to the batch that happened to trigger it.
    Action("SettleWorld", "POST", "/direct/v1/batches",
           needs=["batchPaymentReference"], entity="batch"),
    # Cancels one pending deposit line, leaving the rest of the batch alone. The batch id goes in
    # the path and the body is flat: the published spec declares `DELETE /direct/v1/batches` with
    # an `orders` array, and the service answers 405 to that, because
    # ExternalApiDirectBatchPaymentOperations declares `DELETE /direct/v1/batches/{batchId}`.
    Action("CancelAllocation", "DELETE", "/direct/v1/batches/{batchId}",
           needs=["batchId", "cancellableInstructionId"], entity="batch"),
    # Grouped: batch, credit the bank, poll, process, raise the platform dues, settle. The driver
    # runs it as one action because the six steps have no meaning apart from each other.
    Action("FundAccount", "POST", "/direct/v1/batches",
           needs=["customerId", "accountReference", "accountId", "productId"], entity="account"),
    # Moves the cohort's bank one business day on, accruing and realising interest over that day.
    # The path is a placeholder the driver never calls: advance_business_day posts to the ops API,
    # which the catalogue has no way to name.
    Action("AdvanceBusinessDay", "POST", "/direct/v1/batches",
           needs=["accountId"], entity="account"),
    # Finishes an account that CloseAccount left at CLOSING. Same placeholder path, same reason.
    Action("ProcessClosures", "POST", "/direct/v1/batches",
           needs=["accountId"], entity="account"),
    # Brings this account's waiting notice withdrawals due, then runs the notice processor, which
    # the 03:00 cron does. Grouped because a due date with no processor run moves no money, and a
    # race against CloseAccount then lands the close while the processor raises the withdrawal.
    Action("NoticeFallsDue", "POST", "/direct/v1/batches",
           needs=["accountId"], entity="account"),
    # Places a partial withdrawal on a NOTICE account, brings it due, and closes the account
    # after or during the processor run. The driver forces it; see close_notice_after_due.
    Action("CloseNoticeAfterDue", "POST", "/direct/v1/batches",
           needs=["customerId", "accountId", "accountReference"], entity="account"),
    # The notice processor alone, for every account whose notice is already due.
    Action("ProcessDueNotice", "POST", "/direct/v1/batches",
           needs=["accountId"], entity="account"),
    # Fault injection: forces the customer's compliance status through the simulator. The path is
    # a placeholder, because set_kyc_status posts to the simulator rather than the Direct API.
    Action("SetKycStatus", "POST", "/direct/v1/customers",
           needs=["customerId"], entity="customer"),
    # Faults in the machinery rather than in the domain. Every path here is a placeholder,
    # because these drive toxiproxy and docker rather than the Direct API.
    #
    # Three boundaries carry latency and dropped connections: the run's own credit into the bank,
    # core reaching clearing, and clearing reaching the bank.
    Action("SlowTheBank", "POST", "/direct/v1/batches", needs=["accountId"], entity="account"),
    Action("BreakTheBank", "POST", "/direct/v1/batches", needs=["accountId"], entity="account"),
    Action("SlowClearing", "POST", "/direct/v1/batches", needs=["accountId"], entity="account"),
    Action("BreakClearing", "POST", "/direct/v1/batches", needs=["accountId"], entity="account"),
    Action("SlowBankForClearing", "POST", "/direct/v1/batches",
           needs=["accountId"], entity="account"),
    Action("BreakBankForClearing", "POST", "/direct/v1/batches",
           needs=["accountId"], entity="account"),
    Action("HealTheNetwork", "POST", "/direct/v1/batches", needs=["accountId"], entity="account"),
    # A service that dies part way through is the case every retry and idempotency key exists for.
    Action("RestartClearing", "POST", "/direct/v1/batches", needs=["accountId"], entity="account"),
    Action("RestartCore", "POST", "/direct/v1/batches", needs=["accountId"], entity="account"),
    Action("RestartBank", "POST", "/direct/v1/batches", needs=["accountId"], entity="account"),
    # Delivering the same request twice, which is what a retry after an unclear answer looks like
    # from the service's side.
    Action("ReplayLastCall", "POST", "/direct/v1/batches", needs=["customerId"],
           entity="customer"),
    # Funding with the wire cut part way through, which is the state a fault injected between two
    # whole actions can never make: the money has left the bank and the settlement never ran.
    # Duplicate message delivery on the queues clearing consumes, which is the at-least-once
    # case every consumer is written for.
    Action("DuplicateMessages", "POST", "/direct/v1/batches",
           needs=["accountId"], entity="account"),
    Action("StopDuplicating", "POST", "/direct/v1/batches",
           needs=["accountId"], entity="account"),
    # Funding while the queue that carries payment events delivers every message more than once.
    # Turning duplication on as its own action rarely coincided with traffic, because those queues
    # are idle between fundings, so the window closed before a message crossed them.
    Action("FundAccountDuplicated", "POST", "/direct/v1/batches",
           needs=["customerId", "accountReference", "accountId", "productId"], entity="account"),
    Action("FundAccountInterrupted", "POST", "/direct/v1/batches",
           needs=["customerId", "accountReference", "accountId", "productId"], entity="account"),
    # Closing an account while its payment fails at the bank. Closing drains the account, so the
    # money leaves the product account before the payment is sent, and these are the two ways the
    # payment never arrives: the bank refuses it, or the bank takes it and sends it back.
    Action("RejectClosurePayment", "POST", "/direct/v1/batches",
           needs=["customerId", "accountId", "accountReference"], entity="account"),
    Action("ReturnClosurePayment", "POST", "/direct/v1/batches",
           needs=["customerId", "accountId", "accountReference"], entity="account"),
    # In a fleet, this platform's token against another platform's customer and account. The
    # platform is the tenant boundary, so every one of these calls must be refused.
    # The bank's Direct data feed and its RECON, asked for in the middle of the traffic rather than
    # after it, so the feed's cut of the day races deposits, withdrawals and the accrual run.
    # A valid request with one thing wrong or strange in it (explorer/weird.py). It must never
    # answer 5xx, an invalid one must be refused, and a refused one must change nothing.
    Action("WeirdCall", "POST", "/direct/v1/customers", needs=["customerId"], entity="customer"),
    # This platform's own fee on one of its products, proposed and approved through ops. It moves
    # only this platform's customers' reduced gross rate, so every run in a fleet may take it.
    Action("ChangePlatformFee", "POST", "/direct/v1/batches", needs=["accountId"],
           entity="account"),
    Action("RunDataFeed", "POST", "/direct/v1/batches", needs=["accountId"], entity="account"),
    Action("ProbeOtherPlatform", "GET", "/direct/v1/customers/{customerId}",
           needs=["customerId"], entity="customer"),
    Action("PlaceWithdrawal", "POST",
           "/direct/v1/customers/{customerId}/accounts/{accountId}/instruction",
           needs=["customerId", "accountId", "productId"], body=withdraw_body),
    Action("CancelAccountOpening", "DELETE",
           "/direct/v1/customers/{customerId}/accounts/{accountId}",
           needs=["customerId", "accountId"]),
    # `reason` is a required query parameter, not a body field. Sending none answered 400 "Bad
    # Request" with no message at all, 28 times in one run, and the run read that as the service
    # refusing to close the account.
    Action("CloseAccount", "POST",
           "/direct/v1/customers/{customerId}/accounts/{accountId}/close"
           "?reason=NO_LONGER_NEEDED",
           needs=["customerId", "accountId"]),
    Action("CloseCustomer", "POST", "/direct/v1/customers/{customerId}/close",
           needs=["customerId"], entity="customer"),
    # Cancelling an instruction is for notice accounts. On an instant account the service refuses
    # it, and the run drops the action at that state after two refusals rather than being told.
    Action("CancelWithdrawal", "DELETE",
           "/direct/v1/customers/{customerId}/accounts/{accountId}/instruction/{instructionId}",
           needs=["customerId", "accountId", "instructionId"]),
    Action("SetMaturityDestination", "POST",
           "/direct/v1/customers/{customerId}/accounts/{accountId}/maturityDestination",
           needs=["customerId", "accountId", "productId"], body=maturity_body),
    Action("ClearMaturityDestination", "DELETE",
           "/direct/v1/customers/{customerId}/accounts/{accountId}/maturityDestination",
           needs=["customerId", "accountId"]),
]

# Spends the entity, so the driver forks rather than burning its only one.
SPENDS = {"CloseCustomer", "CloseAccount", "CancelAccountOpening",
          "RejectClosurePayment", "ReturnClosurePayment", "CloseNoticeAfterDue"}

# Actions the run never races, against each other or against anything else. Each one changes the
# bank simulator for the whole stack and then puts it back, so two of them at once leave the bank
# in whichever state the slower one restored, and neither result means anything. One run raced
# RejectClosurePayment against itself and the returned closure was never tried at all.
NEVER_RACED = {"RejectClosurePayment", "ReturnClosurePayment", "CloseNoticeAfterDue"}

# Acts on the whole stack rather than on the entity the driver is standing on. A world action
# conflicts with nothing, so it takes no part in the limit on how many calls of a race may win.
# Actions that break the machinery rather than exercising the domain. They cost far more than an
# ordinary trial — a restart is ninety seconds, and every call inside a fault window waits out a
# timeout — so the driver keeps them to a share of the run rather than picking them by least-tried.
EXPENSIVE_FAULTS = {
    "SlowTheBank", "BreakTheBank", "SlowClearing", "BreakClearing",
    "SlowBankForClearing", "BreakBankForClearing",
    "RestartClearing", "RestartCore", "RestartBank",
    "FundAccountInterrupted", "FundAccountDuplicated", "DuplicateMessages",
    "RejectClosurePayment", "ReturnClosurePayment", "CloseNoticeAfterDue",
}

WORLD = {"SettleWorld", "AdvanceBusinessDay", "RunDataFeed", "ProcessClosures", "ProcessDueNotice",
         "SlowTheBank", "BreakTheBank", "SlowClearing", "BreakClearing",
         "SlowBankForClearing", "BreakBankForClearing", "HealTheNetwork",
         "RestartClearing", "RestartCore", "RestartBank",
         "DuplicateMessages", "StopDuplicating"}

# Actions whose after-read looks at a DIFFERENT object than the before-read. Only CreateCustomer
# qualifies: it moves the driver onto the new customer, so an edge from the old customer's state
# to the new one's reading is fabricated, and the run walked that false edge in a loop.
#
# OpenAccount and FundAccount also create something, but they act on the same customer and the
# same account slot, so `account absent -> account REQUESTED` and `OPEN zero -> OPEN positive`
# are real edges. Excluding them left the learned graph with 3 moving edges across 15 states.
CREATES = {"CreateCustomer"}

BY_NAME = {action.name: action for action in CORE_SIDE}
