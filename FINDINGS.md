# Product findings

Defects the harness has reached on images built from `origin/main`. Each entry says what happens,
where in the code, and where the evidence is kept. Harness faults are not listed here; they are in
the workbook.

Evidence paths are relative to this directory. `archive/<cycle>.tgz` holds that cycle's logs and
trials, and `<run>.json` holds each violation with its lead-up and the service's answer.

## Open

1. **A payment with no status from the bank is recorded as rejected.** SAV-11344. `ClearinghouseHsbcPaymentService.java:85`
   defaults a null status to RJCT. A connection that closes before the bank answers leaves the
   status null, so clearing holds RJCT while the bank paid (ACSC), and a retry pays twice.
   Evidence: cycle 48, payment `PEC000000100000B` (`chain48.json`).
   The double payment also blocks every later payout from the same customer cash account.
   The retry's settled DEBIT takes the account's `partner_payment` sum to one payment below
   zero. The next withdrawal's credit only brings it back to 0.00. Then `AvailableBalanceValidator`
   rejects that withdrawal's payout, but `insertAggregateUid` has already committed. The
   per-minute `PaymentDueScheduler` only picks up dues with a NULL `aggregate_uid`, so it never
   tries the payout again. The due stays EXPECTED and the instruction stays PENDING for good.
   Fleets 10 and 11 found four of these through the rule "an instruction does not stay PENDING".
   Cash accounts 26, 27, 136 and 256 are the four accounts whose RJCT payout was retried and
   paid twice. Account 136: bb4f0777 (`PEC0000001000205`) got RJCT with "Connection closed before
   response was received", and the bank paid it at 14:05:55. The retry `PEC0000001000289` paid
   again at 14:17:12, and payout `d25becd6` was rejected at 14:20:22 with "availableAmount = 0.0".
   Joel: a payout that stays stuck after a failed balance check is already known, so it needs no
   ticket of its own. SAV-11344 comment 79776 records the harness evidence.
   Seen (review 2026-10-01): `PAYMENT_ACCOUNT_NEGATIVE_BALANCE` failed in all 75 conductor runs of fleets 236 to 316, and "clearing and the bank agree on how a payment ended" fired 28 times in fleets 238 to 313. Last fleet316.
2. **On hold until it happens again: a PaymentSettled fails with "Could not find instruction for
   payment due".** The account being CLOSING is not the cause. `completeInstructionForPaymentDue`
   (`DirectCustomerInstructionService.java:200-202`) updates only a PENDING instruction linked to
   the due, so it throws when the due has no linked instruction (the closure drain, finding 15,
   SAV-11694) or when the linked instruction is no longer PENDING, for example cancelled by the
   closure before its payout settled (near SAV-11636). The message retries until the dead-letter
   queue. Cycle 48 (`060b83a9`, `chain48.json`) and fleet 5 (account `71cc626a`) cannot say which:
   the harness kept only 300 characters of each dead letter, cut before the payment due, neither
   archive holds the stack, and no database dump exists for either. The harness now keeps the whole
   message, and the box keeps each cycle's service-error log and dump, so the next occurrence can
   be traced. Box fleets 1 to 10 have not produced it.
   The fleet 10 part first filed here, "Unable to find direct accounts for payment dues" from
   `PaymentDueDirectCustomerAccountService.fetchAccountsForPaymentDues` (`:193`), is finding 15
   (SAV-11694); the box has reproduced it in fleets 3, 5, 6 and 9.
6. **A CLOSED customer moves back to ACTIVATED through a KYC status change.** SAV-11534 part 2.
   Again in fleet 1 (`fleet1-p0.json`, `fleet1-p1.json`, `fleet1-p2.json`).
   Seen (review 2026-10-01): 92 violations in 70 runs of fleets 236 to 314, one of them CLOSED to FROZEN (fleet303, `5d8af591`, reopened to ACTIVATED 0.35 s before an officer freeze). Last fleet314.

7. **Clearing inserts a bank entry again on every poll that reads it, and never deduplicates the
   copies it cannot allocate.** PROD-3976, whose fix is tracked in SAV-11009 (In Development).
   PROD-3976 was moved to Done on 2026-09-03 when it was linked as "implemented by SAV-11009", not
   when a fix landed: no commit on `origin/main` names it, and the code below is unchanged since
   SAV-11011. PROD-3976 saw it in production: one 1,737.00 credit was allocated from its camt.054,
   then reached the exceptions queue from both the camt.052 intraday report and the camt.053
   end-of-day report. Its fix, still to be done in SAV-11009: add `EndToEndId` to the duplicate key
   before the servicer reference, and compare against EXCEPTION rows as well. `AccountStatementLineRepository.insertAccountStatementLine`
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
   over entries the intraday polls already read, which PROD-3976 shows happening in production.
   Evidence: `findings/fleet2-mi.json`, and the rows in clearing until the next wipe.

