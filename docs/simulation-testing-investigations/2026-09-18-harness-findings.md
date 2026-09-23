# Findings from the exploration harness, 18 September 2026

Nine cycles, each one a wiped stack, a freshly stood-up POOLED Direct cohort and a timed run of the
explorer. Cycles 1 to 3 ran for five minutes, 4 to 7 for ten, cycle 8 for fifteen and cycle 9 for
twenty. Every finding below comes with the sequence that produced it and the log or database
evidence behind it.

The last three runs report the same two things and nothing else:

| cycle | seconds | trials | states | findings |
| --- | --- | --- | --- | --- |
| 7 | 600 | 777 | 40 | `OpenAccount` 500 ×87, one race finding |
| 8 | 900 | 1139 | 51 | `OpenAccount` 500 ×157, one race finding |
| 9 | 1200 | 1354 | 47 | `OpenAccount` 500 ×202, one race finding |

The harness reports a finding only from evidence. A race that ends with two accepted calls is not
reported, because an ordering usually exists in which both are correct; what is reported is a call
that failed rather than refused, a call that was accepted and moved nothing, and a conservation
identity that still disagrees on a second reading.

## 1. `OpenAccount` answers 500 after the customer's previous account is closed

**Sequence**, the same in cycles 3, 4, 5 and 7, and the trial numbers below are cycle 7's:

| trial | call | result |
| --- | --- | --- |
| 11 | `POST /direct/v1/customers/{id}/accounts/{accountId}/close?reason=NO_LONGER_NEEDED` | `200`, `status` changed |
| 12 | `POST /direct/v1/customers/{id}/accounts` | **`500`** `An unexpected error occurred` |
| 13 | the same call again | **`500`** |

At trial 11 the account was `OPEN`, carried a positive balance and had no instruction in flight.
Cycle 7 hit this 78 times on its own and 9 more inside a race, out of 777 trials; it is the only
finding that run produced.

The same close is refused cleanly when an instruction is in flight
(`Cannot close account … while a deposit is in flight`), and the same reopen is refused cleanly
while the first account is still `OPEN` (`An open account already exists for platform product …`).
So both guards work; the 500 sits in the window the close opens.

**What core logs at that moment:**

```
ERROR org.jdbi.sql - Exception while executing 'INSERT INTO customer_product_account (
org.postgresql.util.PSQLException: ERROR: Duplicate CPA for non-term product:
  customer_account_sid=1 platform_product_sid=1
ERROR b.l.m.e.handler.SqlExceptionHandler - Unknown SQL error handled
```

The database function `check_non_term_cpa_uniqueness` (migration
`R__0005002_check_non_term_cpa_uniqueness_fn.sql`) counts the customer's rows for the product and
excludes only two statuses:

```sql
and (dca.sid is null or dca.status not in ('CANCELLED', 'CLOSED'))
```

`direct_customer_account.status` also carries `CLOSING`, and an account in that status still counts,
so the insert raises and the tenant gets a 500. `DirectCustomerAccountStatus` holds exactly five
values — `REQUESTED`, `OPEN`, `CLOSING`, `CLOSED`, `CANCELLED` — so `CLOSING` is the only one the
trigger counts and the application does not reject.

`DirectCustomerAccountOrchestrator.validateAccountOpeningRequest` checks the same rule before the
insert, and it blocks two statuses only:

```java
if (accounts.stream().anyMatch(a -> a.status() == DirectCustomerAccountStatus.REQUESTED)) {
  throw new RequestValidationException("An underfunded account already exists for platform product " + ...);
}
if (accounts.stream().anyMatch(a -> a.status() == DirectCustomerAccountStatus.OPEN && a.productType() != ProductType.TERM)) {
  throw new RequestValidationException("An open account already exists for platform product " + ...);
}
```

`CLOSING` is neither `REQUESTED` nor `OPEN`, so validation passes the request through to the insert,
and the trigger raises there. The trigger is the strict one and the application check is the
permissive one, and the gap between the two is one status: `CLOSING`. The whole enum accounts for
itself — the application rejects `REQUESTED` and `OPEN` with a 400, the trigger excludes `CLOSED`
and `CANCELLED` so the insert succeeds, and `CLOSING` alone reaches the insert and raises.

