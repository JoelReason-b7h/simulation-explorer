# Product findings

Defects the harness has reached on images built from `origin/main`. Each entry says what happens,
where in the code, and where the evidence is kept. Harness faults are not listed here; they are in
the workbook.

Evidence paths are relative to this directory. `archive/<cycle>.tgz` holds that cycle's logs and
trials, and `<run>.json` holds each violation with its lead-up and the service's answer.

## Open

1. **A payment with no status from the bank is recorded as rejected.** `ClearinghouseHsbcPaymentService.java:85`
   defaults a null status to RJCT. A connection that closes before the bank answers leaves the
   status null, so clearing holds RJCT while the bank paid (ACSC), and a retry pays twice.
   Evidence: cycle 48, payment `PEC000000100000B` (`chain48.json`).
2. **PaymentSettled on a CLOSING account fails with "Could not find instruction for payment due".**
   `DirectCustomerInstructionService.java:202`. The message retries until the dead-letter queue.
   Near SAV-11636, and a separate defect. Evidence: cycle 48, `060b83a9` (`chain48.json`).
   Again in fleet 5: PaymentSettled for account `71cc626a`, CLOSING, went to the DLQ after 13
   deliveries (`fleet5-p0.json`, stack in `archive/fleet5.tgz`).
   Fleet 10 shows the other face of it: `PaymentInformationSqsConsumer` fails with "Unable to find
   direct accounts for payment dues" from `PaymentDueDirectCustomerAccountService.
   fetchAccountsForPaymentDues` (`:193`), 8 deliveries each for dues `e1782ca9` and `a145407e`. The
   method throws when any due in the message has no Direct account with an instruction, while its
   neighbour around `:181` logs the missing dues and settles the rest, so one unresolvable due
   dead-letters every due in the same PaymentSettled. Stack in `archive/fleet10.tgz`. Fleet 12
   traced where such dues come from: finding 15.
4. **A closure TransferExpectation for an account clearing never created leaves money on a CLOSED
   account.** Account `4f41bd4e`, CLOSED with 3.00. Evidence: cycle 48 (`chain48.json`).
6. **A CLOSED customer moves back to ACTIVATED through a KYC status change.** SAV-11534 part 2.
   Again in fleet 1 (`fleet1-p0.json`, `fleet1-p1.json`, `fleet1-p2.json`).

7. **Clearing inserts a bank entry again on every poll that reads it, and never deduplicates the
   copies it cannot allocate.** `AccountStatementLineRepository.insertAccountStatementLine`
   (`AccountStatementLineRepository.java:60-104`) is a plain INSERT with no `ON CONFLICT`, and
   `account_statement_line` has no unique key on any bank identifier. The only duplicate check is
   in `FallbackHandler.handle`: a line that cannot be enriched goes to EXCEPTION at
   `FallbackHandler.java:45-47`, before `findMatchingProcessedLine` runs, and that query
   (`fetchProcessedByStatementData`, `:210-226`) reads only FINALISED and PUBLISHED rows anyway.
   MI_RECON leg 1 (`ClearingReportsReplicaRepository.java:95-129`) lists each EXCEPTION row under
   its own `uid`, so the report repeats the payment once per copy.
   Seen in fleet 2: 126 unallocated bank entries became 5809 EXCEPTION lines, 163 copies of one
   entry in 16 minutes, and MI_RECON listed 5815 items. hot-sauce-bank held each entry once.
   Reach in a deployed environment: the scheduler polls each period once, so the copies need a
   period read twice: a retry after a failed poll, a manual ops poll, or an end-of-day statement
   over entries the intraday polls already read. The last is not yet shown.
   Evidence: `findings/fleet2-mi.json`, and the rows in clearing until the next wipe.

8. **With no SFTP push for a bank, the Direct feed never ships an interest-only day, and RECON
   never seals.** `InvestecFileRepository.minOpenTransactionValueDate` lets INTEREST choose the
   feed date only after a file for the current day has `sent_at` set. adapter sets `sent_at` only
   when a push sink exists (`DirectFileOutboundPipeline.java:102-106`). RECON then waits for
   those unsent INTEREST rows (`DirectDataReconService.feedNotCaughtUp`) for good. Seen on every
   local bank: account `75b6367d` had its 0.01 of interest on 2026-10-12 left out while 10-11 and
   10-14 shipped, and fleet 3's bank had 846 feed files and no RECON. Reach: a deployed bank has a
   push target, so this needs a bank with none, or a push that keeps failing. What the feed
   selects depends on whether a file was delivered, which is the fragile part.
   The harness now marks sealed files sent before each feed call, in the place of the push.
   Once INTEREST is let back in, the skipped rows ship late: fleet 4 sent a row value-dated
   2026-10-09 in a file after the one for 2026-10-10, so a bank reading the files in order held
   a balance history with a gap until then.
9. **Two feed runs in one second write files with the same name, and the later one replaces the
   earlier in the archive.** The name carries the extract time to the second
   (`DirectFileMetadata.getFileName`), and the archive key is the name. Fleet 3's bank had 262
   names shared by two sealed files, for example `ACCOUNT_20260925T174423Z.csv` with 76 rows and
   with 0. The same runs sent 77 transactions in two sealed TRANSACTION files each, because a run
   reads transactions as open until adapter seals the earlier file. Reach: needs two runs within
   seconds, such as an ops run beside the scheduler. The harness now waits over a second between
   runs. Evidence: `investec_file` for bank `8089b2eb` until the next wipe.

10. **CancelAccountOpening answers 500 when clearing cannot be reached.** The cancel of a non-TERM
    account calls clearing's `softCloseAccounts` at `DirectCashWithdrawalService.java:75`, with no
    error handling on the path from `DirectCustomerAccountService.cancelDirectCustomerAccount`
    (`:144`), so a cut or a timeout becomes a bare 500 with no logref rather than an answer the
    platform can retry on. fleet 4 p0 trial 168 had the cut from core to clearing in force; two
    more (fleet 3 p3, fleet 4 p1) had no fault recorded. Evidence: `fleet4-p0.json`.
    CloseAccount does the same under the same cut (fleet 5 p0, `fleet5-p0.json`).
11. **Clearing's own integrity check finds a PUBLISHED statement line with no partner payment.**
    `ClearingIntegrityCheckService.java:128` (`PUBLISHED_ASL_NOT_LINKED_TO_PARTNER_PAYMENT`) failed
    on line `56f8686a` in fleet 3 and again in fleet 4. The cause is not shown yet; the lead is a
    line that drains before its payment due is raised, which `fund_account` avoids by raising the
    dues first. Evidence: `fleet3-p0.json`, `fleet4-p0.json`.
12. **A closure whose payment the bank refused or returned stays CLOSING after three sweeps.**
    fleet 3 p0 trial 247 and fleet 4 p0 trial 119. Each came next to a payment that clearing
    holds as RJCT while the bank paid it, so this is most likely finding 1 seen from the account's
    side. Not proven without the rows, which the wipe removed.

13. **FLAGGED_PAYMENTS counts more unresolved credits than MI_RECON lists.** Three cycles in a
    row: 800 and 666, then 542 and 362. The two reports are for the same bank and the same day, so
    either one query counts rows the other leaves out, or the gap is IGNORED duplicates from
    finding 7. Not traced yet; the local banks share one DIRECT master account in clearing, so the
    next check needs a bank with no cycle running on it.

14. **A TERM account opens for a requested amount far above the product's deposit maximum.**
    `DirectAccountValidator.validateAccountOpeningOrder` (`DirectAccountValidator.java:46-65`) checks
    only that a TERM amount is present; `AccountOpeningRequest.orderAmount` carries no bound, and
    nothing at opening compares it with `depositRequirementMax` or `maximumAvailable`. Fleet 11
    opened one for 99999999999999.99 against a maximum of 1000000 and got 201. Both limits are
    enforced when money arrives, against the deposit itself: `MaxDepositValidator` and
    `MaxAvailableValidator` run in `DIRECT_RAIL_POST_POLICY`, and a deposit is matched to its account
    by the account's own internal account, not by `requested_amount`. So no money is at risk; the stored `requested_amount` is shown to the
    platform while the account is REQUESTED, and a REQUESTED account blocks a second opening on the
    product. Low severity. Evidence: `fleet11-p1.json`.

15. **Closing a Direct account with cash on its internal account dead-letters the drain's
    settlement, so core never books it.** `DirectAccountClosureOperations.requestAccountClosure`
    calls clearing's `closeAndDrainInternalAccount` (`DirectAccountClosureOperations.java:126`,
    call at `:142`) for INSTANT and NOTICE. When the account holds withdrawable cash,
    `InternalAccountCreationService.closeAndDrainInternalAccount` (`:74-96`) creates the payout
    under `new PaymentDueUid()` inside clearing, so core never runs `createPaymentDueForAccount` and
    has no `payment_due_direct_customer_instruction` row. On settlement,
    `DirectCustomerAccountHandler` sends it to `DirectTransactionWithdrawalHandler.
    onExternalWithdrawal`, whose lookup (`PaymentDueDirectCustomerAccountService:193`) throws, and
    PaymentSettled retries to the DLQ. Every other Direct debit writes the link first.
    Fleet 12: dues `ceba65b4`, `2eb0328f`, `2c3af7ab`, `9bdb66b6` (1.00, 0.50, 0.50, 0.01), all on
    accounts CLOSING with `NO_LONGER_NEEDED`, PRODUCED in clearing, absent from every core table.
    Also from reading the code, not yet seen: the drain is a synchronous call inside core's
    transaction, so a later failure in `requestAccountClosure` leaves clearing's due and soft
    close behind while core rolls back. Evidence: the rows and the DLQ on the running stack;
    `fleet12-p0.json`.

Open, not yet explained: 500s from FundAccount, SettleWorld and CloseAccount while other
platforms' sweeps were in flight (8 in fleets 3 and 4). A wipe removed their stack traces. From
fleet 5 each cycle keeps its services' ERROR lines in `archive/<cycle>.tgz`, and every wipe now
dumps the four databases to `archive/pre-wipe-<time>-db/` first.

## Checked and holding

- A closure payment the bank refuses (REJECT_FAIL) leaves its money on the CLOSED account. This
  is the intended outcome: money that cannot be paid out stays on the account.
- A platform cannot read another platform's customer, balances or instructions: every probe in
  fleet 2 answered 400 "Customer not found".
- The Direct MI reports for the fleet 1 bank agree with each other and with core.
