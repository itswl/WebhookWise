#!/bin/sh
set -eu

exec gunicorn \
    -c gunicorn_config.py \
    core.app:app