**Confirmed.** The account read taken straight after the close reports `CLOSING`, and `CLOSING` is
one of the two statuses the trigger does not exclude. Cycle 8 caught the same shape three separate
times, and every close that landed on `CLOSING` was followed by a 500:

```
 11 CloseAccount 200 landed=CLOSING → 12 OpenAccount 500 → 13 OpenAccount 500
266 CloseAccount 200 landed=CLOSING → 267 OpenAccount 500
290 CloseAccount 200 landed=CLOSING → 291 OpenAccount 500 → 292 OpenAccount 500
```

A close that landed on `CLOSED` was never followed by a 500, which is what the trigger's exclusion
list predicts. The account reaches `CLOSED` once `POST /operations/processor/direct/account-closure`
has run, so the window lasts from the close until that sweep.

**A TERM product is exempt from this trigger entirely.** `check_non_term_cpa_uniqueness` filters
on `bp.product_type != 'TERM'`, so one customer may hold any number of simultaneous TERM accounts
on one product, and `OpenAccount` never refuses on a TERM customer. A run that did not cap it
ended with 67 open TERM accounts on a handful of customers. That is the migration's stated intent
rather than a defect, and it is worth knowing beside this finding: the 500 below is reachable only
on INSTANT and NOTICE products.

**The fix depends on whether a `CLOSING` account should block a new one, which is a product
decision the runs cannot answer.** Both readings are available:

- A `CLOSING` account still holds the customer's money and still accrues interest until
  crystallisation, so it is arguably still a live position. Then the trigger is right, and
  `validateAccountOpeningRequest` must reject `CLOSING` as well, which turns the 500 into the 400 it
  should have been.
- A `CLOSING` account is one the customer has already closed and cannot deposit into. Then the
  trigger must exclude it:

  ```sql
  and (dca.sid is null or dca.status not in ('CANCELLED', 'CLOSED', 'CLOSING'))
  ```

The 500 is a defect under either reading, because a tenant making a legitimate call gets an
unexplained internal server error rather than an answer. Whichever list is right, write it once and
read it from both places, because the two statements of this rule have drifted apart.

**How long a tenant is blocked.** `DirectAccountClosureService` runs on
`@Scheduled(cron = "0 9-17 * * MON-FRI", zoneId = "Europe/London")`, so nothing moves an account out
of `CLOSING` outside 09:00 to 17:00 on a weekday, and a close at 17:05 on a Friday answers 500 to
every `OpenAccount` on that product until 09:00 on Monday at the earliest.

The sweep also finalises only the accounts that `fetchClosingAccountsReadyForFinalisation` selects,
which is stricter than "the next hour":

```sql
WHERE dca.status = 'CLOSING'
  AND (
    (bp.product_type = 'INSTANT'
      AND ips.accrual_next_value_date IS NULL
      AND ips.realised_next_value_date IS NULL)
    OR
    (bp.product_type = 'NOTICE' AND cpa.product_account_balance = 0)
  )
```

An INSTANT account waits for its interest processing schedule to drain, which
`bringForwardFinalRealisation` sets up at the close request and which runs on the business date
cycle, so the account can stay `CLOSING` across an interest run. A NOTICE account waits for its
balance to reach zero, so its block lasts until the notice withdrawal has paid out. The trigger
counts every one of the customer's rows for that product, so an account closed days earlier and
still waiting on either gate blocks a new one today.

**Where the closure payments sit relative to the block.** `requestAccountClosure` calls
`orchestrateInternalAccountClosure` before it writes `CLOSING`, so clearing is already draining the
internal account towards the customer's nominated account while the 500s are being returned. At
finalisation, `closeAccount` writes `CLOSED` first and creates the residual withdrawal afterwards,
so a new account is permitted while the old account's closure payment is still unsettled. The
design therefore already accepts a new account existing alongside money still leaving the old one,
and draws its line at the `CLOSED` status rather than at the payment landing.

## 2. Reading one TERM account costs seconds

Measured against the same running stack, one read each, repeated to rule out a cold cache:

