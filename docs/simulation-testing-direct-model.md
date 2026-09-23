# Simulation testing the Direct model — outside-in exploration harness

A stateful exploration harness over the Direct savings API. It talks HTTP only, declares no domain
model, and reaches deep states by remembering where it has been rather than by being told how the
system works. It runs **many simulated platforms concurrently**.

Evidence for the claims here lives in
[`simulation-testing-investigations/`](simulation-testing-investigations/) — an adversarial review
of an earlier draft of this document and an investigation into system patches, both with
`file:line` references. Where this document states a fact about the system, that is where it was
checked. Earlier drafts asserted things that turned out to be false — including all three
conservation identities in §8 — and the reports say which.

---

## 1. Scope, and which parts are choices

**Decided for V1:** INSTANT and NOTICE products; POOLED platforms; deposits and withdrawals, which
reach the system by two different routes. A deposit is never a `PlaceInstruction` call — it is a
`PlaceBatchPayment` followed by a platform credit (§2), and `PlaceInstruction` carries `WITHDRAWAL`
only. Both appear as `InstructionType` values on the reads, which is why the projector sees a
`DEPOSIT` it cannot place directly.

TERM and `TRANSFER` are out, which takes maturity, rollover and product transfer with them.
`MATURITY` / `ROLLOVER` / `PRODUCT_TRANSFER_*` remain statuses the projector must tolerate if it
ever sees them, and so does `RETURN` — the refund of a rejected inbound deposit
(`DirectDepositRefundService`). `RETURN` is INDIVIDUAL-rail only (the pooled handler has no refund
path), so adding the INDIVIDUAL axis brings it into play for real. SAYE is in the `ProductType` enum with no direct implementation — unreachable.

**POOLED is a scoping choice, not a property of the system.** `CustomerCreationStrategyFactory`
selects between INDIVIDUAL and POOLED strategies for direct platforms, the feature corpus declares
37 INDIVIDUAL platform rows against 30 POOLED, and `ScheduleEventType.java:268-270` states that transfer type
"says nothing about how a Direct Model platform operates (SAV-11158)". Only batch payments are
genuinely POOLED-only (`DirectBatchOrderHandler` validates it). Running INDIVIDUAL platforms
alongside POOLED ones is an available coverage axis later; it is not required to make the harness
work, and it is not blocked by anything.

Three things this scoping does *not* let us assume, all of which an earlier draft got wrong:

- **`depositInfo` is populated for POOLED, and the published description is wrong.**
  `DirectModelResponseMapper:170-179` returns a `DirectDepositInfo` whenever
  `accountStatus == REQUESTED`, with no transfer-type branch, and the two amounts it reads are
  always present: `DirectSavingsAccountRepository` selects `cash_balance` as a `COALESCE(..., 0)`
  and `requested_amount` off the account row. The `@Schema` description on
  `ExternalDirectDepositInfoResponse` says "Always null for POOLED customers", so the perimeter
  documentation contradicts the code and it is the documentation that is wrong. The harness must
  expect `depositInfo` present on every REQUESTED account, pooled included, and a projector that
  treats its presence as the INDIVIDUAL rail will mis-key every pooled account.
  One latent failure sits behind this: `DirectDepositInfo`'s three-argument constructor computes
  `requiredAmount` as `minDepositAmount.subtract(depositedAmount)` with no null guard, so a product
  whose `deposit_requirement_min` is null throws a `NullPointerException` on the read rather than
  returning a null field.
- **A POOLED account has its own `paymentReference`, and the perimeter description denying it is
  stale.** Confirmed against a running local stack on 2026-09-15: a customer created through the
  POOLED platform `481526d3-9419-47d2-9500-ce038892a357` opened a REQUESTED account whose read
  returned `paymentReference` `AR4JDWDZ8GJZM`, and the stored `direct_customer_account` row carries
  the same value. `DirectCustomerAccountService.createDirectCustomerAccount` calls
  `DirectPaymentReferenceGenerator.generate` unconditionally, with no transfer-type branch, and
  `DirectPerCpaBackfillService` exists to move pooled accounts onto a per-account internal account
  with a reference of their own. So the `@Schema` text on `ExternalDirectSavingsAccountResponse.paymentReference`,
  "Null for pooled accounts, which fund in bulk under the platform's own reference", no longer
  describes the system. The harness must not use a null `paymentReference` to tell the two rails
  apart, because on this platform it is never null.

- **The KYC last-name control only applies to `UNVERIFIED` platforms.** A `VERIFIED` platform skips
  the KYC fake entirely (`CustomerCreationStrategyFactory:78-81`), and 29 of the 30 POOLED direct
  platform rows in the corpus are `VERIFIED` with names containing no `pass` (the 37 INDIVIDUAL ones
  are all `UNVERIFIED`, which is where the surname control does apply). So `ACTIVATED` is not
  automatically gating, and a neutral surname does not strand a journey.
- **The KYC fake has more levers than `pass`.** `DefaultCheckResponseProcessor:33-63`: `sanc` →
  sanction TRUE_MATCH, `idv` → IDV INCONCLUSIVE, `advm` → adverse media INCONCLUSIVE. That is the
  cheap route into FROZEN, DEACTIVATED and intervention-pending states, and it should be a
  deliberate input, not a discovery.

---

## 2. Action catalogue

The harness speaks HTTP and nothing else. Tenant paths come from `specs/openapi-v1.yml` in the
**`b7hio/api-docs-portal` repository**, not from this one — the spec Investec reads, 31 operations
over 22 paths, 115 schemas. Nothing in the exchange repository holds that file, so a reader who
looks for it here will not find it. The exchange build generates its own specs into
`apps/*/build/classes/java/main/META-INF/swagger/*.yml`, which is what `scripts/diff-api-specs`
compares; those are a different artefact and the harness does not read them. Compiling against `direct-api-client` instead would couple the harness to the
implementation: the clients are built from the server's own source, so a field rename propagates
into the harness and the run stays green while every real integrator breaks. Ops and simulator
surfaces are not published; their paths are pinned by hand from the `*-api-common` operations
interfaces.

**The published spec lags the running service, so it cannot be the only source.** Checked on
2026-09-15 against the local stack: the 31 operations and 22 paths match, but `GET .../accounts`
returns six fields on each account that `SavingsAccountResponse` does not declare —
`paymentReference`, `lastAccrualDate`, `nextInterestRealisationDate`, `totalAccruedInterestToDate`,
`projectedMaturityValue` and `topUpDeadline`. No schema in the published spec mentions
`paymentReference` at all. Three of the six are the read-derived fields §4 builds its "do not move
the clock" argument on, so an explorer that reads only declared fields would never see the drift
those fields expose. The spec file in `b7hio/api-docs-portal` is dated June while the service is
built from main, which is the likely cause.

