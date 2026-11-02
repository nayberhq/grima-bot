#!/bin/sh
set -eu
# Keep code immutable across releases; only state and secrets are mounted.
ln -sf /run/grima/config.json /app/source/config.json
exec python /app/source/main.py
