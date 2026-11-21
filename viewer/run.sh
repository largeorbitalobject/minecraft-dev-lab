#!/usr/bin/env bash
# Foreground start: collector threads + HTTP server on 0.0.0.0:8095. Ctrl-C to stop.
# Options pass through: --port N, --data DIR, --config FILE, --no-collector, --verbose
cd "$(dirname "$(readlink -f "$0")")" || exit 1
# Fewer malloc arenas: the poller and HTTP threads otherwise each fragment their own heap.
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"
exec /usr/bin/python3 -u -m mcdevlab_viewer.server "$@"