Two consequences. The harness must diff **every** field a response carries rather than only the
declared ones, because a field missing from the spec is exactly where a regression hides. And a
field the spec declares but the service stops returning is itself a finding, so keep both directions
of the comparison rather than treating the spec as the truth. `check_drift.py` in
`tools/simulation-explorer` reports both directions against a running stack.

**The drift reaches request bodies too, and there it stops the harness dead.** Placing a batch
payment against the local stack on 2026-09-15 failed three times, each on a different disagreement
between the published spec and the service:

- The spec declares `BatchPaymentRequest.batchPaymentReference`, and the service answers
  `"Unrecognized field"` — public API request types carry no `@JsonIgnoreProperties`, so an unknown
  field is a 400 rather than something ignored.
- The service wants **two** references where the spec declares one. `ExternalDirectBatchPaymentRequest`
  has `batchReference` (at most 36 characters, for idempotency and de-duplication) and
  `paymentReference` (at most 16, `^[a-zA-Z0-9]+$`, quoted on the funding payment to match it to
  this batch). The published spec has merged them into a single `batchPaymentReference` carrying the
  16-character pattern.
- The spec types `totalPaymentRequired` and each allocation `amount` as `number`; the service rejects
  a number with `"Monetary amount must be a decimal string such as \"1234.56\"."` because both are
  `MoneyString`.

So a body generator driven from the published spec cannot place a batch payment at all. Seed bodies
from the perimeter records under `apps/savings-exchange/api/public-api/direct/direct-api-common`, and
treat the spec as the list of operations rather than as the shape of what they take.

**Two different fields are called `paymentReference`, and confusing them is silent.** The account
read carries one, which names that account's own internal account. The batch carries another, which
is what the funding payment must quote. Crediting the pot with the account's value leaves the batch
unfunded and the account at REQUESTED while every call in the sequence still returns 200 — the dead
journey §3 warns about, produced by a name collision rather than by a bad value. The field
description says several batches may share one `paymentReference` and then settle together only when
a single payment matches their combined total, which is the sharing §6 counts as a journey shape.

**`FundAccount` needs a fourth step the design omits: wait for the customer to reach ACTIVATED.**
Placing the batch while the customer is still PENDING rejects every allocation with `"Customer is not
in a state to deposit"`, and the batch comes back `REJECTED` with `totalPaymentRequired` recomputed
to `"0.00"` — worth knowing, because the §8 per-batch identity holds trivially on a fully rejected
batch. Locally the customer reached ACTIVATED about two seconds after creation.

**The status field is not called the same thing on every entity.** The customer read returns
`customerStatus` and the account read returns `status`. §6 lists both as "status", so the projector
needs the field name per entity rather than one rule.

### Tenant actions — Direct API, per-platform OAuth client

| Action | Call |
|---|---|
| `CreateCustomer` | `POST /direct/v1/customers` (optionally with an initial order) |
| `UpdateCustomer` | `PUT /direct/v1/customers/{customerId}` |
| `AddNominatedAccount` | `PATCH /direct/v1/customers/{customerId}/nominated-account` |
| `CloseCustomer` | `POST /direct/v1/customers/{customerId}/close` |
| `OpenAccount` | `POST /direct/v1/customers/{customerId}/accounts` |
| `CancelAccountOpening` | `DELETE /direct/v1/customers/{customerId}/accounts/{accountId}` |
| `CloseAccount` | `POST …/accounts/{accountId}/close` |
| `PlaceInstruction` | `POST …/accounts/{accountId}/instruction` — `WITHDRAW` only in V1. The request field is `instructionRequestType` on `ExternalDirectInstructionRequestType`, which is WITHDRAW/TRANSFER; `InstructionType` on the batch allocation and the reads is a different enum, DEPOSIT/WITHDRAWAL. Sending `WITHDRAWAL` here is rejected. |
| `CancelInstruction` | `DELETE …/accounts/{accountId}/instruction/{instructionId}` — legal only for a `WITHDRAWAL` on a `NOTICE` account in `PENDING` |
| `PlaceBatchPayment` | `POST /direct/v1/batches` |
| `CancelBatchDeposits` | `DELETE /direct/v1/batches` — no path parameter, and it **takes a body**: `CancelBatchRequest {orders: [CancelBatchOrder {batchPaymentReference, cancelAll, instructionIds}]}` |

Eleven actions. The four webhook operations are not actions: subscription happens once at setup
(§9) and what arrives is output. Reads used as observations: accounts (list and by id),
instructions, balances, transactions (customer-wide and per-account), batches (list and by id),
customers, documents, products.

### World actions

A real integrator calls none of these. Everything except the incoming bank payment runs on cron, so
each entry is a test-stack accommodation that has to justify itself.

| Action | Call | Notes |
|---|---|---|
| `SimulatePlatformCredit` | `POST /simulator/direct/payment/platform/transactions` | The only irreducible world action — no scheduler behind it. Amount = batch total, reference = its `paymentReference`. **Must be called with the platform's own token**: the simulator credits `identityProvider.get().getPartnerUid()`. Publishes `TransactionSettled` straight to the clearinghouse queue — it does not book a statement line, and the HSBC poll is irrelevant to it |
| `DrainTransactions` | `POST /operations/hsbc/statement/transactions/process` | Drains what arrived. For a pooled credit the corpus then aggregates `SAFEGUARD_HUB` and runs a further send/process leg |
| `SettlePaymentsForAccount` | per-account settle — **needs patch P1 (§10)**; today only the global `payments/outgoing` → `payments/transfers` → `payment/groups/process` exists | Global today, which is the main attribution hazard (§5) |
| `AccrueAndRealiseInterest` | `POST /operations/batch/processor/bank/{bankUid}/ACCRUALS_AND_REALISATIONS/sync` | One call per simulated day. **Self-advances the bank business date** — see §4 |
| `ProcessDueNoticeWithdrawals` | `POST /operations/processor/direct/notice` | Bank-wide today; **patch P2** scopes it and takes an `asOf` |
| `ProcessDirectAccountClosures` | `POST /operations/processor/direct/account-closure` | Sweeps every CLOSING account today; **patch P3** scopes it per platform |