| read | time | response |
| --- | --- | --- |
| `GET /direct/v1/customers/{id}/accounts/{id}` on an INSTANT account | 0.19s | 1091 bytes |
| the same call on a TERM account | 2.72s, then 1.70s | 1100 bytes |
| `GET /direct/v1/customers/{id}` | 0.02s | 1042 bytes |
| the account's transactions | 0.04s | 842 bytes |

The two account reads return the same amount of data, so the cost is not in what comes back. Core's
log shows why: reading a TERM account builds a day-by-day interest projection running to maturity,
and the run that first hit this had a product whose term was thirty years, which produced a
projection to 2049 and dropped the harness to one trial a minute.

The product behind the measurement above has a twelve-month term, so the projection is 365 entries
and the read still costs seconds. A five-year term multiplies it.

The harness now reports this by itself: `oracles.answers_promptly` raises a finding for any call
over a second, so a read that becomes slow is caught without anybody timing it by hand.

## 3. `SetMaturityDestination` throws a NullPointerException on an unfunded TERM account

Reachable only once the cohort holds a TERM product, which it now does. The account was `REQUESTED`
rather than `OPEN`, so no deposit had completed and the account carries no maturity date yet.

`POST /direct/v1/customers/{id}/accounts/{id}/maturityDestination` answers **500**, and core logs:

```
ERROR b.l.m.e.h.GlobalExceptionHandler - Fall through error handled:
  Cannot invoke "java.time.LocalDate.isAfter(java.time.chrono.ChronoLocalDate)"
  because the return value of
  "b7h.exchange.core.direct.DirectCustomerAccountRepository$DirectCustomerAccountRecord.maturityDate()"
  is null
```

The caller asked for something the account is not ready for, which is a 400 with a message. A null
maturity date on a TERM account that has taken no deposit is an ordinary state, not an impossible
one, so the read of it has to allow for null.

## 4. Reading a Direct account answers 500 when the destination lookup matches two accounts

Cycle 30, trial 151. `POST /direct/v1/customers/{id}/accounts/{id}/maturityDestination` answered
**500**, and core logged:

```
ERROR b.l.m.e.h.IllegalStateExceptionHandler - Illegal state error handled:
  Expected zero to one elements, but found multiple
java.lang.IllegalStateException: Expected zero to one elements, but found multiple
	at org.jdbi.v3.core.result.ResultIterable.findOne(ResultIterable.java:201)
	at b7h.libs.persistence.jdbi.JdbiExecutionService.executeOptionalQuery
	at b7h.exchange.core.direct.DirectCustomerAccountRepositoryImpl.getDirectCustomerAccount:141
```

`CustomerProductAccountService.setMaturityDestination` makes two lookups, and the second resolves
the destination by product rather than by account:

```java
var destinationAccount = directCustomerAccountRepository
  .getDirectCustomerAccount(productAccountUid, destinationPlatformProductUid)
```

```sql
WHERE ca.uid = (SELECT ca2.uid FROM customer_product_account cpa2 ... WHERE cpa2.uid = ?:originProductAccountUid)
  AND pp.uid = ?:destinationPlatformProductUid
  AND dca.status NOT IN ('CANCELLED', 'CLOSED')
```

The repository states the assumption behind that predicate:

```java
// A customer/product pairing can now have more than one historical DCA (a terminal CANCELLED/CLOSED
// one plus the current one) — exclude terminal statuses so this stays a single-row lookup.
```

It holds for INSTANT and NOTICE, because `check_non_term_cpa_uniqueness` permits one live account
per customer per product. It fails for TERM, because that trigger filters on
`bp.product_type != 'TERM'` and permits any number, and one run ended with 67 live TERM accounts
across a handful of customers. `findOne` then throws and the tenant gets a 500.

No join in `FROM_CLAUSE` can fan out, which rules out the other explanation: every join there is on
a primary key or a unique column, including
`direct_customer_account_customer_product_account_sid_key`,
`platform_product_configuration_platform_product_sid_key` and
`customer_product_account_matur_customer_product_account_sid_key`.

The same TERM exemption makes finding 1 reachable, so the two share a root.

