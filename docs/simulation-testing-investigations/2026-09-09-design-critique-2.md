# Investigation: second adversarial pass over the simulator design

Verbatim output of a background review run on 2026-09-09 (Fable 5.1), over the design as rebuilt at
commit `2ed514a2`. Briefed to hunt for what had been **glossed over** — sections where the hard part
was named rather than solved — rather than for outright errors, since the first review's findings
had been folded in.

It found that all three conservation identities in the oracle section were defective, one of them
tautologically; that the causality tokens the attribution rule depends on mostly do not exist on the
wire; and that the design cites a patches report which argues against its own top-ranked patch.
Recorded unedited.

---

# Second review: `docs/simulation-testing-direct-model.md`

All paths below are under `/home/user/exchange/` unless prefixed otherwise. Ranked by damage within each group.

## Glossed over

**G1. §8's three conservation identities: all three are defective as written, and the oracle layer is the whole point.**
- (a) Per-account: `amount` is **signed** on the wire. `DirectTransactionRepository.java:9-13` derives `DEBIT` from `act.customer_amount < 0` and passes `customer_amount` straight through; `DirectAccountMapper.java:160` wraps with `MoneyString.of` (no `abs`), and `MoneyString.java` says outright "response amounts are signed". `balance == Σ CREDIT − Σ DEBIT` double-negates every debit. The correct identity is `balance == Σ amount`. The type enum also has `ADJUSTMENT` and `MATURITY` (with `INCOME` mapped onto `MATURITY`, same file :17-23); §8 names only `INTEREST`.
- (b) Per-customer: `total_balance` is defined as `total_savings_balance + unallocated_cash_balance` (`core/src/main/resources/db/migration/R__0001004_direct_customer_holdings.sql:94`), so that half is a tautology. The other half, `Σ account.balance == totalSavingsBalance`, reads the same column both ways (`cpa.product_account_balance`: `DirectSavingsAccountRepository.java:48` and the SQL at `:69`). This identity cannot detect money loss; it detects only a paging/consistency bug in the read path.
- (c) Per-batch: `totalPaymentRequired = SUM(dci.amount) FILTER (WHERE status <> 'CANCELLED' AND type = 'DEPOSIT')` (`core/direct/DirectBatchInstructionRepository.java:123`). It **includes COMPLETED**, so it does not "decrease to zero at SETTLED"; and `Σ accepted allocations == totalPaymentRequired` breaks on the first cancellation, since accepted excludes only REJECTED.
- (d) The "gap" is not a gap. `GET /operations/account/own/owner/{accountOwnerUid}/{connectorType}/{currencyCode}/{accountType}` (`ops-api-common/.../account/OpsPortalAccountOperations.java:37`, controller `/operations/account`) returns `InternalAccountDetailResponse{accountUid, balance}`, and `AccountOwnerUid == entityUid` for platforms (`EntityInternalAccountRepository.java:474`). The platform pot is readable over HTTP today — but `balance()` is `.abs()`'d (`InternalAccountDetailResponse`), so the sign is lost.

**G2. §5/§7 causality tokens — the reads the harness diffs mostly carry none.**
- The transactions read (`ExternalDirectSavingsAccountTransactionResponse.java`) has `transactionId, accountId, amount, mark, type, valueDate, productName` — no reference, no instructionId. The account read's `balance` has none. So the two diffs §8 depends on can never be attributed by token; only by entity id.
- Webhooks: `SAVINGS_TRANSACTION` does carry `reference` (= `coalesce(cpo.platform_order_ref, dcig.instruction_reference)`, `AccountEventQueryRepository.java:54-55`) and `instructionId`. `CUSTOMER_STATE_CHANGED`, `ACCOUNT_CLOSED`, `INTEREST_REALISED`, `PAYMENT_PENDING_ALLOCATION`, `NOMINATED_ACCOUNT_ADDED`, `CUSTOMER_DATA_CHANGED` carry entity ids only. `PAYOUT_FAILED.instructionId` is a `CashInstructionUid` (clearing), not the `DirectInstructionUid` the harness holds.
- `Idempotency-Key` is echoed only on the response to the request that carried it (`IdempotencyFilter.java:148-200`). It never appears in any later diff — useless as an attribution token, yet §5 lists it as one.
- Actions with no harness-minted token at all: `UpdateCustomer`, `AddNominatedAccount`, `CloseCustomer`, `CancelAccountOpening`, `CloseAccount`, `CancelInstruction`, `CancelBatchDeposits`, and all six world actions. §7's "poll until … its causality token has appeared" is undefined for 13 of 17 actions. What remains is platform-URL + entity-id attribution, which is sound only if one journey runs per platform at a time — a constraint the document never states, and §6's per-entity state key with §12's "how many concurrent cohorts" both assume otherwise.

