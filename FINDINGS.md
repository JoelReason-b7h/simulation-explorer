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
4. **Core opens, funds and closes a Direct account that clearing never created, then leaves its
   money on the CLOSED account.** Core asks clearing for the account asynchronously at opening
   (`DirectCustomerAccountOrchestrator.java:81-82`) and never checks that clearing made it. In
   cycle 48 the INVESTEC GBP DIRECT preloaded pool had 0 AVAILABLE rows, so clearing's
   `AccountRequested` handler threw and dead-lettered. The account still opened and took 3.00,
   because Direct funding moves money to the bank's virtual account and never touches the missing
   account. `closeAndDrainInternalAccount` succeeds on an unknown account (the lock, the balance
   read and the soft close all no-op), so CLOSING commits. The closure sweep has no balance or
   clearing check (`DirectCustomerAccountRepository.java:119-130`), `closeAccount` writes CLOSED
   first, and the payout `TransferExpectation` names the missing account, so clearing throws "No
   account found" and dead-letters it after 13 deliveries. Nothing in core notices. No injected
   fault was live at any step. It can happen in a deployed environment whenever the pool runs dry,
   and the INVESTEC auto-preloader is off by default in prod. The code path is unchanged on
   `origin/main`. Evidence: `chain48.json` violations 20 and 21, account `4f41bd4e`, due `ec7ec4d8`.
   The harness now keeps the pool full, so it only reaches this on purpose.

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

8. **A day with only interest can stop a bank's Direct feed for good.** The feed date comes from
   `minOpenTransactionValueDate` (`InvestecFileRepository.java:207-233`). INTEREST may choose the
   date only once a file has `sent_at` after the last date flip, a gate added by SAV-10950
   (#11942). The ship query and RECON's `feedNotCaughtUp` treat INTEREST as an ordinary row.
   - With a sealed RECON (watermark W), from reading the code, not yet run: if day W+1 holds only
     interest, that interest did not ship before the next flip, and a deposit dated W+2 exists
     before the next run, then every run picks W+2 and holds (`DirectDataFeedService.java:160`).
     All four files stop, nothing is sent, so the INTEREST gate stays shut, and RECON for W+1
     waits on that interest. Nothing recovers it. A push sink makes no difference.
     Realistic trigger: RECON misses its one daily slot on an interest-only day, such as a weekend.
   - With no RECON ever sealed and no push sink (local, and dev when #11942 merged), an
     interest-only day is skipped, and RECON never seals (fleet 3: 846 files, no RECON). With a
     push, the skipped rows ship late and out of order (fleet 4: account `9c7304b7` balance chain
     broken on 10-10).
   A draft scenario for `direct_data_feed_recon_hold.feature` asserts the day still closes; it is
   expected to fail on `main`. Not yet run.

10. **CancelAccountOpening answers 500 when clearing cannot be reached.** The cancel of a non-TERM
    account calls clearing's `softCloseAccounts` at `DirectCashWithdrawalService.java:75`, with no
    error handling on the path from `DirectCustomerAccountService.cancelDirectCustomerAccount`
    (`:144`), so a cut or a timeout becomes a bare 500 with no logref rather than an answer the
    platform can retry on. fleet 4 p0 trial 168 had the cut from core to clearing in force; two
    more (fleet 3 p3, fleet 4 p1) had no fault recorded. Evidence: `fleet4-p0.json`.
    CloseAccount does the same under the same cut (fleet 5 p0, `fleet5-p0.json`).
11. **Clearing's integrity check fails for about a second after every statement line is
    published.** `PUBLISHED_ASL_NOT_LINKED_TO_PARTNER_PAYMENT`
    (`ClearingIntegrityCheckReplicaRepository.java:24-30`) has no grace period, while the
    `partner_payment` row is written by the relay consumer 0.3 s (median) to 2 s (p99) after the
    publish commits. The harness runs the check straight after funding, so it lands in that gap:
    5 failed runs of 82 since the last wipe, each followed by a pass, and 0 lines unlinked now.
    `56f8686a` was one such row that fleet 4 read again from fleet 3. In prod the check alerts only
    after 4 failures in a row, so this is noise. Low; a grace window like the dropped-deposit
    check's would remove it. Two lasting paths exist in the code and were not seen: a publish
    whose relay message throws after the line is marked PUBLISHED
    (`AccountStatementLinePublisher.java:61-65` swallows it), and a relay processor that only
    logs an unknown account.

12. **A NOTICE closure whose payout is refused and failed stays CLOSING for good, still
    showing the balance.** After the bank rejects the payout and ops choose REJECT_FAIL,
    `InstructionFailureService.handleDirectPaymentFailure` cancels the instruction (`:166`) and
    logs ERROR "closure cannot finalise until it is paid out manually" (`:191-196`). The closure
    sweep needs a zero balance for NOTICE (`DirectCustomerAccountRepository.java:127`), a CLOSING
    account takes no new instruction, and nothing raises a new payout. From reading the code, with
    a deterministic scenario drafted; not yet run. With finding 1 upstream, the bank has paid,
    core keeps the balance, and a manual payout pays twice. The fleet 3 and 4 cases first filed
    here were a harness fault: the harness never brought the closure's notice withdrawal due, so
    no payout was raised. Fixed in `finalise_the_closure`.

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

Open, not yet explained: 500s from FundAccount, SettleWorld and CloseAccount while other
platforms' sweeps were in flight (8 in fleets 3 and 4). A wipe removed their stack traces. From
fleet 5 each cycle keeps its services' ERROR lines in `archive/<cycle>.tgz`, and every wipe now
dumps the four databases to `archive/pre-wipe-<time>-db/` first.

## Checked and holding

- FLAGGED_PAYMENTS counts more unresolved credits than MI_RECON lists. By design: the daily
  MI_RECON leaves out unclaimed platform credits younger than one hour
  (`ClearingReportsReplicaRepository.java:233-236`) and the monthly count does not
  (`:519-522`), as the integration tests state. Four pairs rerun: the gap equals the young
  credits each time. The harness check counted young credits at check time, not report time.
- Two feed runs in the same second write files with the same name, and the later one replaces the
  earlier in the archive. Joel: not important in practice, because runs seconds apart do not happen.
- A TERM account opens for an amount above the product's deposit maximum. Joel: the stored requested
  amount is not used anywhere in practice, and the deposit limits hold when money arrives.
- A closure payment the bank refuses (REJECT_FAIL) leaves its money on the CLOSED account. This
  is the intended outcome: money that cannot be paid out stays on the account.
- A platform cannot read another platform's customer, balances or instructions: every probe in
  fleet 2 answered 400 "Customer not found".
- The Direct MI reports for the fleet 1 bank agree with each other and with core.