`RaiseBankPaymentDue` (`BANK_ORDER_PAYMENT_DUE` + `BANK_DISTRIBUTION_PAYMENT_DUE`) and
`RaiseCustomerPayout` (platform `DEPOSIT_PAYMENT_DUE`) are **probably not needed for direct**.
Direct payout dues (`CASH_ACCOUNT_CUSTOMER`) are created inline in clearing by
`CashAccountWithdrawalService:66-67`, and `PlatformDepositPaymentDueRaisingService` is
`EMONEY_HUB`-directional — the trust rail. The corpus invokes them, but its step is
`longAwaitWithActionUntil(raise, until raised)`, which passes identically whether the trigger did
the work or the due was already there. **They cannot be tested by omission on an environment where
scheduling is enabled** — the batch executor decides dueness from the injected clock and will raise
them anyway. Test by omission locally, where scheduling is off.

### Scheduling is off locally and on elsewhere

`scripts/launch_stack` forces `MICRONAUT_ENVIRONMENTS=acceptance`, and both core and clearing
acceptance profiles set `b7h.async.scheduling.enabled: false`. So locally every world action is
required — nothing happens unless the harness asks. On dev/sandbox/preprod they fire on their own.
That difference is the single biggest behavioural gap between environments and it cuts both ways:
locally the harness owns the world; remotely it shares it.

**How the flag actually works, and the two holes in it.** No consumer lives in this repository; the
flag is read by `ShedlockMethodInterceptor` in the external `b7h.libs.micronaut` shedlock library,
whose source is at `~/IdeaProjects/libs/micronaut/micronaut-shedlock`. Read it before relying on the
flag, because it gates less than the name suggests:

- **It only gates methods that resolve a shedlock `LockConfiguration`.** A `@Scheduled` method with
  no `@SchedulerLock` falls to `context.proceed()` and runs whatever the flag says. Of the 63
  production files in `apps/` that declare a `@Scheduled` method, eight carry no `SchedulerLock`:
  `ProductSyncScheduler`, `SftpRefreshDirectories`, `PartnerRegistry`, `UserManagementService`,
  `BankStatisticsCacheWarmer`, `ProductionOrderProcessingCache`, `GracePeriod` and `WebhookSender`.
  None of the eight writes direct customer, account, instruction or payment state, so the claim
  above holds for everything the harness treats as a world action — but it holds by inspection of
  that list, not by the flag, so re-check the list when a scheduler is added.
  Two of the eight still change what the harness sees, because they are 15-minute cache clears that
  keep running with the flag false: `WebhookSender.clearCache()` drops the webhook URL cache and
  `GracePeriod.clearCache()` drops the platform end-of-day cache. So a webhook subscription the
  harness changes mid-run is picked up on that timer rather than immediately, and the harness should
  subscribe once at setup (§9) instead of re-subscribing between cohorts.
- **A direct call ignores the flag and takes no lock.** The interceptor decides whether a call came
  from the scheduler by scanning the current stack trace for `io.micronaut.scheduling`
  (`wasScheduled()`), and when it did not, it logs "Running task … because it was called directly"
  and proceeds. Every world action in the table above reaches its job through an ops endpoint, so it
  runs regardless of the flag, and it runs **outside** the `@SchedulerLock` that would otherwise
  exclude a concurrent scheduled run. Locally that costs nothing because the schedulers are off. On
  a shared environment it means an ops-triggered sweep and a cron-triggered sweep can run at the same
  time, which §5 needs for the notice sweep in particular, where two concurrent runs collide on
  `dci_pdt_unique`.

The interceptor also sets the flag false on `ShutdownEvent`, so jobs stop before the application
does.

**The lever:** per bank, per event type, `POST /operations/batch/processor/bank/{bankUid}/{scheduleEventType}/status`
with `DISABLED`. The reserve SQL selects only `COMPLETED`/`FAILED`, so a disabled row never fires.
Disable the harness's own banks' jobs on a shared environment and it owns the tick for its cohorts
while the rest of the environment ticks normally.

---

## 3. What is declared, and what is discovered

**No preconditions and no effects.** The API rejects what it will not allow, with a status and a
message, which is a better account of the rule than a transcription. Preconditions that are too
strict make states unreachable with nothing to show it happened; effect lists that are incomplete
never aim at the effects they omit. Rejections are output, not failure.

Declared, and none of it is a domain model:

| Declared | Source |
|---|---|
| the action list and how to form each call | the published spec; ops and simulator paths pinned by hand |
| which identifiers each call needs | the spec's path parameters |
| where an identifier comes from | producer response field name → consumer parameter name, below |
| multi-call grouping | `FundAccount` = place batch → credit → drain → wait for OPEN. Plumbing, not semantics |
| when an action is finished | per-entity quiescence (§7) |
| whether an action spends an entity | one bit. `CloseCustomer`, `CloseAccount`, `CancelAccountOpening` (→ `CANCELLED`) — so the driver forks rather than burns. `CancelBatchDeposits` does **not** belong here: a partial cancel recomputes the batch status rather than terminating it |

### Bindings: output → input

The only real structure the harness has. The spec has six bindable path parameters after webhooks
are excluded — `customerId` (19 uses), `accountId` (8), and then `batchId`,
`instructionId`, `documentId` and `productId` at one use each — and response schemas carry the same names for the same things.
`SavingsAccountResponse` returns `accountId` and `customerId`; `BatchPaymentResponse` returns
`batchId`. Match producer field name to consumer parameter name and the graph builds itself.

This says which actions are *constructible* from what is currently held — a weaker claim than which
will succeed, which is why it is safe to declare. Name-matching also beats matching Java types:
`batchId` binds despite being a bare `UUID` in Java, `accountReference` and `paymentReference` bind
as strings, and the internal typed-UID re-wraps never reach the perimeter.

**Bodies are where journeys die silently, not paths.** `CustomerRequest` needs a valid `nino`, a UK
`phoneNumber`, a `GBR` address, `customerTaxResidencies` that must *not* be `GBR`,
`fscsAcknowledgedAt` not in the future, `dateOfBirth` at least 18 years back, and a coupled
`isVulnerable` / `vulnerableDescription`. None of that is derivable from the spec beyond `required`.
The failure mode that matters is not a 400 — it is a plausible generated value that is accepted and
produces a dead journey: an `AccountOpeningRequest.amount` below the product minimum settles to
unallocated cash via `BELOW_PRODUCT_MINIMUM` and looks like a finding when it is an artefact of the
generator. Seed bodies from the spec examples, the `postman/` collections and the feature corpus,
and own the generated uniques (`customerReference`, `batchReference`, `instructionReference`,
`paymentReference`) explicitly.

