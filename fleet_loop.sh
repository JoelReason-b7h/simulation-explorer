#!/bin/bash
# Fleet cycles back to back until the file `stop.fleet` exists in this directory.
#
#   FLEET_PLATFORMS=4 ./fleet_loop.sh <first cycle number>
#
# The length of each cycle is read from fleet.seconds when the cycle starts, 900 when the file is
# missing. after_cycle.py writes it: longer after a quiet cycle, back to 900 after anything else.
#
# Each cycle reads the harness code afresh, so an edit between cycles takes effect on the next
# one. The stack lease is renewed every four minutes while the loop runs, and released at the end.
cd "$(dirname "$0")" || exit 1
# bash reads a script as it runs, so an edit to this file while it loops changes what the running
# loop does next: one edit restarted a cycle under the same name and overwrote its evidence. The
# loop therefore runs from a copy, and this file can be edited at any time.
if [ -z "$FLEET_LOOP_COPY" ]; then
  cp "$0" .fleet_loop.running.sh
  FLEET_LOOP_COPY=1 exec bash .fleet_loop.running.sh "$@"
fi
LOCK=~/.claude/bin/stack-lock
export SIM_STACK_REPO=${SIM_STACK_REPO:-/Users/joelreason/IdeaProjects/exchange.worktrees/sim-main}
export SIM_POOL_LIVE=${SIM_POOL_LIVE:-20}
PLATFORMS=${FLEET_PLATFORMS:-4}
AWAKE=()
command -v caffeinate > /dev/null && AWAKE=(caffeinate -i -s)
n=${1:-1}
rm -f stop.fleet
"$LOCK" mutate > /dev/null 2>&1
( while [ ! -f stop.fleet ]; do "$LOCK" use > /dev/null 2>&1; sleep 240; done ) &
KEEP=$!
while [ ! -f stop.fleet ]; do
  while [ -e "archive/fleet$n.tgz" ] || [ -e "fleet$n.cohorts.json" ]; do n=$((n + 1)); done
  name="fleet$n"
  SECONDS_PER_CYCLE=$(cat fleet.seconds 2>/dev/null || echo 900)
  echo "{\"name\": \"$name\", \"started\": \"$(date '+%Y-%m-%d %H:%M:%S')\", \"seconds\": $SECONDS_PER_CYCLE}" >> fleet.progress
  "${AWAKE[@]}" python3 -u fleet_cycle.py "$SECONDS_PER_CYCLE" "$name" "$PLATFORMS" > "$name.cycle.log" 2>&1
  echo "CYCLE_EXIT $?" >> "$name.cycle.log"
  python3 -u after_cycle.py "$name" >> "$name.cycle.log" 2>&1
  n=$((n + 1))
done
kill "$KEEP" 2>/dev/null
"$LOCK" mutate-end > /dev/null 2>&1
"$LOCK" release > /dev/null 2>&1
