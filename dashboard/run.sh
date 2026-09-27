#!/bin/bash
# Starts the dashboard unless it is already running. Cron calls it at boot and every minute:
#   @reboot   <repo>/dashboard/run.sh
#   * * * * * <repo>/dashboard/run.sh
DIR=$(cd "$(dirname "$0")" && pwd)
pgrep -f "$DIR/dash.py" > /dev/null && exit 0
nohup python3 "$DIR/dash.py" >> "$DIR/dash.log" 2>&1 &