**The fix.** The endpoint takes a destination platform product, and
`validateDestinationIsEasyAccessOrNotice` rejects a TERM destination one line after the lookup, so
the lookup should not match a TERM account either:

```sql
  AND dca.status NOT IN ('CANCELLED', 'CLOSED')
  AND bp.product_type <> 'TERM'
```

The tenant then gets the existing "Unable to find direct customer account for product …" as a 400.

`getDirectCustomerAccount(CustomerAccountUid, PlatformProductUid)` carries the same predicate and the
same assumption, so it is exposed the same way wherever a TERM product reaches it.

## 5. An illegal customer status transition answers 500 rather than 400

`POST /simulator/direct/customer/{id}/kyc/check` with `{"customerStatus": "PENDING"}` on a customer
already at `DEACTIVATED`:

```json
{"status": "INTERNAL_SERVER_ERROR",
 "message": "Illegal customer verification status transition: DEACTIVATED -> PENDING"}
```

The service knows exactly what is wrong and says so in the message, so the caller asked for
something the domain forbids, which is a 400. Nine times in cycle 4, twice more under a race.

The route in is the simulator, which is a non-prod endpoint. The transition guard itself sits in
core, so the same rule is reachable from the ops API, and the status code is decided in the same
place either way.

## 6. A CLOSED customer became ACTIVATED again

Cycle 4, trial 514. The before-read and the after-read carry the same `customerId`
(`a91303bc-49b6-4c44-8ffd-af3ed7bd1ccb`), so this is one customer, not two:

```
rule    a terminal status is not left
detail  customer moved from CLOSED to ACTIVATED
action  AddNominatedAccount
```

This is the race SAV-11534 records, and `AddNominatedAccount` is not a second route into it.
`CustomerNominatedAccountAssignmentService.persistAndPublishNominatedAccountChange` stores the
account, sends the `NOMINATED_ACCOUNT_CHANGED` webhook, and stops if the customer is not
`ACTIVATED`. No KYC check starts there, and Confirmation of Payee verifies the account rather than
the customer, so nothing on that path writes `platform_customer.verification_status`.

The harness names the action it was running when a rule broke, and it drives KYC through the
simulator, so a compliance result from an earlier trial can land in any later window. Naming
`AddNominatedAccount` records what was running, not what caused the change. The harness needs to
record what else is in flight, or say that it cannot attribute the change.

## 7. A close that was accepted moved nothing

Cycle 4, one race:

```
CloseCustomer was accepted and the customer is still PENDING, unchanged from before the race,
so the call that ends it moved nothing
```

The status before the race and the status after it are the same value, so the accepted call left no
trace. A slow close would show an intermediate status; standing completely still would not.

## 8. `NominatedAccountVerificationDispatcher` reports a state it is not in

Core logs, during cycle 3:

```
ERROR b.e.c.a.v.NominatedAccountVerificationDispatcher - CoP dispatch failed for customer … ;
  the account stays UNVERIFIED and can be retried via the ops re-check
java.lang.IllegalStateException: CoP outcome cannot be applied to nominated account in state
  VERIFIED (legal start states: UNVERIFIED, AWAITING_REVIEW)
```

The message tells an operator the account stays `UNVERIFIED` while the exception it is reporting
says the account is `VERIFIED`. An operator following the advice would re-check an account that
needs nothing.

## What the harness injects, and what survived it

Three boundaries carry a toxiproxy listener, created by `scripts/launch_stack` before any service
dials through them, so with no toxics each is a plain pass-through:

| boundary | port | what crosses it |
| --- | --- | --- |
| `core-to-clearing` | 20001 | every payment due and settlement |
| `clearing-to-bank` | 20002 | every statement poll and payment submission |
| `harness-bank` | 20000 | the run's own credit into the bank |

On top of latency and cut connections on each of those, the run stops and starts core, clearing and
the bank outright, and it replays its last change verbatim, which is what a client retry after an
unreadable answer looks like from the service's side.

`FundAccountInterrupted` is the one that reaches states nothing else does, because a fault injected
between two whole actions never lands inside a sequence. It funds an account and breaks one step,
rotating through four:

