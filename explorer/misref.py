"""Inbound credits whose reference or amount is almost, but not exactly, what a batch is owed.

Clearing settles a Direct batch's payment due (match_by_reference, PLATFORM_SAFEGUARD) only from
funding records whose customer_reference equals the due's payment_reference as a string, and whose
amounts sum to the dues of that reference: FundingRecord.settles and ReferencePaymentMatcher.
Nothing trims, folds case or compares a prefix, so a near-miss reference has to stay unmatched.
"""
from decimal import Decimal

VARIANTS = ("case", "affix", "whitespace", "truncated", "cancelled", "settled", "unknown",
            "amount_up", "amount_down")

NEEDS_LIVE_BATCH = {"case", "affix", "whitespace", "truncated", "amount_up", "amount_down"}
AMOUNT_VARIANTS = {"amount_up", "amount_down"}
TERMINAL = {"SETTLED", "COMPLETED", "CANCELLED", "REJECTED"}
PENNY = Decimal("0.01")


def mutate(variant, reference, turn):
    """The end-to-end id to send for a batch's paymentReference; `turn` picks between sub-shapes."""
    if variant == "case":
        mutated = reference.swapcase()
        return mutated if mutated != reference else reference + "x"
    if variant == "affix":
        return "Z9" + reference if turn % 2 == 0 else reference + "Z9"
    if variant == "whitespace":
        return (" " + reference, reference + " ", " " + reference + " ")[turn % 3]
    if variant == "truncated":
        return reference[:-3] if len(reference) > 6 else reference[:-1]
    return reference


def amount_for(variant, required):
    if variant == "amount_up":
        return required + PENNY
    if variant == "amount_down":
        return required - PENNY if required - PENNY >= PENNY else required + PENNY
    return required


def literal(text):
    return "'" + str(text).replace("'", "''") + "'"


def credit_filter(floor_sid, amount, sent):
    """The funding records this credit made: after the snapshot, same amount, same reference
    (compared as sent, and with the ends trimmed in case the bank trims)."""
    return ("f.sid > {} AND f.debit_credit_mark = 'CREDIT' AND f.value_amount = {} "
            "AND (f.customer_reference = {} OR btrim(f.customer_reference) = btrim({}))").format(
                int(floor_sid), Decimal(amount), literal(sent), literal(sent))


def funding_sql(floor_sid, amount, sent):
    return "SELECT f.uid, f.customer_reference FROM funding_record f WHERE {}".format(
        credit_filter(floor_sid, amount, sent))


def links_sql(floor_sid, amount, sent):
    return ("SELECT f.uid, f.customer_reference, d.payment_reference FROM partner_payment_link l "
            "JOIN funding_record f ON f.sid = l.funding_record_sid "
            "JOIN partner_payment_due d ON d.sid = l.payment_due_sid WHERE {}").format(
                credit_filter(floor_sid, amount, sent))


def judge(variant, sent, funding, links, status_before, status_after):
    """(findings, notes). `funding` is [(uid, stored reference)], `links` is
    [(uid, stored reference, due reference)], statuses are the target batch's, or None.

    A finding is (rule, detail, expected, actual). A near-miss that settles because the stored
    reference came out equal to the due's is a note: the matcher is exact on what it stored, so the
    normalisation happened before it, and the rule cannot say which side owns it.
    """
    findings, notes = [], []
    if len(funding) > 1:
        findings.append((
            "an inbound credit becomes one funding record",
            "one credit sent as {!r} produced {} funding records".format(sent, len(funding)),
            "one funding record", "{} funding records".format(len(funding))))
    for uid, stored, due_reference in links:
        if due_reference != stored:
            findings.append((
                "a credit settles only a batch whose reference it carries exactly",
                "funding record {} carries {!r} and was linked to a payment due of {!r}".format(
                    uid[:8], stored, due_reference),
                "no link, or a due whose reference equals {!r}".format(stored),
                "linked to {!r}".format(due_reference)))
        elif stored == sent:
            findings.append((
                "a credit that is not the exact reference and amount does not settle a batch",
                "the {} credit sent as {!r} was matched to a payment due of that reference".format(
                    variant, sent),
                "an unmatched funding record", "linked to a due of {!r}".format(due_reference)))
        else:
            notes.append("sent {!r}, stored {!r}, and the stored reference matched a due".format(
                sent, stored))
    normalised = bool(notes)
    if status_before and status_after and status_before != status_after:
        terminal_left = status_before in TERMINAL
        settled = status_after in ("SETTLED", "COMPLETED")
        if terminal_left or (settled and not normalised):
            findings.append((
                "a batch's status does not move because of a credit that is not its exact payment",
                "the batch went {} to {} across a {} credit sent as {!r}".format(
                    status_before, status_after, variant, sent),
                status_before, status_after))
        else:
            notes.append("batch status {} to {}".format(status_before, status_after))
    if not funding:
        notes.append("no funding record for the credit within the wait")
    elif not links:
        notes.append("held as an unmatched funding record")
    return findings, notes