**G3. §6's driver has no defined subject for "least-tried action", and the hold state it says to tolerate is invisible.**
- Constructibility is a property of identifiers *the journey holds*; visit counts are per *entity-state*; `PlaceBatchPayment` touches a batch, N customers and N accounts. Nothing says which entity-state the trial is recorded against or how a multi-entity diff is split. "Journey-level aggregates as separate counters" names this and stops.
- `HELD` maps to `PENDING` externally (`ExternalDirectCustomerInstructionStatus.fromInternal`), so the `DEPOSIT_POOLED` hold §6 lists is not observable on any instruction read. Its only wire trace is via `pending_count`, which **excludes held rows** (`DirectBatchInstructionRepository.java:110-116`), feeding `deriveStatus` (`DirectBatchResponseFactory.java:10-24`): a fully-held batch reads `PARTIALLY_ACCEPTED`; one completed + one held reads **`SETTLED`** while money sits in the pot. §8's batch identity will fire on that. It may be a genuine system finding — but the harness has no classification for it.

**G4. §4's per-bank time partition is unobservable.** There is no HTTP read of a bank's business date — the only `businessDate` in ops-api-common is a path *input* (`OpsPortalProcessorOperations.java:110-114`); `OpsPortalSetupOperations` has only setClock. The date starts at global today at approval (`PartnerBankDecisionService.java:122`). The patches doc proposed a get/set (its item 3); §10 dropped it. Consequences: the harness must assume a fresh bank per run or count blind; P2's `asOf` must be defaulted server-side because the harness cannot supply it; and §11's "advance ~33 days" only works because notice due dates are stamped global-today + period (`NoticeProductService.java:72,82` via `DirectNoticeWithdrawalOperations.java:57`) and a fresh bank starts at global today — a precondition §4 never states.

**G5. §12's "learn clean, freeze, inject" contradicts the corpus that §6 and §11 say grows continuously.** Either the relation freezes (and §3's looser-than-seed check stops learning new clean edges) or faults contaminate it. Point 1 (record the fault schedule, join to each violation) already solves this without a freeze; presenting both as required is the seam. Point 2's "eventually" oracle also needs per-cohort quiescence on clearing, which §10 P4 admits does not exist.

**G6. §3's bindings and "spends" bit.** `CancelBatchRequest.instructionIds` takes instruction *references* ("Specific deposit instruction references to cancel", `ExternalDirectCancelBatchRequest.java`; `RequestedCancellation.instructionReferences`) — name-matching binds it to `instructionId` (a UUID) or nothing. A partial cancel does not spend the batch (status recomputed, `DirectBatchDepositCancellationService.java:63-136`), and `CancelAccountOpening` (→ `CANCELLED`) does spend an account but is missing from the list.

## Contradictions

**C1. §10 P1 vs its own cited evidence.** §10 says "Detail and `file:line` in `2026-09-08-system-patches.md`". That document lists "Scoping payment aggregation" under **"Investigated and concluded not worth patching"** — "Leave it; run one shared pump or just wait 60 s." §5 then makes P1 "the answer" to its worked example. The design may legitimately overrule the investigation, but it must say so; as written it cites evidence that argues the opposite.

**C2. §1's corpus numbers disagree with each other.** "38 INDIVIDUAL against 29 POOLED" then "29 of 30 POOLED". Actual platform rows in `tools/test-e2e/src/test/resources/features/order/direct/**`: **37 INDIVIDUAL (all UNVERIFIED), 30 POOLED (29 VERIFIED, 1 UNVERIFIED)**.

**C3. §4 says "P2 closes the notice half" and §5's table says the sweep becomes "per bank, explicit asOf" — but §4 also says due dates are stamped from the global clock at placement.** P2 as described only changes the comparison side. It works only when bank date ≥ global date + notice period (G4). The claim "removes the last reason to move the clock" is true only under that unstated precondition.

## Wrong

