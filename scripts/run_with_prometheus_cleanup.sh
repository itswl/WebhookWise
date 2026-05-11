#!/bin/sh
set -eu

child_pid=""

cleanup_prometheus_child() {
    if [ -z "${PROMETHEUS_MULTIPROC_DIR:-}" ] || [ -z "${child_pid:-}" ]; then
        return
    fi
    python3 - "${child_pid}" <<'PY' || true
import sys

from prometheus_client import multiprocess

multiprocess.mark_process_dead(int(sys.argv[1]))
PY
}

forward_signal() {
    if [ -n "${child_pid:-}" ]; then
        kill -TERM "${child_pid}" 2>/dev/null || true
    fi
}

trap forward_signal TERM INT

"$@" &
child_pid=$!

set +e
wait "${child_pid}"
status=$?
set -e

cleanup_prometheus_child
exit "${status}"
