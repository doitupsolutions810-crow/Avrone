#!/bin/sh
set -eu
git config --global --add safe.directory "${CROW_REPO:-/repo}" || true
exec python run.py
