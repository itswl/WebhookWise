#!/bin/sh
set -eu

api_host="${HOST:-0.0.0.0}"
api_port="${PORT:-8000}"
api_workers="${API_WORKERS:-4}"

exec gunicorn \
    -c gunicorn_config.py \
    --bind "${api_host}:${api_port}" \
    --workers "${api_workers}" \
    -k uvicorn.workers.UvicornWorker \
    --timeout 120 \
    --graceful-timeout 30 \
    -e UVICORN_LOOP=uvloop \
    -e UVICORN_HTTP=httptools \
    core.app:app