8. **After a day with no feed file, one deposit can stop a bank's Direct feed for good.** Low: it
   needs an earlier day-long feed or RECON failure, but once it starts nothing recovers it.
   How the date is chosen: each `DirectDataFeedService` run sends one business date, the oldest
   open value date from `minOpenTransactionValueDate` (`InvestecFileRepository.java:207-233`), or
   the watermark + 1 when nothing is open. INTEREST is left out of that choice until a non-RECON
   file has `sent_at` after the bank's business date last moved (SAV-10950), because the nightly
   realisation is booked with the new live date. A run whose date is more than one day past the
   watermark (the last sealed RECON) holds and writes nothing (`DirectDataFeedService.java:160`).
   RECON seals a date only when no transaction dated up to it is unsent
   (`findAnyUnsealedTransactionValueDatedInWindow`). RECON never sends a feed file.

   Prerequisites, all of them, around the midnight when the date moves from D to D+1:
   - Day D holds only interest: no deposit or withdrawal dated D is unsent at the first feed run
     after midnight. Any such row would make that run pick D and close it.
   - D's interest is still unsent at midnight. It can only go out once RECON has sealed D-1, so
     this needs no feed file sent between RECON sealing D-1 and midnight: the feed is down for
     the rest of day D, or RECON for D-1 seals only after midnight (for example because
     `interestProcessingIncomplete` skipped it while the accrual run was RUNNING or FAILED). On
     the hourly schedule (`V20260521092320__SAV-10151_backfill_dm_data_feed_schedule.sql`,
     00:00 to 23:00) that is about 23 runs in a row sending nothing.
   - A deposit or withdrawal dated D+1 settles after the accrual run moves the date and before the
     first feed run after it. Without it, that run falls back to watermark + 1 = D and sends D.

   What follows: the first run after midnight leaves both days' interest out, picks D+1 from the
   deposit, and holds because D+1 is two days past the watermark (D-1). It writes no file, so no
   file has `sent_at` after the date moved, so every later run makes the same choice and holds.
   RECON for D skips because D's interest is unsent, so the watermark never moves. All four files
   stop for that bank. No ops endpoint recovers it: `reemitDirectFeedEntry` covers only CUSTOMER,
   ACCOUNT and PRODUCT entries, and a held run plans no file anyway.

   Fix: keep INTEREST in the date choice when it is dated no later than watermark + 1, which is
   the day being closed and so can never jump the run ahead. Add `OR at.value_date <= ?:watermark
   + 1` beside the `EXISTS` in `minOpenTransactionValueDate`. The nightly realisation, dated the
   live date, is still left out on the first run after the date moves, and a stuck bank recovers
   on its first run after deploy.

   Evidence: PR #12453 adds the scenario "A deposit on the live date does not stop the feed closing
   the unshipped day before it" (`direct_data_feed_recon_hold.feature:110`) and no production
   code. CI run 36232970688 failed on it alone: with RECON sealed for 2026-04-06, the 2026-04-07
   interest unsent and a deposit settled on 2026-04-08, the latest TRANSACTION file stayed on
   2026-04-06 where 2026-04-07 was expected. The scenario moves the clock a day at a time with no
   hourly runs between, which is how it meets the second prerequisite without an outage.
   Walkthrough video: `~/tools/feed-stall-video/out/feed-stall.mp4`.