- the credit into the bank, so the money never left;
- clearing reading the statement, so the credit landed and was never attributed;
- core raising the payment due, so the credit was attributed and never moved on;
- clearing dying outright between the credit and the settlement, which takes its in-memory state
  with it rather than leaving it to finish once a wire returns.

### Duplicate message delivery, and the two ways of doing it that did not work

Clearing consumes `eventbridge-iar-clearing`, `eventbridge-icac-clearing` and
`eventbridge-pe-clearing`. Delivering one of those messages twice is the at-least-once case every
consumer is written for, and two obvious ways of producing it both failed silently:

- **Setting the queue's `VisibilityTimeout` to zero changes nothing.** The consumer deletes a
  message within milliseconds of taking it, so no second poll ever sees it. Every repeat observed
  that way turned out to be the ordinary thirty-second retry after a failed insert, which is the
  default timeout and not an injected fault at all.
- **Copying a waiting message while clearing is up finds nothing.** The queues drain faster than
  the run can look, so `messagesCopied` stayed at zero across a whole run.

What works is stopping clearing first. The messages pile up behind it, the run reads one with a
zero visibility timeout so the original stays in place for its real consumer, sends the body back
as a second message, and starts clearing again. Both copies are then waiting.

**The evidence that it fired**, from clearing's own log — two receipts of one event id 252
milliseconds apart, far too close to be the thirty-second retry:

```
03:20:05.864  Received ... with id 2e822fa0-a8fc-46d5-8119-abd13bc73d47
03:20:06.116  Received ... with id 2e822fa0-a8fc-46d5-8119-abd13bc73d47
03:16:30.695  Received ... with id 2f8d5abf-d01a-4ae8-9c64-c72cac1b28ee
03:16:30.965  Received ... with id 2f8d5abf-d01a-4ae8-9c64-c72cac1b28ee
```

`PaymentExpectation` was among the events duplicated, which is the path the money takes.

**The duplicate was handled correctly.** Of 26 copies of one statement line, one is `PUBLISHED`
and 25 carry `duplicate_of`, and no `funding_record` uid appears twice. Nothing was counted twice.

**Nothing broke.** Across cycles 25 to 35, with 42 business days advanced and every interruption
kind exercised, no conservation oracle fired: no balance disagreed with its transactions, no batch
total disagreed with its lines, and no fully paid batch failed to settle. Every finding those runs
produced was a 5xx while a fault was live, which is the fault working.

Two rules keep that honest. A 5xx while a fault is injected is reported under
`a fault is survived without a server error`, apart from findings on a healthy stack, because
without the split one injected fault turned all 24 rules into findings at once. And faults are
budgeted to one trial in eight, because fourteen of the roughly thirty actions break the machinery
and picking by least-tried alone cost 160 trials in ten minutes where the budget gives 545.

## Two environment faults the harness had to fix before any of this was reachable

**The simulator container has no service URLs.** `docker/docker-compose.yml` gave `simulator-api`
only `B7H_SERVICE_WIREMOCK_URL`, so every other client fell back to the `localhost` defaults in its
`application.yml`, which inside the container point at itself. Every simulator call that reaches
core answered `500` with `Connection refused: localhost:4000`. Adding `B7H_SERVICE_CORE_URL`,
`B7H_SERVICE_CORE-RO_URL`, `B7H_SERVICE_CLEARING_URL` and `B7H_SERVICE_HOT-SAUCE-BANK_URL` to that
service makes the whole simulator API usable locally.

**The preloaded account range and the bank agree, and an earlier hand edit broke them.** Clearing
seeds the INVESTEC agency range on sort code `405656`
(`V20260630103239__sav_10657_seed_investec_agency_account_range.sql`) and the hot-sauce bank reads
`hsbc.api.profile.investec.agency-sort-code`, which defaults to the same `405656`. A local edit
moving the clearing range to `180422` to match the bank's own Investec IBAN breaks agency matching
in `AccountService.getMatchingAgencyRealAccount`, so every funding payment books against the master
account and the statement line ends at `EXCEPTION`. A wiped stack needs no edit at all.