**Cancelling a batch is the one action the binding graph cannot build, and it is worth stating why.**
`DELETE /direct/v1/batches` carries no path parameter, so there is nothing for a path-parameter rule
to match; the batch is named inside the body, by `batchPaymentReference` rather than by the `batchId`
the create call returned. `CancelBatchRequest` is `{orders: [{batchPaymentReference, cancelAll,
instructionIds}]}`, and `instructionIds` holds instruction *references* despite its name, so matching
it to the `instructionId` UUID produces a request the server rejects. Both fields bind from
references the harness minted itself, which means `CancelBatchDeposits` has to be constructed by
hand rather than derived. `BatchPaymentResponse` returns `batchId` and `batchPaymentReference` both,
so the harness must keep the reference alongside the id for every batch it creates.

**The references the harness mints have a format budget, and it is tighter than the replay scheme in
§6 assumes.** `BatchPaymentRequest.batchPaymentReference` is at most 16 characters and matches
`^[a-zA-Z0-9]+$`, so it takes no hyphen, no underscore and no colon. `InstructionRequest.instructionReference`
is at most 18 characters. `CustomerRequest.customerReference` allows 128 and `PersonRequest.personReference`
36. So "prefix every reference with a run id" only works if the run id is short and alphanumeric —
a UUID with its hyphens fails the batch pattern outright, and even stripped of hyphens it leaves no
room for a counter. Mint the run id as about six alphanumeric characters and budget the rest.

### The corpus rules are an expectation set, not input

Rules extracted from the feature corpus are encoded as a seed table the explorer is checked
against. Learned matches seed: the rule holds. Learned stricter: a regression. Learned looser:
something succeeds that should reject — a missing validation, and the direction a declared model
would hide. That check only works if attribution is sound (§5).

---

## 4. Time

**Do not call `SetClock`.**

There are four clocks, not three. `setClock` reaches `core:4000` and clearing; hot-sauce-bank is set
separately. The Direct API's *reads* go through `*ReadOnlyClient` → `core-ro:4001`, which
`setClock` never touches and `timeProvider.set()` is per-JVM. Move the clock and read-derived
fields — `projectedMaturityValue`, `nextInterestRealisationDate`, `topUpDeadline` — compute at
wall-clock while the write side is ahead.

It is also unnecessary. **Business date is already per-bank and persisted.** `BankDateProvider` /
`DatabaseBankDateProvider` store it per bank uid via `BusinessDateRepository`, live by default
(`logical-accrual-date` defaults true; `RealTimeBankDateProvider` replaces it only when explicitly
false). Direct accrual reads only the bank date and **self-advances**: accrue →
`setNextBusinessDate` → realise. So advancing a cohort n days is n calls to
`ACCRUALS_AND_REALISATIONS/sync` and no clock movement at all.

**The bank's business date is not readable over HTTP.** There is no get for it — the only
`businessDate` on the ops API is a path *input*, and `OpsPortalSetupOperations` has only `setClock`.
So the harness either starts every cohort on a **fresh bank**, whose date begins at global today at
approval, and counts its own advances from there; or it is blind to where a cohort sits in logical
time. Fresh-bank-per-run is the assumption until **P5** (§10) adds the read.

That also gives §11's "advance ~33 days" its unstated precondition: notice due dates are stamped
*global today + notice period* at placement, so advancing a bank's date past them only works because
a fresh bank starts at global today. On a reused bank whose date has drifted, the arithmetic differs.

Two consequences:

- **One bank per cohort partitions logical time.** Interest, and after P2 the notice period, advance
  per cohort without touching anything else.
- **`moveBankBusinessDate=true` destroys the partition** — it overwrites every bank's date with the
  global clock's. The e2e `Migration.setTime` always passes `true`. Never use that path.

Where the partition leaks: notice due dates are stamped from the global clock at placement
(`NoticeProductService:72,82`) *and* compared against it in the sweep, and clearing has no
bank-date concept at all — payment dues, the simulated settlement timestamp and poll windows all
read clearing's own clock. Payouts are unaffected because both sides stamp `now()` and aggregate on
`due_date <= now`. P2 closes the notice half; the clearing half does not matter as long as the clock
does not move.

---

## 5. Concurrency and isolation

Many platforms run at once. Two problems: getting them to exist, and keeping their effects apart.

### Getting them to exist

`platform_client_link` has `UNIQUE (client_id)` and `UNIQUE (platform_sid)` — one client, one
platform. Non-prod direct platform creation auto-links the single shared
`direct-model-test-clientId` with `ON CONFLICT (client_id) DO UPDATE SET platform_sid = <new>`, so
the shared client follows the **newest** platform and the previous one is left unlinked. (The
folklore that it binds to the *first* platform comes from public-api caching resolved
`Authentication` per JWT hash until half the token TTL — an old token keeps resolving to the old
platform. `tools/performance-testing/docs/authoring/direct-vs-trust.md` states this backwards.)

So N concurrent platforms need N client ids, repainted onto the desired platforms in the local DB
copy. Three things that follow:

- **Mint fresh tokens after repainting**, or the cache above serves the old binding.
- **Each cohort's `SimulatePlatformCredit` needs that cohort's credentials**, not a shared ops token
  — the simulator credits the token's platform.
- Secrets follow the existing pattern: `tools/performance-testing` keeps them in gitignored
  `data/auth/<env>.env` with `<env>-<profile>.env` overlays.

### Keeping their effects apart

The hazard is not correctness — the system holds per-account locks and is safe. It is
**attribution**. The harness learns from `(pre-state, action) → diff`; if a global sweep run by one
journey produces the diff another journey observes, the second journey records an edge that was
never caused by the action it is recorded against. Concretely: A places a withdrawal (PENDING,
nothing moves), B calls the global payment settle, A's withdrawal pays out, and A learns
"`PlaceInstruction` → payout completed". The driver then plans `PlaceInstruction` to reach a
payout, and it works whenever someone else's sweep happens to run. The relation carries a
non-causal edge whose failures look like flakiness, and §3's looser-than-seed check starts firing on
nothing — losing the signal that justifies the approach.

**The answer is to de-globalise, not to architect around it.** Inventory:

| Global today | After patch | Status |
|---|---|---|
| payment aggregation | per **account** | P1 — the per-account method already exists |
| notice sweep | per **bank**, explicit `asOf` | P2 |
| closure sweep | per **platform** | P3 |
| cron pollers | per bank/event | no patch — `status: DISABLED` |
| `AutomaticFundingScheduler`, `/processor/fund/*` | off | no patch — `sim-automatic-withdrawal-funding` flag, unset in acceptance; verify wherever you run |
| interest / accruals | per bank | no patch — one bank per cohort |
| `resetPollingPeriods` at injected midnight | n/a | moot while the clock does not move |
| `statement/transactions/process` | — | genuinely global; drains a shared queue |
| other people's activity on a shared environment | — | unpatchable |