**W1. Ops webhook paths.** The controller is `@Controller("/webhook")` (`ops-api/.../webhook/OpsPortalPlatformWebhookController.java:28`; client `path = "/webhook"`), so it is `GET /webhook/events` and `POST /webhook/link`, not `/operations/webhook/...` as in §7 and §10 P4. Every other ops controller carries `/operations/`; this one does not.

**W2. §10/§13 frame the `AccountUid` question wrongly.** Direct deposit dues are raised on the **platform's** internal account (`PaymentDueDirectCustomerAccountService.java:96-97, 109`, `PLATFORM_SAFEGUARD`), not on anything mapped from a `ProductAccountUid`. The grain of P1 is the platform pot — which is exactly the cohort grain, good news — and its `AccountUid` is readable today via the endpoint in G1(d). P1 should take `PlatformUid` and resolve internally; the open question is closed.

**W3. §12 "~20 DB connections per service"** — core 20, clearing **10** (`clearing/src/main/resources/application.yml:107`).

**W4. Small slips.** `customerId` has 20 path-param uses, not 19 (`documentId`/`productId` 2 each). `direct-vs-trust.md` lives at `tools/performance-testing/docs/authoring/`, not `docs/authoring/`. §6's `instruction.status: REJECTED` is only ever produced inside a batch allocation result (mapper maps null status → REJECTED; the internal enum has no REJECTED) — never on `GET /instructions`. `InstructionType.RETURN` (refund of a rejected inbound deposit, `DirectDepositRefundService.java:19-21`) is missing from §1's tolerate-list; it is INDIVIDUAL-rail only (`DirectCustomerAccountHandler` → `DirectTransactionDepositHandler`; the pooled handler has no refund path), so adding the INDIVIDUAL axis §1 calls "not blocked" brings it in.

## P1–P4 against the code

- **P1** — `aggregateAndSettleExternalPaymentDuesForAccount(AccountUid)` exists exactly as claimed: package-private, `@Transaction`, blocking `acquireLock` (`clearing/.../PaymentDueAggregationService.java:91-97`); the public sweep is a `forEach` over it (`:67-69`); `PaymentController` exposes only the sweep. Correct — but see C1 and W2. One note: an exposed call sits outside `@SchedulerLock("RequestedPaymentBatch")` (`PaymentDueScheduler.java:24`); the blocking account lock makes that safe, and §10 should say so as it does for P2.
- **P2** — repository joins `partner_bank pb` (`DirectCustomerAccountNoticeRepository.java:55`), predicate is `due_date <= ?:dueDate` (`:63`); placement uses the global clock. As described, with G4/C3 caveats.
- **P3** — `FROM_CLAUSE` joins `partner_platform ppl` (`DirectCustomerAccountRepository.java:70`). As described.
- **P4** — `AWAITING_RESPONSE` exists; the query bean has `platformUid` and `eventState`. As described except the path (W1). The dropped business-date get/set is the patch the design actually leans on (G4).

## Fine, actually

Spec counts 31/22/115 exact; all six enum lists exact; `PlaceInstruction` request type is `WITHDRAW | TRANSFER`. toxiproxy 2.9.0 on `:8474`, `POST /hsb/v1/payments/status`, `POST /hsb/credit-notifications/{uid}/resend`, and `scenario.py`'s teardown semantics (`:473-478`) are all exact. `TZ=Europe/London` set on the compose services; `sim-automatic-withdrawal-funding` flag exact; idempotency enabled in acceptance and the header genuinely echoed. Ops webhook link takes `platformId` explicitly, `@NotBlank url`, and an internal `WebhookVersion` — the version trap is real. `/simulator/direct/payment/platform/transactions`, `/operations/hsbc/statement/transactions/process`, `/operations/processor/{payments/outgoing,payments/transfers,payment/groups/process,direct/notice,direct/account-closure}`, and `/operations/batch/processor/bank/{bankUid}/{type}/{sync,status}` are all exact. `sync` invokes the job directly (`ScheduledEventController.java:79-85`) and never consults job status, so `DISABLED` + `sync` works as §9 assumes. Self-advance is exactly accrue → `setNextBusinessDate` → realise (`DirectModelInterestProcessing.java:58-66`). Reference matching is scoped to `ia.account_uid` as claimed. 16 `dm_batch` features; `CancelInstruction` rule as stated. §2's "eleven actions" is right (`SetMaturityDestination`/`RemoveMaturityDestination` are TERM-only and correctly excluded, though unmentioned).
