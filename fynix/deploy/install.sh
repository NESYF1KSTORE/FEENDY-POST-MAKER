#!/usr/bin/env bash
# Run ON the server, as root. Installs Docker, deploys FYNIX and creates the
# first admin. Safe to re-run: it upgrades an existing install in place.
#
#   curl -fsSL <raw-url>/deploy/install.sh | bash -s -- --domain fynix.example.com
#
set -Eeuo pipefail

REPO_URL="${REPO_URL:-https://github.com/nesyf1kstore/feendy-post-maker.git}"
BRANCH="${BRANCH:-claude/deploy-project-server-yapfn4}"
INSTALL_DIR="${INSTALL_DIR:-/opt/fynix}"
DOMAIN=""
ADMIN_EMAIL=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --domain) DOMAIN="$2"; shift 2 ;;
    --email)  ADMIN_EMAIL="$2"; shift 2 ;;
    --branch) BRANCH="$2"; shift 2 ;;
    --repo)   REPO_URL="$2"; shift 2 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

log() { printf '\n\033[1;35m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root"

if [[ -z "$DOMAIN" ]]; then
  # No domain given: serve plain HTTP on the server's public address.
  DOMAIN=":80"
  PUBLIC_IP="$(curl -fsS --max-time 10 https://api.ipify.org || hostname -I | awk '{print $1}')"
  BASE_URL="http://${PUBLIC_IP}"
  log "no --domain given: serving HTTP on ${BASE_URL} (no TLS)"
else
  BASE_URL="https://${DOMAIN}"
fi

# ---------------------------------------------------------------- packages ---
log "installing base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git ufw openssl >/dev/null

if ! command -v docker >/dev/null 2>&1; then
  log "installing Docker"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  # shellcheck disable=SC1091
  . /etc/os-release
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
  systemctl enable --now docker
fi
docker --version

# ---------------------------------------------------------------- firewall ---
log "configuring firewall (22, 80, 443)"
ufw --force reset >/dev/null
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

# ------------------------------------------------------------------- source ---
if [[ "${SKIP_CLONE:-0}" == "1" ]]; then
  # Sources were pushed here by deploy/push.sh — cloning would delete them.
  log "SKIP_CLONE=1: using the sources already present"
  INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
elif [[ -d "$INSTALL_DIR/.git" ]]; then
  log "updating existing checkout in $INSTALL_DIR"
  git -C "$INSTALL_DIR" fetch --depth 1 origin "$BRANCH"
  git -C "$INSTALL_DIR" checkout -B "$BRANCH" "origin/$BRANCH"
else
  log "cloning $REPO_URL ($BRANCH) into $INSTALL_DIR"
  rm -rf "$INSTALL_DIR"
  git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi

APP_DIR="$INSTALL_DIR/fynix"
[[ -f "$APP_DIR/docker-compose.yml" ]] || die "docker-compose.yml not found in $APP_DIR"
cd "$APP_DIR"

# ---------------------------------------------------------------------- env ---
if [[ -f .env ]]; then
  log "keeping existing .env (secrets are preserved across upgrades)"
else
  log "generating .env with fresh secrets"
  ADMIN_EMAIL="${ADMIN_EMAIL:-admin@${DOMAIN#:}}"
  [[ "$ADMIN_EMAIL" == "admin@80" ]] && ADMIN_EMAIL="admin@fynix.local"
  cat > .env <<EOF
FYNIX_ENV=prod
FYNIX_DOMAIN=${DOMAIN}
FYNIX_BASE_URL=${BASE_URL}
FYNIX_LOG_LEVEL=INFO
POSTGRES_PASSWORD=$(openssl rand -hex 24)
JWT_SECRET=$(openssl rand -hex 32)
BOOTSTRAP_ADMIN_EMAIL=${ADMIN_EMAIL}
BOOTSTRAP_ADMIN_PASSWORD=$(openssl rand -base64 18 | tr -d '/+=' | head -c 20)
AI_DEFAULT_PROVIDER=mock
ANTHROPIC_API_KEY=
DEEPSEEK_API_KEY=
RUNNER_DRIVER=local
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
DEFAULT_PROJECT_BUDGET_RUB=50000
EOF
  chmod 600 .env
fi

# `prod` refuses to start over plain HTTP, which is correct — but an IP-only
# first deploy has no certificate yet, so run it as staging until DNS exists.
if [[ "$BASE_URL" == http://* ]]; then
  sed -i 's/^FYNIX_ENV=prod$/FYNIX_ENV=dev/' .env
fi

# ------------------------------------------------------------------- deploy ---
log "building and starting the stack"
docker compose pull --quiet --ignore-buildable 2>/dev/null || true
docker compose build --quiet
docker compose up -d --remove-orphans

log "waiting for the API to become ready"
for attempt in $(seq 1 60); do
  if docker compose exec -T api curl -fsS http://127.0.0.1:8000/health/ready >/dev/null 2>&1; then
    break
  fi
  [[ $attempt -eq 60 ]] && { docker compose logs --tail 80 api; die "API did not become ready"; }
  sleep 3
done

log "creating the first tenant and admin"
docker compose exec -T api python -m app.cli bootstrap \
  --slug fynix --tenant-name "FYNIX STUDIO" || true

log "done"
cat <<EOF

  Portal:  ${BASE_URL}/portal
  API doc: ${BASE_URL}/docs
  Health:  ${BASE_URL}/health

  Admin credentials are in ${APP_DIR}/.env
    grep BOOTSTRAP_ADMIN ${APP_DIR}/.env

  Useful commands (run from ${APP_DIR}):
    docker compose ps
    docker compose logs -f api worker
    docker compose exec api python -m app.cli demo      # offline end-to-end run
    docker compose exec api python -m app.cli verify-audit

  Next steps:
    1. Change the admin password after the first login.
    2. Point DNS at this server, then re-run with --domain <your-domain> for TLS.
    3. Set AI_DEFAULT_PROVIDER and the provider key in .env, then:
         docker compose up -d --force-recreate api worker

EOF
