# simulation-explorer

A copy of `tools/simulation-explorer` from the exchange repo, kept here so the work is safe
while it is not yet committed there. The design and the findings are in `docs/`.

The harness drives the Direct API against the local exchange stack, injects faults, and checks
the results against oracles and against the transition chains in core. `cycle.py` wipes the
stack, stands a cohort up and runs one exploration; `live.html` shows a run while it is going.

It does not run on its own. It expects to sit at `tools/simulation-explorer` inside an exchange
checkout, because it imports `tools/performance-testing/lib`, reads credentials from
`tools/performance-testing/data/auth`, and calls `./local-down.sh` and `./scripts/launch_stack`
from the repo root.

To refresh this copy, copy the source files over again from the exchange checkout and commit.
