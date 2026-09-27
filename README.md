A copy of `tools/simulation-explorer` from the exchange repo, kept here so the work is safe while it is not yet committed there. It expects to sit at `tools/simulation-explorer` inside an exchange checkout. To refresh it, run the staging script again and commit.

# simulation-explorer

A harness that drives the local exchange stack through the Direct API, ops-api and the bank
simulator, and checks what it sees against the product's own rules. It runs in cycles of about 15
minutes, one conductor run and three member runs per cycle. `FINDINGS.md` lists what it has found.

## Running it

Use `./harness` for everything. Do not run `scripts/launch_stack`, `local-down.sh` or
`fleet_loop.sh` by hand.

```bash
./harness start    # launch the stack, wait for every service, start the fleet loop
./harness status   # containers, the loop's processes, the last cycles
./harness pause    # stop the loop and leave the stack up; ./harness start resumes it
./harness stop     # stop the loop, take the stack down, release the stack lease
```

`./harness start`:

1. Checks the stack checkout (`stack_check.py checkout`). The checkout must be
   `exchange.worktrees/sim-main` with the local-auth patch applied (`local_auth_patch.py`). If it
   is not, the command stops.
2. Launches the stack from that checkout with `scripts/launch_stack`, unless it is already up.
3. Waits until every service is running and ops-api answers `/health` with 200
   (`stack_check.py up`). A service that stopped early is started again, up to three times. For
   example, the adapter can start before LocalStack has created its queues.
4. Starts `fleet_loop.sh` in the background at the next unused cycle number.

Each step checks its own result and stops with a message if it fails. `harness.log` holds the
output.

`./harness stop` checks that no harness process is left and that no container is still running.

## Settings

- `FLEET_PLATFORMS`: the number of runs per cycle (default 4).
- `SIM_LOCAL_AUTH=0`: use the dev Cognito pool, not the harness's own tokens.
- `SIM_LONG_LIVED_BANK=0`: stand up a fresh cohort each cycle, not the saved long-lived bank.
- `SIM_JOURNEY_SHARE`: the share of a run's time that journeys may use (default 0.3).

## Files a cycle writes

- `fleetN-pK.json` and `fleetN-pK.html`: the trials, violations and journeys of each run.
- `fleet.progress`: one line when a cycle starts and one when it ends.
- `archive/fleetN.tgz` and `archive/fleetN-db`: the logs of the cycle and a dump of the databases.
- `cleared-groups.jsonl`, `approved-duplicate-holds.jsonl` and `refused-group-decisions.jsonl`:
  every payment group the harness failed, approved or decided on behalf of an operator.
