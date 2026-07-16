#!/usr/bin/env bash
# Builds the miles-api image, stamping it with the current git hash and
# build time so the UI can display them (see nav.js / /api/version).
set -euo pipefail

export GIT_HASH="$(git rev-parse --short HEAD)"
export BUILD_TIME="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

docker compose up --build "$@" -d
