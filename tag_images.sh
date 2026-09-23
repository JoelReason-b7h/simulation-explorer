#!/bin/bash
# Copies the ten service images under the simharness prefix, so this worktree's stack runs its own
# build even after another session retags e2e/*:latest. local-common.sh fixes IMAGE_TAG to latest
# but lets IMAGE_HOST be overridden, so launching with IMAGE_HOST=simharness picks these up.
#
#   ./tag_images.sh            copy e2e/*:latest to simharness/*:latest
#
# Run it straight after building the images from this worktree, and only then.
set -e
for service in \
  savings-exchange-core savings-exchange-adapter savings-exchange-clearing \
  savings-exchange-hot-sauce-bank savings-exchange-api-ops savings-exchange-api-compliance \
  savings-exchange-api-public savings-exchange-simulator-api forge-compliance forge-notification
do
  docker tag "e2e/${service}:latest" "simharness/${service}:latest"
  echo "tagged ${service}"
done
