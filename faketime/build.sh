#!/bin/bash
# Copy the distribution's own libfaketime into faketime/<base>-<arch>/ for each base the stack runs.
#
#   faketime/build.sh            both architectures
#   faketime/build.sh arm64      one
#
# A library built against a newer C library does not load on an older one, so each base gets the
# build its own distribution packages. trixie covers the Java services (distroless java25-debian13),
# postgres:17.9 and LocalStack 4.14, which are all Debian 13. alpine covers redis:7.2-alpine (musl).
set -e
cd "$(dirname "$0")"
for arch in ${1:-arm64 amd64}; do
  mkdir -p "trixie-$arch" "alpine-$arch"
  docker run --rm --platform "linux/$arch" -v "$PWD/trixie-$arch:/out" debian:trixie bash -c '
    apt-get update -qq && apt-get install -y -qq libfaketime >/dev/null &&
    cp /usr/lib/*-linux-gnu/faketime/libfaketimeMT.so.1 /out/ &&
    dpkg -s libfaketime | grep "^Version" > /out/VERSION'
  docker run --rm --platform "linux/$arch" -v "$PWD/alpine-$arch:/out" alpine:3.21 sh -c '
    apk add -q libfaketime &&
    cp "$(find / -name "libfaketimeMT.so.1" -not -path "/out/*" | head -1)" /out/ &&
    apk list -I libfaketime > /out/VERSION'
  ls -l "trixie-$arch" "alpine-$arch"
done
