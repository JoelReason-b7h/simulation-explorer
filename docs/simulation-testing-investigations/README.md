# Simulation testing investigations

Raw outputs of background investigations behind
[`../simulation-testing-direct-model.md`](../simulation-testing-direct-model.md). Kept verbatim so
the evidence and the `file:line` references survive independently of whatever the design document
currently says — several findings here contradict the design document as it stood when they were
written, and the reconciliation is not yet done.

Three questions these files left open are now closed in the design document, by reading the code
rather than by running anything: `depositInfo` is populated for POOLED and the perimeter `@Schema`
description that denies it is wrong (§1); `b7h.async.scheduling.enabled` gates only the schedulers
that carry a `@SchedulerLock`, eight production schedulers carry none, and an ops-triggered job
ignores the flag and the lock both (§2); and
the batch status `CASE` excludes held rows deliberately, with two integration tests pinning it, so
it is a seed expectation rather than a defect (§8).

| File | What it is |
|---|---|
| `2026-09-08-design-critique.md` | Adversarial review of the design: wrong claims, designs that won't survive contact, gaps, and what was checked and found correct. Run with concurrency as a hard V1 requirement. |
| `2026-09-08-system-patches.md` | Ranked proposals for small changes to the system that would make the harness easier to build, plus what was investigated and ruled out. |

Both were produced by agents reading the tree on 2026-09-08. Treat line numbers as of that date
and re-verify before acting. Where the two disagree, the critique is the more sceptical read: the
patches report concludes `AdvanceDays` needs no `SetClock` because direct accrual self-advances the
bank date, while the critique adds that a `SetClock` would additionally fire every due batch job
and that `core-ro` holds its own clock the harness cannot set. Both point the same way — do not
call `SetClock`.