Reference matching is already scoped to the receiving virtual account
(`PaymentDueForMatchingRepository:44`), so `paymentReference` collisions are intra-platform and the
harness's own problem. The preloaded internal-account pool uses `SKIP LOCKED` on an exact
four-tuple: race-safe, so it is a shared *exhaustion* point, not a correctness one — a sizing
concern (§9).

### Attribution: what the wire actually gives you

An earlier draft assumed causality tokens were available to match a diff to an action. Mostly they
are not, and the design has to be built on what is really there.

**Not available.** The transactions read carries `transactionId, accountId, amount, mark, type,
valueDate, productName` — no reference, no instruction id. The account read's `balance` carries
none. So the two diffs the §8 identities depend on can never be matched by token. Most webhooks
(`CUSTOMER_STATE_CHANGED`, `ACCOUNT_CLOSED`, `INTEREST_REALISED`,
`PAYMENT_PENDING_ALLOCATION`, `NOMINATED_ACCOUNT_ADDED`, `CUSTOMER_DATA_CHANGED`) carry entity ids
only, and `PAYOUT_FAILED.instructionId` is a clearing `CashInstructionUid`, not the
`DirectInstructionUid` the harness holds. `Idempotency-Key` is echoed **only on the response to the
request that carried it** — it never appears in a later diff, so it is not an attribution token at
all.

**Available.** Entity ids, on every read. The per-platform webhook URL. And one genuine token:
`SAVINGS_TRANSACTION` carries `reference` (the instruction reference) and `instructionId`.

Of the seventeen actions, thirteen mint no token the harness can later observe. So attribution is
**by entity identity**, not by token, and that carries a constraint worth stating plainly:

> A diff on entity E is attributable to an action only while **one action is in flight against E**.

That is weaker than it sounds. Different entities in the same platform are distinguishable, so
single-entity actions can run concurrently within a cohort. It bites on actions whose in-flight set
is large: `PlaceBatchPayment` touches a batch plus N customers plus N accounts, and every world
action touches the whole cohort. So:

- **Across cohorts: fully parallel.** Different platforms, different banks, different entities.
- **Within a cohort: serialise around any multi-entity or world action**, and run single-entity
  tenant actions concurrently between them.

That is what "many platforms at once" means here, and it is the concurrency the requirement needs —
platform-parallel, with a short serial section per cohort rather than a global one.

### And never guess a cause

Where attribution fails anyway — a diff on an entity with nothing in flight against it — **record it
as unattributed rather than pinning it on the last action.** That is the honest behaviour regardless
of which patches land, and the unattributed-diff rate doubles as the test of whether isolation
holds: near zero means the cohort boundary is real; climbing means something global is leaking, or
the in-flight rule above is being violated.

---

## 6. Abstract state, and the driver

### State needs a subject

The state key is **per entity**, not per journey: an account-level tuple joined to its customer's
status and product type. A journey-level key collapses "two customers, two batches, one shared
reference" into the same bucket as "one customer, one batch", which is the distinction worth
exploring.

Two things need pinning down, because naming the key is not enough:

- **Which key a trial is recorded against.** A single-entity action records against that entity's
  key. A multi-entity action (`PlaceBatchPayment` — one batch, N customers, N accounts) records
  against **the batch**, with the per-account diffs recorded as effects of that batch key rather
  than as separate trials. Otherwise one action inflates N+1 visit counts and the frontier
  calculation is wrong.
- **Journey-shape counters are separate from entity keys**: number of live customers, accounts per
  customer, batches sharing a `paymentReference`. These drive *what to construct*, while entity
  keys drive *what to do next to a thing that exists*. Conflating them is what made the earlier
  draft vague here.

Read the enums from the spec rather than restating them — all six are `enum` schemas — and treat a
value outside the list as a finding, not a crash.

```
customer.status      : PENDING | ACTIVATED | DEACTIVATED | FROZEN | CLOSED | CANCELLED
account.status       : REQUESTED | OPEN | CLOSING | CLOSED | CANCELLED
account.productType  : INSTANT | NOTICE
instruction.status   : PENDING | COMPLETED | CANCELLED | REJECTED
batch.status         : PENDING | SETTLED | PARTIALLY_ACCEPTED | REJECTED | CANCELLED

savings_balance      : zero | positive
withdrawable         : zero | partial | full
unallocated_cash     : zero | positive
nominated_account    : none | present
batch.outstanding    : full | partial | zero
```

`instruction.status: REJECTED` appears only inside a batch allocation result — the internal enum has
no REJECTED, the mapper produces it from a null status — so it never shows on `GET /instructions`.

States the projector will meet and must not be surprised by: funds left in the platform pot by
cancelled groups; `PAYMENT_PENDING_ALLOCATION` distinguishing `NO_ASSOCIATED_REQUEST` from
`BELOW_PRODUCT_MINIMUM`; account closure driving a *system-initiated* full-notice withdrawal
(`DirectAccountClosureOperations:193-223`); and `NominatedAccountVerificationState` with CoP, where
`BankAccount.accountName` becomes required.

**One state is not observable at all.** A `DEPOSIT_POOLED` hold — a frozen customer's deposit
sitting in the platform pot — maps to `PENDING` on the wire, and the `pending_count` that derives
batch status *excludes* held rows. So the hold has no direct representation the projector can read;
its only trace is the batch status anomaly recorded in §8. Do not model a held state the harness
cannot see; treat the anomaly as the observable and the hold as the inferred cause.

### The driver

Visit counts plus replay. No planner.

1. **Trial.** From the current state, pick the least-tried action among those the bindings make
   constructible from the identifiers held. Execute, snapshot, diff, attribute (or mark
   unattributed), record.
2. **Return.** Pick the entity-state with the lowest visit count that still has untried actions,
   replay the shortest stored journey that reaches it, resume trial.

Pure exploration stalls because it is memoryless — nothing tells it it has walked the shallow
region already. Memory plus a replayable corpus fixes that without anyone writing down what the
system does.

**Replay reaches an isomorphic state, not the same one.** Every replay re-mints
`customerReference`, `batchReference` (409 on reuse, unique per `(reference, platform)`),
`instructionReference`, `paymentReference` and claims a fresh preloaded account. Prefix every
reference with a run id. That is fine for visit counting and useless for "resume this journey after
30 days" — for that, the persisted-with-due-date model is right, and it works here only because
logical time is per-bank: a cohort can sit at day 3 while another advances to day 33.

---

## 7. Quiescence and observation