15. **Closing a Direct account with cash on its internal account dead-letters the drain's
    settlement, so core never books it.** SAV-11694. `DirectAccountClosureOperations.requestAccountClosure`
    calls clearing's `closeAndDrainInternalAccount` (`DirectAccountClosureOperations.java:126`,
    call at `:142`) for INSTANT and NOTICE. When the account holds withdrawable cash,
    `InternalAccountCreationService.closeAndDrainInternalAccount` (`:74-96`) creates the payout
    under `new PaymentDueUid()` inside clearing, so core never runs `createPaymentDueForAccount` and
    has no `payment_due_direct_customer_instruction` row. On settlement,
    `DirectCustomerAccountHandler` sends it to `DirectTransactionWithdrawalHandler.
    onExternalWithdrawal`, whose lookup (`PaymentDueDirectCustomerAccountService:193`) throws, and
    PaymentSettled retries to the DLQ. Every other Direct debit writes the link first.
    In all four fleet 12 cases the cash was a partial withdrawal in flight: its internal leg had
    moved the amount onto the internal account, and closure came less than a second later. The
    drain paid that same amount to the customer under its own due, 0.3 to 0.9 seconds after the
    internal leg. Core had not yet published the withdrawal's own `CASH_ACCOUNT_CUSTOMER` due, and
    that due is what `withdrawableBalance` reserves, so the cash read as free. The customer was
    paid once, by the drain; the withdrawal stays PENDING and core booked no WITHDRAWAL on any of
    the four. Any cash with no due against it reaches the same path deterministically, for example
    a withdrawal payout the bank reversed into the customer's cash account.
    Existing e2e features close only after processing has settled, so the cash is always reserved
    or already at the bank, and none of them reaches the drain with cash. The new scenario is
    `account-closure/close_notice_account_drains_unallocated_cash.feature` in worktree
    `exchange.worktrees/closure-drain-e2e`, PR #12452. CI run 36228963409 reproduces the dead letter: core rejects the drain due's PaymentSettled four times with "Unable to find direct accounts for payment dues".
    Fleet 12: dues `ceba65b4`, `2eb0328f`, `2c3af7ab`, `9bdb66b6` (1.00, 0.50, 0.50, 0.01), all on
    accounts CLOSING with `NO_LONGER_NEEDED`, PRODUCED in clearing, absent from every core table.
    Also from reading the code, not yet seen: the drain is a synchronous call inside core's
    transaction, so a later failure in `requestAccountClosure` leaves clearing's due and soft
    close behind while core rolls back. Evidence: the rows and the DLQ on the running stack;
    `fleet12-p0.json`.

16. **SAV-11534 part 3. An officer's REJECT or CANCEL answers 200 and is then overwritten by the onboarding KYC
    flow.** The override runs on a separate thread (`FlowInterventionController.java:144-162`,
    `executorService.submit`). `validateNoPendingChecks` reads only the pending flags, which an
    onboarding check in `NEW` never sets. Both writes are plain `UPDATE customer ... WHERE
    customer_uid = ?` with no expected status: the override in `CustomerRepository.updateStatus`,
    the classify step in `updateRiskAndStatus` (`PersonFlowClassifyService.java:78-82`). The
    override's own publish re-reads the row (`CoreResultPublishingService.java:35`), so it
    publishes the classify step's ACTIVATED. Proven on `26b5835e` (DEACTIVATED at 30.127, ACTIVATED
    at 30.240, override run publishing ACTIVATED at 30.276) and `b52445d7` (CANCEL). The
    `kyc_check` audit rows say REJECT and CANCEL, and the customer reads ACTIVATED. Same shape as
    finding 6: from the code, a Direct close during onboarding lets the classify step publish
    ACTIVATED over CLOSED, and core allows CLOSED to ACTIVATED. Realistic trigger: an officer
    acting while a KYC provider retry is pending (`ProviderFailureScheduler`, every 10 minutes).
    No existing ticket found. Deterministic reproduction: a WireMock W2 stub with a delay on one
    surname, then the override during the delay.
    Seen (review 2026-09-30): 14 violations on ACTIVATED customers in fleets 150 to 226, and 1 in fleet231 (`927c7890`). Last fleet231.

17. **The Direct nominated account update accepts an account name of any length.** SAV-11695.
    `ExternalDirectBankAccount.accountName` has no `@Size`, so a 5000-character name answered 200
    (fleet 175, twice), while core stores the name in `payee_account.account_name varchar(255)`
    and no row with that name exists afterwards. The harness labelled it "reference", because
    its text mutation replaces the first text field it finds, which here was `accountName`.
    Seen (review 2026-10-01): 23 violations in 20 fleets, 238 to 313. Last fleet313.

18. **A redelivered PaymentExpectation re-aggregates a due already stored, and the trigger
    stops it with an ERROR.** SAV-11684. `PaymentExpectationAction.process` (`:78-82`) calls
    `insertExternalPaymentDue`, which treats a duplicate uid as a no-op (SAV-9995), then aggregates
    anyway from the message, with no aggregate (`:92-96`). The second aggregation mints a new
    aggregate uid and `prevent_aggregate_uid_update` rejects it: "Cannot change value of
    aggregate_uid once set". No money moves twice and nothing is left unaggregated; each
    redelivery logs an ERROR, which is a Sentry issue in deployed environments, and SQS is
    at-least-once there. Fleet 183, due `cfb96b8b`, from the harness's own duplicate delivery
    (FundAccountDuplicated). Low. Fix: skip aggregation when the insert was a no-op.

