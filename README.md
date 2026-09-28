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
./harness start 10 # the same on a fake clock running ten times real time
./harness start 10 --at "2026-12-31 18:00"   # ...starting at that London time
./harness up [10]  # launch the stack and wait for every service; no fleet loop
./harness status   # containers, the loop's processes, the last cycles, the fake time
./harness pause    # stop the loop and leave the stack up; ./harness start resumes it
./harness stop     # stop the loop, take the stack down, release the stack lease
```

The rate is a plain number. With no rate the stack runs on the real clock, as it always has. The
start time can also be the second argument: `./harness up 10 "2026-12-31 18:00"`.

`./harness start`:

1. Checks the stack checkout (`stack_check.py checkout`). The checkout must be
   `exchange.worktrees/sim-main` with the local-auth patch applied (`local_auth_patch.py`). If it
   is not, the command stops.
2. Unless the stack is already up, applies `stack_patch.py` to that checkout, writes the fake
   clock's timeline when `SIM_CLOCK` is set, and launches the stack with `scripts/launch_stack`.
3. Waits until every service is running and ops-api answers `/health` with 200
   (`stack_check.py up`). A service that stopped early is started again, up to three times. For
   example, the adapter can start before LocalStack has created its queues.
4. Starts `fleet_loop.sh` in the background at the next unused cycle number.

`./harness up` runs steps 1 to 3 only.

Each step checks its own result and stops with a message if it fails. `harness.log` holds the
output.

`./harness stop` checks that no harness process is left and that no container is still running,
then removes the fake clock's timeline.

## Settings

- `FLEET_PLATFORMS`: the number of runs per cycle (default 4).
- `SIM_LOCAL_AUTH=0`: use the dev Cognito pool, not the harness's own tokens.
- `SIM_LONG_LIVED_BANK=0`: stand up a fresh cohort each cycle, not the saved long-lived bank.
- `SIM_JOURNEY_SHARE`: the share of a run's time that journeys may use (default 0.3).
- `SIM_STACK_REPO`: the sim-main checkout (default the Mac path in `exchange.worktrees`).
- `SIM_AUTH_DIR`: the folder that holds the ops credentials (`local.env`).
- `SIM_CLOCK`: the fake clock as libfaketime writes it, for example
  `SIM_CLOCK="@2026-10-25 00:00:00 x10"` (UTC). `./harness start 10` sets it; set it by hand only
  to replay an exact start from a cycle's results.

## The fake clock

One rate for the life of a stack. A rate change does not re-time the waits already running in
the services, so `./harness start 20` on a stack that is up at x10, or on the real clock, stops
with a message: `./harness stop`, then start again. A stack that is up keeps its clock, so
`./harness start` with no rate resumes the loop on it.

### Where a run starts

With a rate and no start time, `faketime/boundaries.py` picks a boundary from this catalogue at
random, within the next two years, and starts between 30 minutes and 6 hours before it (London
time unless marked):

- the clocks changing, GMT to BST and BST to GMT (the last Sunday of March and of October, 01:00
  UTC);
- London midnight during BST, which is 23:00 UTC;
- month end: midnight after the last calendar day, and after the last working day when that is
  earlier;
- quarter end and year end, into 1 April, 1 July, 1 October and 1 January;
- Friday 17:00, into the weekend;
- Christmas Day, Boxing Day, Good Friday and Easter Monday;
- 29 February 2028 and the 1 March after it;
- just before a cron the services run daily: 00:00, 01:00, 02:00, 02:30, 03:00, 17:45 (the
  platform deposit due), 18:00, 20:00 (the harness's accrual schedule) and 23:00.

The rate, the fake start and the boundary go into the timeline, into every run's JSON and the
cycle summary as `clock`, and into the digest `publish_results.py` pushes, so a finding can be
replayed with `SIM_CLOCK` from the same moment.

### What the harness does differently

The services' own schedulers do the scheduled work, so the harness's stand-ins for those crons do
nothing and answer with the scheduler that does it: payment sending, internal transfers, payment
files and status enquiries, statement polling and processing, platform payment dues, accruals and
realisations, the Direct feed, RECON and MI, the notice processor, the closure sweep, fee
withdrawals, the nominated-account outbox, and topping up the preloaded account pool during a
run. The standup still seeds the pool and the partner file, because a deployed pool is never
empty. The driver no longer picks AdvanceBusinessDay, ProcessClosures, ProcessDueNotice or RunDataFeed, and
the long-lived bank's clock keeper stays off: only the bank's accrual job, at 20:00 London, moves
the business date. The operator actions (failing nameless payment groups, approving duplicate
holds, deciding refused groups) and moving a notice due date still run, because they stand in for
people and for elapsed calendar time, not for crons.

Everything the harness sends to the system or compares with the system's timestamps reads
`explorer/clock.py`: token `iat` and `exp`, product and rate dates, the statement window, and the
"older than" windows over the databases. A window that allows the services time to finish work
in the background is real time, so it grows by the rate on the fake clock. The harness's own
timings, waits and HTTP timeouts stay on the real clock.

### The stack under the fake clock

`stack_patch.py` adds `docker/docker-compose.faketime.yml` to
`scripts/launch_stack` and `local-up.sh`, and copies the libraries and `faketime/clock.py` into the
checkout's `docker/faketime/`. The overlay:

- preloads libfaketime into postgres, LocalStack, redis and every Java service. The libraries in
  `faketime/<base>-<arch>/` are the distributions' own packages, copied by `faketime/build.sh`:
  `trixie` for the Debian 13 images and `alpine` for redis;
- adds a `faketime-clock` container that keeps the current fake time in a shared volume. Each
  process reads it once, when it starts, and then runs at the rate from there, so every container
  shows the same time and a restarted one carries on rather than starting over
  (`faketime/clock.py` explains why);
- turns on the schedulers of core, clearing, compliance and adapter, which the acceptance profile
  turns off, so their crons fire on the fake clock;
- multiplies every Java service's timeouts by the rate, because libfaketime runs their timers at
  the rate too: at x10 a 10-second HTTP read timeout would give up after one real second. The
  HTTP client timeouts (global and per named client), the database connection and statement
  timeouts, the webhook outbox's and Redis's are listed in `faketime/timeouts.json`, which
  `faketime/timeouts.py` rebuilds from the services' `application*.yml` whenever the checkout has
  the source. The box's checkout has none and uses the committed file;
- adds `docker/localstack/998_faketime_sqs_visibility.sh`, which multiplies every queue's SQS
  visibility timeout by the rate, so a message a consumer holds for more than three real seconds
  at x10 is not delivered a second time.

WireMock and toxiproxy stay on the real clock.

## Running it on Linux

The harness also runs on a Linux host with Docker Engine, for example the mini PC on the tailnet.
Drive it from the Mac with `box/box`; run it with no arguments for the list of commands.

```bash
box/box images    # build the ten images for amd64 and load them on the box
box/box sync      # copy the harness, the sim-main stack files and their inputs, then check them
box/box start     # ./harness start on the box, detached
box/box start 10  # the same on a fake clock; box/box up 10 --at "2026-12-31 18:00" also works
box/box status    # the dashboard's verdict
box/box dump fleet12   # copy one cycle's database dumps to ~/Downloads
```

`BOX_HOST`, `BOX_STACK_SRC`, `BOX_PERF_SRC` and `BOX_HARNESS_SRC` override where it connects and
what it copies. The box keeps at most `SIM_DUMP_BUDGET_GB` (default 100) of dumps, removing the least
recently used first (`prune_dumps.py`).

- Build the images on the Mac for `linux/amd64` and send them with `docker save | ssh <host> docker
  load`. The Dockerfiles only copy JARs, so the build needs no emulation.
- The host needs `postgresql-client`, `python3-httpx` and `python3-yaml`.
- Set `SIM_STACK_REPO` to a directory named `sim-main` that holds the compose files, `docker/`,
  `scripts/` and `local-*.sh`. Set `SIM_AUTH_DIR` to a copy of the ops credentials.
- `stack_patch.py` adds `docker/docker-compose.linux-host.yml` on Linux. It maps
  `host.docker.internal` to the host, because Linux Docker does not define that name and the
  services send webhooks and Cognito calls to the harness through it.
- `caffeinate` and `~/.claude/bin/stack-lock` are used only where they exist.
- `box/box sync` copies `faketime/` with the rest of the tracked files, and `stack_patch.py` picks
  the `amd64` libraries by the host's architecture.

## Dashboard

`dashboard/dash.py` serves a health page on `127.0.0.1:8440`: the host's load, memory, swap, disk
and temperature, each container's state and memory against its limit, ops-api `/health`, the loop,
the current cycle against its time budget, and the recent cycles. `/api/status` returns the same
data as JSON. It uses only the standard library.

```bash
crontab -e                  # @reboot and * * * * * <repo>/dashboard/run.sh
tailscale serve --bg 8440   # https://<host>.<tailnet>.ts.net, tailnet only
```

## Files a cycle writes

- `fleetN-pK.json` and `fleetN-pK.html`: the trials, violations and journeys of each run.
- `fleet.progress`: one line when a cycle starts and one when it ends.
- `archive/fleetN.tgz` and `archive/fleetN-db`: the logs of the cycle and a dump of the databases.
- `cleared-groups.jsonl`, `approved-duplicate-holds.jsonl` and `refused-group-decisions.jsonl`:
  every payment group the harness failed, approved or decided on behalf of an operator.