Every mutation crosses SQS or JMS and `AFTER_COMMIT` listeners. A 200 is not evidence the effects
ran: `TransactionSynchronizationUtils` catches `Throwable` per listener, logs ERROR and continues,
so a commit succeeds, the caller sees 200, and the listener silently did not fire. Assert on
observable outcome, and scrape service ERROR logs as a finding channel.

**Quiescence is per entity, not per action:** poll until the entity's own diff is stable across two
consecutive reads. A causality token strengthens that where one exists — `SAVINGS_TRANSACTION`
carries an instruction reference — but as §5 sets out, thirteen of seventeen actions mint none, so
stability-plus-in-flight-set is the general mechanism and the token is the exception. Fixed waits
are a scenario-player idiom and do not survive concurrency.

Two real signals, and one patch that would give a third:

- **Webhooks.** Subscribe through **ops-api**, not the public perimeter. The public
  `ExternalDirectWebhookSubscription` enforces `^https://.+` — a deliberate, separately tested
  contract — and TLS delivery to an acceptance loopback fails, which is why direct webhooks have
  never been delivered in acceptance. The ops endpoint validates only `@NotBlank` and takes the
  platform explicitly, so it registers a plain http loopback. Use the existing
  `LOOPBACK_URL + platformUid` shape: a distinct path per platform makes arrivals attributable by
  URL without parsing.
  **Version trap:** the wire value is `VERSION_1`, internally `WebhookVersion.VERSION_1_2` (hence
  '1.2' in feature files). Take it from `ExternalDirectWebhookVersion.VERSION_1.toInternal()` —
  hand-writing the internal version creates a subscription the sender never matches and **no
  webhook record is created at all**.
- **Outstanding webhook work:** `GET /webhook/events?platformUid=&eventState=AWAITING_RESPONSE`.
  Note the path: `OpsPortalPlatformWebhookController` is `@Controller("/webhook")`, without the
  `/operations/` prefix every other ops controller carries. Subscription is `POST /webhook/link`.
- **P4 (§10)** would add a per-platform outstanding-work read covering pending instructions,
  unprocessed notices and CLOSING accounts — replacing settle-timeouts for everything core-side. Note the
webhook-event path is `/webhook/events`, not `/operations/webhook/events`.

---

## 8. Oracles

Weak invariants first: no unexplained 5xx; reads stay readable after every mutation; created
resources continue to exist; terminal statuses do not revert; a repeated idempotency key creates one
resource.

Those stay *valid* under concurrency but stop discriminating money loss, so conservation is needed.
An earlier draft stated three identities and all three were wrong — the corrected forms, with why
the obvious version fails:

**Per account.** `balance == Σ amount` over `…/accounts/{id}/transactions`.

Not `Σ CREDIT − Σ DEBIT`: `amount` is **signed on the wire**. `DirectTransactionRepository` derives
`DEBIT` from `customer_amount < 0` and passes the value through unchanged, `DirectAccountMapper`
wraps it without `abs`, and `MoneyString` documents response amounts as signed. Subtracting debits
double-negates them. `mark` is a label on the sign, not an instruction to apply one. The type set to
expect is `DEPOSIT`, `WITHDRAWAL`, `INTEREST`, `ADJUSTMENT` and `MATURITY` (with `INCOME` mapped
onto `MATURITY`) — an earlier draft named only `INTEREST`.

**Per customer — do not use.** `Σ account balances + unallocatedCashBalance == totalBalance` is a
tautology: `total_balance` is *defined* as `total_savings_balance + unallocated_cash_balance` in
`R__0001004_direct_customer_holdings.sql`. And `Σ account.balance == totalSavingsBalance` reads
`cpa.product_account_balance` on both sides. It cannot detect money loss; at most it detects a
paging bug in the read path. Keep it only if that is what you want, and label it as such.

**Per batch.** `totalPaymentRequired == Σ amount of allocations that are neither CANCELLED nor
REJECTED` — including COMPLETED ones. It does **not** decrease to zero at SETTLED:
`DirectBatchInstructionRepository` computes it as
`SUM(amount) FILTER (WHERE status <> 'CANCELLED' AND type = 'DEPOSIT')`, so completed deposits stay
in the total. An earlier draft asserted both the decrement and an equality against "accepted"
allocations; the second breaks on the first cancellation.

**The platform pot is readable, so it is not a gap.**
`GET /operations/account/own/owner/{accountOwnerUid}/{connectorType}/{currencyCode}/{accountType}`
returns `InternalAccountDetailResponse{accountUid, balance}`, and for a platform the account-owner
uid is the entity uid. Two caveats: `balance()` is `.abs()`'d, so the sign is lost and the harness
must infer direction from the account type; and this is where held and cancelled-group money sits,
so a cohort-level identity needs it or conservation reports holds as leaks.

**The batch status derivation excludes held rows on purpose — seed it as an expectation, not a
finding.** A `HELD` share keeps its `PENDING` status on the wire, and the `pending_count` in
`DirectBatchPaginatedRepository`'s status `CASE` counts only `PENDING` rows with no unreleased row
in `direct_customer_instruction_hold`. So one completed share plus one held share reads `SETTLED`
while the held money is still in the platform pot, and the per-batch identity above fires on it.

That is intended behaviour, not a defect, and two integration tests pin it — the assertion in
`DirectBatchOrderHandlerIntTest` states the reason outright: without the hold exclusion "this batch
reads PARTIALLY_ACCEPTED and never settles in the partner's list view". So the harness must carry it
in the §3 seed table as a known expectation. Treating it as a discovery wastes the first run, and
the per-batch conservation identity has to subtract held allocations before comparing, or it reports
intended behaviour as money loss on every batch that has one.

One corner of the same `CASE` is **not** covered by either test and is still worth a verdict: when
every share is held, `pending_count` is zero, `completed_count` is zero, so neither the `SETTLED`
branch nor the `PENDING` branch matches and the batch reads `PARTIALLY_ACCEPTED` although nothing
was ever accepted past a hold. Confirm that one by hand before the first run.

**Do not use "another customer's actions never change this customer's state" as a metamorphic
check.** It is false by design here: shared `paymentReference`, bank-wide notice, global closure,
and `account_cancel_reduces_shared_batch.feature`. Scope it to different platforms or drop it. The
cancel-then-reissue check is NOTICE-only, since `CancelInstruction` is legal only there.

---

## 9. Environment preconditions

Each of these makes a run go green while doing nothing.

- **`SetClock` is a silent no-op outside acceptance.** `TimeProviderFactory` hands out the settable
  provider only when `b7h.env == acceptance`. Assert it round-trips before trusting it — though per
  §4 the harness should not need it.