19. **SAV-11696. A realisation whose interest rounds to 0.00 books an INTEREST transaction that no webhook
    announces.** `RealisedInterestTransactionService` (`:64-83`) books the row when the interest,
    the bank fee or the platform fee is non-zero, under the comment "Do not insert a zero valued
    transaction", but returns a transaction for the SAVINGS_TRANSACTION webhook only when the
    interest is non-zero. So a 0.00 INTEREST row carrying a fee shows in the Direct API and never
    reaches the platform. Fleet 183, account `39c32852`, twice. Low.

20. **SAV-11697. PlaceWithdrawal accepts an empty instructionReference.** `ExternalDirectInstructionRequest`
    has `@NotNull @Size(max=36)` and no `@NotBlank`, so "" answers 201 (fleet 185 p3). Low.
    Seen (review 2026-10-01): 4 violations, fleets 244 to 295. Last fleet295.

22. **A redelivered TransferExpectation fails on the payment due's unique key and retries to the
    dead-letter queue.** SAV-11684. `PartnerPaymentDueRepository.insertInternalPaymentDue` (`:129-212`) has no
    `ON CONFLICT (uid) DO NOTHING`, unlike `insertExternalPaymentDue` (`:125`, SAV-9995), so the
    second delivery of the same expectation throws `duplicate key value violates unique
    constraint "partner_payment_due_uid_key"`, `PaymentExpectationConsumer` fails the message, and
    it retries until the DLQ. The first delivery already booked the transfer, so no money moves
    twice; each redelivery in production ends in the DLQ and its alarm. Fleet 187, seven failures,
    from the harness's duplicate delivery. Low.
    Not every `partner_payment_due_uid_key` dead letter is a redelivery: finding 32 sends a new
    transfer under an old uid, and none of its deliveries books it. Read the message's direction
    before counting a dead letter here.
    Seen (review 2026-10-01): the 11 dead letters of fleets 238 and 239 are finding 32, not redeliveries. Last fleet229.

24. **SAV-11699. One payment with no creditor name stops every outbound payment.** Clearing's
    `BatchedFileSender.sendFiles` (`:56`) sends the oldest ten files in order with no per-file
    catch, and `PartyIdentification` (`:47`) requires the account name, so one nameless file throws
    an NPE on every run and no file behind it is sent. The only escape is two overlapping runs, where
    the second skips the locked bad file at `acquireNonBlockingLock` (`:62`). High.
    Cause of the missing name: `DirectWithdrawalEgressService.resolveCounterpart` (`:79-84`) sends
    the payout expectation with only the sort code and account number of the nominated account that
    is current when the payout is raised. If clearing does not yet hold that account,
    `ExternalAccountService.createOrFetchExternalAccount` (`:237`) inserts it with no name, and
    `OutgoingPaymentService.getPaymentDetails` (`:109`) copies that NULL into `creditor_name`. On a
    CoP platform a replaced nominated account is not published to clearing until
    `NominatedAccountPublisher` drains its outbox (every 30s in a deployed stack), so a withdrawal
    whose payout is raised inside that window after a payee change pays a nameless account. Nine
    initiations locally: 552, 554, 563, 565, 600, 601, 633, 642, 643, eight from JourneyPayeeChange.
    Locally the drain never ran (scheduling is off and the harness did not call
    `/operations/processor/nominated-account/publish/drain`), which made the window unbounded; the
    harness now calls it on every settle. Databases kept in `archive/finding24-send-loop-db` and
    `archive/finding24-before-reject-db`. The same NPE makes ops `POST /operations/processor/payment/groups/process` answer a bare 500 after 2 to 6 s, which is every FundAccount and SettleWorld 500 since fleet 190. By 19:30 London there were 39 nameless initiations and 242 unsent APPROVED payments. No ops endpoint can clear it: `REJECT_FAIL` answers 400 "Payment group must be pending approval" for an APPROVED group, and `PaymentFileRepository.SELECT_FILES_TO_SEND` ignores the group status, so a rejected group's unsent file would still be picked first. Only a database change moves the file. The harness now fails such a group on every settle: it marks the payments sent and RJCT, moves the group to PENDING_APPROVAL, and sends the operator's REJECT_FAIL (log in `cleared-groups.jsonl`). It failed all 47 at 21:05 London on 2026-09-26, and sending resumed at once.
    Seen (review 2026-10-01): 3 payout violations, fleets 236 and 243; the harness's REJECT_FAIL of each nameless group is what triggers finding 32 on the box. Last fleet243.

