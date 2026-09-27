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
- `SIM_STACK_REPO`: the sim-main checkout (default the Mac path in `exchange.worktrees`).
- `SIM_AUTH_DIR`: the folder that holds the ops credentials (`local.env`).

## Running it on Linux

The harness also runs on a Linux host with Docker Engine, for example the mini PC on the tailnet.
Drive it from the Mac with `box/box`; run it with no arguments for the list of commands.

```bash
box/box images    # build the ten images for amd64 and load them on the box
box/box sync      # copy the harness, the sim-main stack files and their inputs, then check them
box/box start     # ./harness start on the box, detached
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