- **Virtual account pool preloaded** — `realAccountType=DIRECT`, `connectorType=INVESTEC`,
  `taxWrapperType=DEFAULT`, via `PreloadedInternalAccountClient`. Claims match the exact four-tuple
  with no fallback and there is no on-demand minting, so an undersized pool stalls mid-run. Size it
  to the *total* across concurrent cohorts, not per cohort.
- **Every platform the harness uses needs a `custom_platform_config` row.** Without one,
  `POST /direct/v1/batches` fails with `500 "Invalid platform"` although the platform is present in
  `partner_platform` and resolves correctly for products, customers and account opening. The row is
  missing because the dev database dump and restore does not carry it, not because the platform was
  created wrongly, so insert it rather than recreating the platform:

  ```sql
  INSERT INTO custom_platform_config (platform_sid, whitelisted, match_by_reference)
  SELECT pp.sid, true, true
  FROM partner_platform pp
  LEFT JOIN custom_platform_config cpc ON cpc.platform_sid = pp.sid
  WHERE pp.transfer_type = 'POOLED' AND cpc.platform_sid IS NULL;
  ```

  Every other column has a default, so `platform_sid` and `whitelisted` are the only ones needed;
  `match_by_reference` is set here because §9 wants it anyway.

  Worth knowing why the error names the wrong thing, because it sends you to look at the platform.
  `DisabledOrderProcessingCache.platformHasDepositEmbargo` reads
  `fetchPartnerPlatform(platformUid).map(PartnerPlatformRecord::isDepositEmbargo).orElseThrow(...)`.
  The query uses only LEFT JOINs, so it does return the platform row, but `isDepositEmbargo()` is a
  boxed `Boolean` that is null with no configuration row, and `Optional.map` turns a null result into
  an empty `Optional`. `platformHasWithdrawalEmbargo` and `platformHasOrderMovementEnabled` share the
  shape. This is a diagnostic trap rather than a live defect, because deployed environments have the
  row.

- **The clearing database must actually hold its schema, and after a restore it may not.** Seen on
  2026-09-15: the `clearing` database had zero tables in `public`, so
  `POST /operations/hsbc/statement/transactions/process` returned a 500 and the clearing log showed
  `relation "internal_account" does not exist`. The core database restored fine, so a run gets all
  the way through customer creation, account opening and batch placement, and only fails when money
  has to move. Check `SELECT count(*) FROM information_schema.tables WHERE table_schema='public'`
  against clearing before a run rather than after.
  Clearing builds its schema through Flyway at startup, so a service that was already running when
  the database was replaced never migrates the new one. Restart the clearing service, or rebuild the
  stack, and check the startup log reports the migration.

  A migrated but empty clearing is still not enough, and this is the part that costs a run. Clearing
  holds each platform's own internal account, and core registers it when the platform is created. A
  platform that already existed before clearing lost its data is never registered again, so
  `account_owner` has no PLATFORM row for it and there is no pot for a deposit to land in. The whole
  sequence then passes with every call returning 200 — batch PENDING, credit accepted, drain and all
  three settle legs accepted — while `account_statement_line`, `partner_payment_due` and
  `partner_payment` all stay empty and the account sits at REQUESTED with `depositedAmount` `"0.00"`.
  So check clearing has a PLATFORM `account_owner` row for the cohort's platform, not merely that
  clearing answers.

- **HSB auto-processing on** for `INVESTEC`, or outbound payments stay PENDING forever.
- **`TZ=Europe/London`** on clearing and hot-sauce-bank, or the poll window misses freshly-booked
  debits.
- **Webhooks subscribed via ops-api**, all event types, per-platform loopback URL, version from the
  audience enum (§7).
- **`match_by_reference = true`** on each platform, or a large amount collision across cumulative
  runs sends the group matcher down the wrong path and hangs.
- **Per-cohort bank**, its `ACCRUALS_AND_REALISATIONS` schedule row `DISABLED` so only the harness
  advances its date.
- **`sim-automatic-withdrawal-funding` off**, or `AutomaticFundingScheduler` funds everyone's dues
  behind the harness's back.
- **One client id per platform, repainted, with fresh tokens minted after** (§5).

---

## 10. System patches that would help

All additive, none altering production behaviour. Detail and `file:line` in
`simulation-testing-investigations/2026-09-08-system-patches.md`.

**P1 — per-account payment settle.** The highest-payoff one, and the smallest, because the method
already exists. Note this **overrules the patches investigation**, which put "scoping payment
aggregation" under *not worth patching* — on the grounds that per-account locks make the global
sweep safe and cohorts calling it concurrently serialise. That reasoning is about correctness and it
is right. The reason to patch anyway is attribution (§5): a safe global sweep still produces diffs
in cohorts that did not ask for them. `PaymentDueAggregationService.aggregateAndSettleExternalPaymentDues()` is a
`forEach` over `aggregateAndSettleExternalPaymentDuesForAccount(AccountUid)`, which is already
`@Transaction` and already takes the account lock. Nothing exposes it — clearing's `PaymentController`
offers only the global sweep. Expose it on `PaymentOperations` and surface through ops-api. This is
what removes the worked example in §5.

The `AccountUid` question an earlier draft raised is closed, and the answer is better than expected:
direct deposit dues are raised on the **platform's** internal account (`PLATFORM_SAFEGUARD`), not on
anything derived from a `ProductAccountUid`. So P1's grain is the platform pot — exactly the cohort
grain — and it should take a `PlatformUid` and resolve the account internally. The pot's own
`AccountUid` is already readable via the ops account endpoint cited in §8. One note for the patch:
an exposed per-account call sits outside the scheduler's `@SchedulerLock`, which the blocking
account lock makes safe.

**P2 — scope the notice sweep by bank, with an explicit `asOf`.** ~25 lines over 5-6 files; the
repository already joins `partner_bank`, so it is one predicate plus threading two optional
parameters. Makes the per-bank time partition usable for NOTICE and removes the last reason to move
the clock. Also reduces a real hazard: two concurrent unscoped sweeps see the same rows and the
second hits `dci_pdt_unique`, rolling back its *entire* batch. Bank scoping stops cohorts colliding;
the harness must still serialise within a bank.

**P3 — scope the closure sweep by platform.** ~15 lines; `FROM_CLAUSE` already joins
`partner_platform`. Isolation of observation, not correctness — `closeAccount` already locks and
re-checks.

**P5 — per-bank business-date read (and set).** Small, and the design leans on it: without it the
harness cannot observe where a cohort sits in logical time and must assume a fresh bank per run.
`SetupOperations` already sits next to a `/time` endpoint and `BankDateProvider` already has
`getDate`/`setDate`; guard the setter with the existing `!env.equals("prod")` check. Setting without
running accruals leaves interest schedules behind, so it is for alignment, not for skipping days.