25. **SAV-11111. A withdrawal pays out to a nominated account that failed Confirmation of Payee.** The same
    `resolveCounterpart` takes the current nominated account with no verification check, unlike
    `CustomerWithdrawalPayoutRaisingService` (`:152-159`). A withdrawal accepted before a payee
    change pays the new payee even when its CoP result is AWAITING_REVIEW, and clearing inserts it
    as VERIFIED because `CASH_ACCOUNT_NOMINATED.isTrustedExternal()` is true. Initiations 554, 565,
    601 and 643; customer `0bb2eb94`: link AWAITING_REVIEW at 16:02:23.192, payout due raised at
    16:02:23.537 to 10799988837491 and SENT.

26. **SAV-11701. A funded fixed-term account accepts top-ups through a pooled batch.** SAV-10844 refunds a
    top-up only on the rail path (`DirectTransactionDepositHandler` `:151-160`); the batch path
    (`DirectPlatformAccountHandler.handleExternalCredit` → `DirectSettlementPlan` `:44-68`) and
    `MaxDepositValidator` never check that the TERM is already funded. TERM `8bd7b8e5` booked 50.00
    then 5.00 (batch `81r0qrp46`); TERM `9c1138ce` booked 26 deposits of 3.00.

27. **Fixed by SAV-11198 (PR #12160, open). Deactivating a frozen customer cancels a held closure
    withdrawal whose money clearing has already moved, and core never books it.** #12160 books the
    savings debit and the cash credit when the internal transfer settles, so core and clearing both
    show the 3.00 on the customer's cash account, and the cancel correctly moves nothing. Joel: the
    money then staying on a deactivated customer's cash account is intended, because a compliance
    intervention needs manual work to get the money to the right place. Not yet run against #12160's
    branch. Fleet 192, customer `aee6c983`, INSTANT account
    `7ee36205` with 3.00: FROZEN at 17:05:58; the account closed at 17:06:11 and raised full-balance
    withdrawal `32228427`; clearing's INTERNAL due `6b214939` went PRODUCED at 17:06:17 and credited
    3.00 to the account's cash balance (`cash_account_balance` on internal account 985, now
    SOFT_CLOSED); `DirectWithdrawalTransferExpectationHandler` held the withdrawal because the
    customer was frozen; DEACTIVATE at 17:09:20 ran `FrozenInstructionCancelService`, which cancels
    the instruction and "moves no money". Core now holds the CLOSED savings account at 3.00 with no
    withdrawal transaction, clearing holds the 3.00 on a soft-closed cash account, and nothing pays
    it out. The hold withholds only the payout, so a held withdrawal has already moved money that
    the cancel does not put back or book.
    Seen (review 2026-10-01): once, fleet313 (`18bcded5`, 0.01, held for a frozen customer and then cancelled, so the money stays in the cash account). Last fleet313.

29. **SAV-11702. A TERM funded between midnight and 01:00 London in summer matures a day early.**
    `DirectTransactionDepositHandler.processOpeningPayment` (`:321`) sets the maturity date when the
    opening payment clears, through `CustomerProductAccountMaturityService.upsertMaturityDate`
    (`:47-49`), whose start date is `timeProvider.getLocalDate()`. `AbstractTimeProvider` (`:16-17`,
    libs) returns that date in UTC, while the services, the customer and the bank work in
    Europe/London. During BST, funding after London midnight and before 01:00 still reads the UTC
    date of the day before, so `BondsmithBankCustomMaturityDateFormula` adds the term to the wrong
    day. Two one-month TERMs funded at 00:11 and 00:13 London on 2026-09-27 (customers `5546cb07`
    and `c1a8905a`) got maturity 2026-10-26 where the rule gives 2026-10-27: one day less of the
    term and one day less interest than the customer was promised.

The 500s from FundAccount, SettleWorld and CloseAccount are ops-api's read timeout. Fleet 181
recorded every one as `POST /operations/processor/payment/groups/process` answering 500 after
30.0 s: ops-api gives up on clearing at 30 s and answers a bare 500 with no logref, while clearing
sends each payment to the bank. Here the bank simulator was slow because the harness had filled it
with 7.9 million virtual accounts, so this is mostly a harness condition; the part that is the
product's is the same as finding 10, a timeout surfaced as a bare 500.

