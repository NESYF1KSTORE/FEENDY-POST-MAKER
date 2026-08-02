#!/usr/bin/env bash
# Run on YOUR machine. Copies this checkout to the server over SSH and deploys.
# Use this when the server should not clone from GitHub itself.
#
#   ./deploy/push.sh --host 203.0.113.10 [--user root] [--domain fynix.example.com]
#
# Authentication uses your SSH key. If the server only accepts a password,
# install `sshpass` and export SSHPASS=... — but adding a key is the better fix.
set -Eeuo pipefail

HOST=""
USER_NAME="root"
DOMAIN=""
REMOTE_DIR="/opt/fynix"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --host)   HOST="$2"; shift 2 ;;
    --user)   USER_NAME="$2"; shift 2 ;;
    --domain) DOMAIN="$2"; shift 2 ;;
    --dir)    REMOTE_DIR="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

[[ -n "$HOST" ]] || { echo "usage: $0 --host <ip-or-host> [--domain <domain>]" >&2; exit 2; }

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH=(ssh -o StrictHostKeyChecking=accept-new "${USER_NAME}@${HOST}")
SCP_BASE=(rsync -az --delete
  --exclude '.venv' --exclude '__pycache__' --exclude '.pytest_cache'
  --exclude '.ruff_cache' --exclude '*.pyc' --exclude '.env')

if [[ -n "${SSHPASS:-}" ]]; then
  command -v sshpass >/dev/null || { echo "SSHPASS is set but sshpass is not installed" >&2; exit 1; }
  SSH=(sshpass -e "${SSH[@]}")
  SCP_BASE+=(-e "sshpass -e ssh -o StrictHostKeyChecking=accept-new")
fi

log() { printf '\n\033[1;35m==>\033[0m %s\n' "$*"; }

log "preparing ${REMOTE_DIR}/fynix on ${HOST}"
"${SSH[@]}" "mkdir -p ${REMOTE_DIR}/fynix"

log "syncing sources"
"${SCP_BASE[@]}" "${HERE}/" "${USER_NAME}@${HOST}:${REMOTE_DIR}/fynix/"

log "running the installer remotely"
REMOTE_ARGS=""
[[ -n "$DOMAIN" ]] && REMOTE_ARGS="--domain ${DOMAIN}"
# INSTALL_DIR is already populated, so the installer skips the clone step.
"${SSH[@]}" "cd ${REMOTE_DIR}/fynix && SKIP_CLONE=1 bash deploy/install.sh ${REMOTE_ARGS}" || {
  echo "remote install failed; check the output above" >&2
  exit 1
}

log "deployment finished"