**P4 — per-platform outstanding-work read.** Counts of pending direct instructions, unprocessed
notices, CLOSING accounts and `AWAITING_RESPONSE` webhook events. Replaces settle-timeouts
core-side. The clearing half (unsettled payment dues) is a separate service and DB, so without it
"quiescent" still means "counts zero across two reads and statuses agree".

**Ruled out:** a per-tenant instant clock (545 `timeProvider` call sites plus 64 SQL-side
`now()`/`CURRENT_DATE` no Java override reaches — the bank business date already gives the partition
that matters); `batchId` as a typed UID (it is `string/uuid` over the wire either way); replay
patches (solved harness-side with run-id prefixes); any acceptance-only auth bypass to multiplex one
token across platforms.

---

## 11. Coverage, and choosing what to aim at

Endpoint coverage is meaningless here. Track abstract states reached, transitions reached,
(action × precondition-state) pairs keyed on product type and customer status, terminal statuses,
rejection reasons observed, unattributed-diff rate, and longest journey depth.

The metric that decides whether this pays for itself is **transitions no existing `.feature`
covers** — and building that index is the *first* task, not a later refinement. An earlier draft of
this document picked a headline target (two batches sharing a `paymentReference`, funded for one
batch's total) and asserted nothing tested it;
`dm_batch/batch_match_by_reference_set_settled_by_two_payments.feature` is that scenario verbatim,
among 16 `dm_batch` features. Index first, then choose.

One target that does survive, as a shape to validate the driver against: **a notice withdrawal
becoming due and paying out.** Create a customer with a NOTICE account, fund it by batch, credit the
platform, drain, place a withdrawal, advance the cohort's bank ~33 days by repeated accrual, process
due notices, then settle. It exercises the per-bank time partition, the notice processor and both
payment legs, and its cancel-branch sibling (cancel before the period elapses, expect no payout)
is the cheap contrast. Note the corpus includes a `SAFEGUARD_HUB` aggregation and a further
send/process leg after a pooled credit — an earlier draft omitted it.

If the driver cannot reach that unaided, the diagnosis is a mechanism, not a missing rule: the state
key is too coarse to tell the frontier apart, replay is not returning cheaply enough, quiescence is
returning empty diffs, or attribution is unsound.

---

## 12. Where this is going: unsupervised fault injection

The intended end state is fault injection and limit-pushing, running unsupervised. That is not V1,
but four things about it are cheap to build in now and expensive to retrofit, so they belong here.

**The levers already exist.** `toxiproxy` 2.9.0 is in `docker/docker-compose.yml` with its admin on
`:8474`, already an env var in `tools/performance-testing/envs/local.env`. Hot-sauce-bank exposes
`POST /hsb/v1/payments/status` (set a payment's outcome) and
`POST /hsb/credit-notifications/{uid}/resend` (duplicate a credit notification). The scenario
runner's `teardown` block exists specifically to roll back fault state so it does not leak into the
next run, and it runs after a phase raises or on abort — the right pattern to copy.

**Start with faults the domain already names.** Each of these has defined expected behaviour and
usually a feature file, which means a finding under it can be triaged:
`PAYOUT_FAILED` / `CASH_ACCOUNT_PAYOUT_FAILED`, `REJECTED_TRANSACTION`, a `PARTIALLY_ACCEPTED`
batch, a mismatched pooled credit held rather than allocated, a compliance freeze landing while a
deposit is in flight, an idempotency-key replay, a duplicated credit notification. Infrastructure
faults — toxiproxy cutting clearing off from HSB mid-settlement — are more powerful and nothing
defines correct behaviour under them, so they produce findings that cannot be triaged. Wrong order
for something unsupervised.

**Four design consequences for now:**

1. **Faults are part of the recorded state, or nothing self-classifies.** Unsupervised means nobody
   reads the stream, and under a deliberate fault every invariant in §8 will fire. A violation is
   only a finding if no fault was live in its window. So the fault schedule is recorded alongside
   the journey and joined to each violation. This is the same causality machinery as the
   unattributed-diff rule in §5 — one mechanism, two uses, which is the reason to build the fault
   dimension into the record now rather than bolt it on.
2. **The property under test becomes recovery, not correctness.** Under a fault the system is
   allowed to be wrong. The oracle is inject → hold → clear → converge → assert, and the invariants
   need an *eventually* form: after the fault clears, does money conservation hold again, do PENDING
   instructions resolve rather than stick, does a held deposit release. That is a different
   assertion shape from §8's and the fault lifecycle should be built around it.
3. **Tag every learned edge with the fault context it was learned under.** An earlier draft said to
   learn the relation clean and then freeze it, which contradicts a corpus that §6 and §11 both
   expect to keep growing — freeze it and clean learning stops too. Point 1 already solves this:
   if the fault schedule is part of the record, an edge carries whether any fault was live when it
   was observed, and §3's looser-than-seed check simply reads the fault-free subset. No freeze
   needed.
4. **The stop condition is the safety-critical part.** Unsupervised faults plus concurrency can wedge
   the shared stack, and the specific ways are already known: the preloaded internal-account pool
   has no on-demand minting, so a fault that leaks accounts exhausts it and every cohort stalls
   silently; a `dci_pdt_unique` collision rolls back an entire batch; a wedged job holds its
   `@SchedulerLock` for its `lockAtMostFor` window. The harness needs a health check that **halts**
   rather than retries, and teardown that runs on abort.

**On limits.** Concurrency degree is a dimension the harness controls, and the DB connection pools
bound it before anything else. It will meet clearing's ceiling first: core allows 20 connections, clearing 10. It is not a load
tool and should not become one —
`tools/performance-testing/k6` already holds RPS. The limits worth pushing here are state-space
limits: how many live entities, how deep a journey, how many concurrent cohorts before isolation
stops holding — which the unattributed-diff rate measures directly.

---

## 13. Open questions

- **Client-id supply** — repainting existing ids onto desired platforms in the local DB copy, and
  making the credentials available to the harness. Being handled outside this document. This is the
  one open item that blocks the concurrency requirement, because §5 shows a single shared client id
  can only ever point at one platform.
- **A fully-held batch reads `PARTIALLY_ACCEPTED`** (§8) — the one corner of the batch status `CASE`
  that no test covers. Confirm by hand before the first run. The rest of the hold exclusion is
  settled and belongs in the seed table.
- **`tools/performance-testing/docs/authoring/direct-vs-trust.md` needs correcting** — it says the direct client binds to the
  *first* platform; it binds to the last, and a cached token explains the appearance (§5).