30. **SAV-11700. Ops can REJECT_FAIL a payment group whose payout has already settled.** A payout
    whose connection closed before the bank answered is held as RJCT (finding 1, SAV-11344) and its
    group waits in PENDING_APPROVAL. The bank had paid, so the statement line settled it anyway and
    core completed the withdrawal (`DirectTransactionWithdrawalHandler.java:84,106` completes only
    on `PaymentSettled`). REJECT_FAIL then sent `PaymentFailed`. `InstructionFailureService.
    handleDirectPaymentFailure` (`:166`) calls `cancelInstructionGroup`, whose update matches only
    PENDING instructions (`DirectCustomerInstructionRepository.java:138-144`), so it throws "No
    instruction group found to cancel" and the message retries to the DLQ. Had it gone through, the
    amount would have moved back to savings for money already paid out. Box fleet 7: withdrawals
    `b4c2d309`, `a7cf8671`, `5b41b35b` (1.00 each, COMPLETED by 14:00 UTC), bank ACSC on
    `PEC00000010001FD`, `PEC0000001000201`, `PEC0000001000207` at 14:05, the harness's
    `decide_refused_groups` REJECT_FAIL at 14:17 (`refused-group-decisions.jsonl`), three dead
    letters at 14:17.

31. **NOMINATED_ACCOUNT_SYNC_CHECK reports every customer whose payee is still under review.** Core's
    integrity check lists ACTIVATED individual customers with an active nominated account link last
    updated more than 30 minutes ago, and expects clearing to hold an active external account for
    each one (`DbIntegrityCheckReplicaRepository.findActiveCustomersWithNominatedAccounts`,
    `DbIntegrityCheckReplicaRepository.java:780-789`). Its query has no condition on
    `verification_state`. The publisher sends a nominated account to clearing only once it is
    VERIFIED (`CustomerNominatedAccountRepository.PENDING_EXTERNAL_ACCOUNT_PUBLISH`), so a link in
    AWAITING_REVIEW never reaches clearing on purpose, and the check fails for it from 30 minutes
    after the last change until ops decide the review. In production a review can stay open for
    days while ops contact the customer, so the check fails on every run in that time. The fix
    belongs in the check: leave out links that are not VERIFIED. Box fleets 150 to 229: the check
    failed in all 77 conductor runs with the same 23 customers, each with one link in
    AWAITING_REVIEW and `external_account_published_at` NULL, for example `dd55b124` (updated
    2026-12-31 08:40), `240771fa` (2026-12-31 08:46) and `482a3adc` (2027-01-03 19:49), fake
    time. `b7h.integrity.disabled-checks` can switch the check off per environment; no environment
    in the repo sets it.
    Seen (review 2026-10-01): 39 conductor runs of fleets 236 to 316. Last fleet316.

32. **A failed Direct withdrawal payout never returns to savings in clearing, so both Direct
    reconciliations fail.** No ticket: #12160 (SAV-11198) removes it for every withdrawal made after
    it is released. On main, `InstructionFailureService.handleDirectPaymentFailure` cancels the
    instruction and sends the cash-to-bank-VA transfer under `internalPaymentDueFor`, which is the
    withdrawal's own INTERNAL due. Clearing already holds that due as PRODUCED (the earlier
    bank-VA-to-cash move), so `insertInternalPaymentDue` throws `partner_payment_due_uid_key` on
    every delivery and the message dead-letters with reference `UNABLE_TO_PROCESS`. Core counts the
    money back in savings; clearing still holds it in the cash account.
    `INVESTEC_GBP_DIRECT_PRODUCT_RECONCILIATION` and `INVESTEC_GBP_DIRECT_PLATFORM_CASH_ACCOUNT_RECONCILIATION`
    then fail by the same amount on every run. Evidence, box 2026-09-30: every core payment due
    exists in clearing; 16 cancelled withdrawals leave 48.83 in eight cash accounts, for example
    `04967d07`, whose four cancelled withdrawals (15.17) match its four dead letters (0.5, 7.33,
    0.01, 7.33). The rest of the reconciliation gap moves between runs with withdrawals in flight.
    On #12160, stage 3 debits savings before the payout, so a failed payout goes through
    `transferCashToSavingsAccount`, whose new DEPOSIT instruction gets a new INTERNAL due, or leaves
    the money in the cash account, where core and clearing agree.
    Seen (review 2026-10-01): the harness reported the reconciliation in fleets 237 and 238 only, because its baseline drops a check that already failed; core logged 84 failed runs of 86 by fleet246, the gap still growing. Fleet250's two money-conservation breaks are the same cause. Last fleet250.

