#!/bin/bash
# Fleet cycles back to back until the file `stop.fleet` exists in this directory.
#
#   FLEET_PLATFORMS=4 FLEET_SECONDS=3600 ./fleet_loop.sh <first cycle number>
#
# Each cycle reads the harness code afresh, so an edit between cycles takes effect on the next
# one. The stack lease is renewed every four minutes while the loop runs, and released at the end.
cd "$(dirname "$0")" || exit 1
LOCK=~/.claude/bin/stack-lock
export SIM_STACK_REPO=${SIM_STACK_REPO:-/Users/joelreason/IdeaProjects/exchange.worktrees/sim-main}
export SIM_POOL_LIVE=${SIM_POOL_LIVE:-20}
PLATFORMS=${FLEET_PLATFORMS:-4}
SECONDS_PER_CYCLE=${FLEET_SECONDS:-3600}
n=${1:-1}
rm -f stop.fleet
"$LOCK" mutate > /dev/null 2>&1
( while [ ! -f stop.fleet ]; do "$LOCK" use > /dev/null 2>&1; sleep 240; done ) &
KEEP=$!
while [ ! -f stop.fleet ]; do
  name="fleet$n"
  echo "{\"name\": \"$name\", \"started\": \"$(date '+%Y-%m-%d %H:%M:%S')\"}" >> fleet.progress
  caffeinate -i -s python3 -u fleet_cycle.py "$SECONDS_PER_CYCLE" "$name" "$PLATFORMS" > "$name.cycle.log" 2>&1
  echo "CYCLE_EXIT $?" >> "$name.cycle.log"
  python3 -u after_cycle.py "$name" >> "$name.cycle.log" 2>&1
  n=$((n + 1))
done
kill "$KEEP" 2>/dev/null
"$LOCK" mutate-end > /dev/null 2>&1
"$LOCK" release > /dev/null 2>&1
