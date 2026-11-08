#!/bin/bash
set -euo pipefail
phase="${1:-rebuild}"
cd /build
if [ "$BENCH_ENGINE" = postgres ]; then
  if [ "$phase" = initial ]; then
    /src/configure --prefix=/opt/pg --without-icu CFLAGS='-O1 -g0'
  fi
  make -j"${MAKE_JOBS:-12}"
  make install
elif [ "$BENCH_ENGINE" = sqlite ]; then
  if [ "$phase" = initial ]; then
    /src/configure CFLAGS='-O1 -g0' --disable-shared
  fi
  make -j"${MAKE_JOBS:-12}" sqlite3.c testfixture
  gcc -shared -fPIC -O1 -g0 -I. sqlite3.c -o /build/libsqlitepilot.so -ldl -lpthread -lm
else
  echo "No implemented build adapter for $BENCH_ENGINE" >&2
  exit 2
fi