## Checked and holding

- Candidate from code reading, not yet driven: the Direct transaction list's amount filter
  compares the signed `customer_amount` (`DirectCustomerTransactionService`), while `MoneyString`
  refuses a negative bound, so no `valueAmountFrom`/`valueAmountTo` pair can select withdrawals by
  size. JourneyReadModels now exercises the filters.
- Candidate from code reading: ops `POST /operations/own/account/{id}/term/maturity/break`
  (`DepositBreakingService`) runs the Trust break flow and shows no Direct guard.

- Under investigation, not yet a finding: INSTANT accounts left CLOSING with a live interest
  schedule keep accruing and realising and never reach the closure sweep, which needs both
  next-value dates NULL (`DirectCustomerAccountRepository.fetchClosingAccountsReadyForFinalisation`).
  `closeOffSchedulesForClosingDirectAccounts` only nulls a schedule whose `realised_last_value_date`
  equals the run's new business date. Five CLOSING accounts on bank `3bf7a390` sit one day behind
  the OPEN ones (realised 2028-11-18 against 2028-11-19), so every close-off misses them. The
  harness advances this bank from two places at once, so a second interest run for the same bank
  may cause the one-day gap; production runs one run per bank. Account `891d326f` is one.
  Fleet 10 tripped the rule "an account does not stay CLOSING" on INSTANT account `6245916a`,
  CLOSING for 169 days with no withdrawal instruction. On its bank, about 20 CLOSING INSTANT
  accounts have realised to 2029-01-20, and many OPEN accounts to 2029-01-21.

- An instruction PENDING behind `PotentialDuplicateGate` is intended. A repeated 1.00 payout to
  the same nominated account fails the gate and waits for an ops approval, which the harness
  never gives. Instruction `62134d8f`, payout `94c7c016`, stays SENT.

- A withdrawal booked while its customer is FROZEN is intended when the payout was released
  before the freeze: fleet 185's 0.01 was paid by the bank 1.7 s before the freeze and booked when
  the statement confirmed it. The hold is decided when the PLATFORM payment due is raised, and the
  harness rule now judges the status at that moment.
- A SAVINGS_TRANSACTION event dated eleven minutes before its transaction was the harness's own
  doing: ops-api's PUT /webhook/{uid}/resend only backdates the row by 15 minutes for core's
  resend job, which is off locally, so nineteen harness PUTs sent nothing. In production a single
  timed-out delivery is resent by that job.
- FLAGGED_PAYMENTS counts more unresolved credits than MI_RECON lists. By design: the daily
  MI_RECON leaves out unclaimed platform credits younger than one hour
  (`ClearingReportsReplicaRepository.java:233-236`) and the monthly count does not
  (`:519-522`), as the integration tests state. Four pairs rerun: the gap equals the young
  credits each time. The harness check counted young credits at check time, not report time.
- Two feed runs in the same second write files with the same name, and the later one replaces the
  earlier in the archive. Joel: not important in practice, because runs seconds apart do not happen.
- A TERM account opens for an amount above the product's deposit maximum. Joel: the stored requested
  amount is not used anywhere in practice, and the deposit limits hold when money arrives.
- A NOTICE closure whose payout the bank refuses, and ops then REJECT_FAIL, stays CLOSING with its
  balance. `InstructionFailureService.handleDirectPaymentFailure` cancels the withdrawal (`:166`),
  moves nothing back because a CLOSING account takes no instruction, and logs ERROR "closure cannot
  finalise until it is paid out manually" (`:191-196`); the sweep closes NOTICE only at a zero
  balance (`DirectCustomerAccountRepository.java:127`). Joel: intended, ops pay it out by hand.
  INSTANT differs: its sweep ignores the balance, so it closes with the money on it (the REJECT_FAIL
  note below). While CLOSING, the customer cannot open another account on that product. Box fleet5
  account `710d1bbb`, 3.00, CLOSING since 13:45 on 2026-09-27; the harness's "a closure the bank
  refused still finishes" rule still reports it.
- A closure payment the bank refuses (REJECT_FAIL) leaves its money on the CLOSED account. This
  is the intended outcome: money that cannot be paid out stays on the account.
