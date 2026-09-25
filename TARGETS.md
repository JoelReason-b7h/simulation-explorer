# Targets for the harness

Behaviour worth driving, taken from main as of `4a386edee8` (2026-09-25). Each entry names what
changed or broke, and what the harness should see afterwards.

## Recent changes on main

- SAV-11278 (`ccbb5b8`): a Direct withdrawal is capped by the available balance, which is the
  ledger balance less other pending outbound. Two overlapping withdrawals on one account must not
  both pass. A zero-available closing withdrawal ends CANCELLED.
- SAV-11590 (`add0f82`): the pooled payment-due list no longer defaults to AWAITING when a
  `paymentId` or `raisedOn` filter is given, so a settled due is returned by its payment id.
- SAV-11067 (`4c2ffba`): every webhook is written inside its business transaction. A fault at
  commit must leave both the business row and the webhook row, or neither.
- SAV-11572 (`6af1ca2`): schedule triggers share `ScheduleManager`, and `StuckScheduleReaper`
  reclaims a job left RUNNING. The harness turns the reaper on. A restart in the middle of a
  schedule must end with the job reclaimed and run again.
- PROD-4089 (`868fa0c`): each order in an API batch is judged on its own verdict. One valid and
  one underfunded order in one batch give one CONFIRMED_SENT and one REJECTED.
- SAV-11198 (`cef6691`): a withdrawal completes when either the savings leg or the cash leg books.
- SAV-11499 (`8ec4cac`): a pooled deposit funded to the hub but not published can be cancelled,
  which reverses the hub-to-platform leg through a REVERSE_TRANSFER due.
- SAV-11459 (`c8ef7c2`): a duplicate CoP check is refused while an unanswered attempt younger than
  five minutes exists.
- SAV-10837 (`02c4dbac`): ops can re-emit the latest sealed feed entry.
- SAV-11566 (`6105714`): an account publish that keeps being rejected dead-letters, ops can
  requeue it, and the entries behind it still deliver.
- SAV-11597, SAV-11682: the Direct MI reports. `checks/direct_mi.py` checks them.

## Past defects to keep hitting

- SAV-10233, IMBC-384: concurrent withdrawals against a balance that covers one of them.
- SAV-11092: reopening a product must not bring back a CANCELLED or CLOSED account row.
- SAV-10577: back-to-back nominated account changes must publish the final state once.
- SAV-10999: a replayed TERM maturity must not book twice.
- SAV-10651: concurrent instructions on accounts that share a customer must not deadlock.
- SAV-10940: a quiet day must still ship its closing feed file after RECON seals.
- The open findings in `docs/simulation-testing-investigations/2026-09-18-harness-findings.md`:
  OpenAccount 500 beside a CLOSING account, SetMaturityDestination on an unfunded TERM account, a
  maturity lookup 500 with two live TERM accounts, an illegal KYC move answering 500.

## Races

- A cancel against the bank file claim of the same notice order: never both CANCELLED and
  BANK_WITHDRAWAL_INSTRUCTION_SENT.
- A customer close against a cash withdrawal or an API order: `CustomerActionGate` guards only
  some of these paths.
- Two platforms opening accounts at once while the preload runs: no preloaded account is given
  twice.
- Two platforms on one bank advancing the business day at once: one runs, the other is refused
  with "Job is already in progress", and the accrual is booked once.
- A global settlement sweep started by one platform while another platform is in the middle of a
  withdrawal.

## Files the checks read

- `checks/direct_feed.py <bankUid> --generate N`: the CUSTOMER, ACCOUNT, PRODUCT, TRANSACTION and
  RECON files under `s3://upload/archive/direct/<bankUid>/`.
- `checks/direct_mi.py <bankUid>`: MI_RECON, FLAGGED_PAYMENTS and ONBOARDING under
  `s3://upload/archive/direct-mi/<bankUid>/`.