- A FEES transaction carries the wall-clock date as its value date, not the bank's business date.
  All three writers do this: `PlatformFeeWithdrawalRepository.movePlatformFeesToBondsmithPot`
  (`?:createdAt::date`), `BondsmithFeeWithdrawalOrderService` (`:122`,
  `timeProvider.getLocalDate()`) and the Trust order-adjustment insert in
  `CustomerAccountBalanceRepository` (`current_date`). Every other transaction takes
  `bank_business_date`. The only reader affected is `get_cpa_end_of_day_stats`, which totals
  `platform_withdrawal_amount` and `bondsmith_withdrawal_amount` by `value_date`, so a withdrawal
  on the wrong day makes both days' pot start balances wrong. The two dates match once the day's
  accrual run has moved the business date on (`DirectModelInterestProcessing.execute`), and the
  scheduled fee withdrawals (`FEE_WITHDRAWALS`, daily 04:30 in the test fixtures) run after the
  accrual run (00:00). So the dates differ only when a fee withdrawal runs before that night's
  accrual run finishes: the accrual run is late or failed, which is a much worse fault by itself,
  or an ops withdrawal lands in the minutes after midnight. Joel: as long as fees come after
  accruals, this does not happen. Box fleets 3, 5, 7 and 8 reported 12 of these rows under "a
  transaction booked after an accrual's cutoff is dated after that day", because the harness runs
  the business date months ahead of the clock.
- Core opens, funds and closes a Direct account that clearing never created, and leaves its money
  on the CLOSED account. Core asks clearing for the account asynchronously
  (`DirectInternalAccountCreationService.java:65,82`) and never checks that clearing made it, so an
  empty INVESTEC GBP DIRECT preloaded pool makes clearing's `AccountRequested` handler throw while
  the account opens, takes money and closes (cycle 48, account `4f41bd4e`, due `ec7ec4d8`,
  `chain48.json` violations 20 and 21). Joel: this does not happen in prod, because ops watch the
  virtual account counts closely, so the pool does not run dry. The INVESTEC auto-preloader
  (`InvestecAccountAutoPreloadScheduler`) runs only with `b7h.clearing.investec.auto-preload.enabled`,
  and the harness keeps the pool full, so it reaches this only on purpose.
- CancelAccountOpening and CloseAccount answer 500 when core cannot reach clearing. The clearing
  call (`softCloseAccounts`, `DirectCashWithdrawalService.java:75`) runs inside core's transaction,
  so a failure rolls core back, and clearing's soft close is a plain `UPDATE ... SET access_status =
  'SOFT_CLOSED'` that is safe to repeat, so the platform can retry. Joel: a 500 is acceptable here
  for now. The body says "please contact support with the logRef below" with `logref: null`, and
  core logs the error with an empty `traceId`, so support cannot link a platform's call to the log;
  that comes from `GlobalExceptionHandler` (`b7h.libs.micronaut`) and affects every unexpected 500.
  Box fleet1 p3 trial 182, under the harness's cut of core's calls to clearing.
- Clearing's `PUBLISHED_ASL_NOT_LINKED_TO_PARTNER_PAYMENT` check
  (`ClearingIntegrityCheckReplicaRepository.java:24-30`) fails for about a second after a statement
  line is published, because the relay consumer writes the `partner_payment` row 0.3 s (median) to
  2 s (p99) later and the check has no grace period. The harness runs it straight after funding,
  so it lands in that gap: 5 of 82 runs on the Mac, and once each in box fleets 7 and 8, every one
  followed by a pass. Production alerts only after 4 failures in a row, so it never alerts.
  Joel: move to holding. Two paths would leave a line unlinked for good, from the code and never
  seen: `AccountStatementLinePublisher.java:61-65` swallows a relay send error after the line is
  marked PUBLISHED, and the relay processor only logs an unknown account.
- A NOTICE closure can leave one accrual row for a day after the account stopped earning: the
  per-bank accrual run (`InterestProcessing.accrue`, `:38-44`) lists accounts in one transaction and
  accrues each later without re-reading the schedule that
  `DirectNoticeWithdrawalOperations.drainIfClosing` (`:109-125`) has just nulled. Joel: fine, the
  accrual never realises, so no interest is paid. Mac account `5f3b4e67`, accrual 321569 written
  56 ms after notice 341 was processed.
- A platform cannot read another platform's customer, balances or instructions: every probe in
  fleet 2 answered 400 "Customer not found".
- The Direct MI reports for the fleet 1 bank agree with each other and with core.
