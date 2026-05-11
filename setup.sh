#!/usr/bin/env bash
# Bootstrap ax on a Linux host with systemd, Caddy, and uv.
#
# Pairing: on your laptop run `ax generate` once, use the same token here.
# Non-interactive: export PLATFORM_BASE_DOMAIN and RUNNER_TOKEN before running.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="${REPO_ROOT}/infra"
ENV_FILE="${INFRA_DIR}/.env"
RUNNER_DIR="${REPO_ROOT}/runner"
CADDY_SOURCE="${INFRA_DIR}/Caddyfile"
CADDY_TARGET="/etc/caddy/Caddyfile"
RUNNER_ENV="/etc/ax/runner.env"
RUNNER_SERVICE="/etc/systemd/system/ax-runner.service"
CADDY_DROPIN_DIR="/etc/systemd/system/caddy.service.d"
CADDY_DROPIN="${CADDY_DROPIN_DIR}/ax.conf"

die() {
  echo "error: $*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing '$1'. Install it and retry."
}

if [[ "$(uname -s)" != "Linux" ]]; then
  die "setup.sh now targets Linux hosts with systemd. For local CLI dev, use uv sync in cli/."
fi

if [[ "${EUID}" -ne 0 ]]; then
  die "run setup as root so it can write /etc/caddy and /etc/systemd/system"
fi

[[ -f "$CADDY_SOURCE" ]] || die "Caddyfile not found: ${CADDY_SOURCE}"
[[ -f "${RUNNER_DIR}/pyproject.toml" ]] || die "runner project not found: ${RUNNER_DIR}"

require_cmd uv
require_cmd caddy
require_cmd systemctl
UV_BIN="$(command -v uv)"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

PLATFORM_BASE_DOMAIN="${PLATFORM_BASE_DOMAIN:-}"
RUNNER_TOKEN="${RUNNER_TOKEN:-}"

prompt_yes_no() {
  local prompt="$1"
  local default="${2:-y}"
  local reply=""
  local suffix="[Y/n]"
  if [[ "$default" == "n" ]]; then
    suffix="[y/N]"
  fi
  while true; do
    read -r -p "${prompt} ${suffix} " reply
    reply="${reply,,}"
    if [[ -z "$reply" ]]; then
      reply="$default"
    fi
    case "$reply" in
      y|yes) return 0 ;;
      n|no) return 1 ;;
    esac
  done
}

if [[ -n "$PLATFORM_BASE_DOMAIN" && -t 0 ]]; then
  if prompt_yes_no "Use existing PLATFORM_BASE_DOMAIN=${PLATFORM_BASE_DOMAIN}?" "y"; then
    :
  else
    PLATFORM_BASE_DOMAIN=""
  fi
fi

if [[ -z "$PLATFORM_BASE_DOMAIN" ]]; then
  if [[ ! -t 0 ]]; then
    die "PLATFORM_BASE_DOMAIN is not set and stdin is not a TTY. Export it or create ${ENV_FILE}."
  fi
  read -r -p "Runner host / PLATFORM_BASE_DOMAIN (hostname or IP, no https://): " PLATFORM_BASE_DOMAIN
fi

if [[ -n "$RUNNER_TOKEN" && -t 0 ]]; then
  if prompt_yes_no "Use existing RUNNER_TOKEN from environment or ${ENV_FILE}?" "y"; then
    :
  else
    RUNNER_TOKEN=""
  fi
fi

if [[ -z "$RUNNER_TOKEN" ]]; then
  if [[ ! -t 0 ]]; then
    die "RUNNER_TOKEN is not set and stdin is not a TTY. Export it or create ${ENV_FILE}."
  fi
  echo "Runner API token: use the value from your laptop (\`ax generate\`), or enter a new secret."
  _suggest=""
  if command -v openssl >/dev/null 2>&1; then
    _suggest="$(openssl rand -hex 24)"
  fi
  read -r -p "RUNNER_TOKEN [empty = use suggested random]: " RUNNER_TOKEN
  RUNNER_TOKEN="${RUNNER_TOKEN:-$_suggest}"
fi

[[ -n "$PLATFORM_BASE_DOMAIN" ]] || die "PLATFORM_BASE_DOMAIN is empty."
[[ -n "$RUNNER_TOKEN" ]] || die "RUNNER_TOKEN is empty."

PLATFORM_BASE_DOMAIN="${PLATFORM_BASE_DOMAIN#https://}"
PLATFORM_BASE_DOMAIN="${PLATFORM_BASE_DOMAIN#http://}"
PLATFORM_BASE_DOMAIN="${PLATFORM_BASE_DOMAIN%/}"

install -d -m 700 /etc/ax
install -d -m 755 /etc/caddy/apps/platform-routes
install -d -m 755 "$CADDY_DROPIN_DIR"
install -d -m 755 /var/lib/ax/apps /var/lib/ax/python /var/cache/ax/uv

umask 077
cat >"$ENV_FILE" <<EOF
PLATFORM_BASE_DOMAIN=${PLATFORM_BASE_DOMAIN}
RUNNER_TOKEN=${RUNNER_TOKEN}
EOF

cat >"$RUNNER_ENV" <<EOF
PLATFORM_BASE_DOMAIN=${PLATFORM_BASE_DOMAIN}
RUNNER_TOKEN=${RUNNER_TOKEN}
RUNNER_DATA_DIR=/var/lib/ax
CADDY_APPS_DIR=/etc/caddy/apps
CADDY_CONFIG=/etc/caddy/Caddyfile
SYSTEMD_DIR=/etc/systemd/system
UV_CACHE_DIR=/var/cache/ax/uv
UV_PYTHON_INSTALL_DIR=/var/lib/ax/python
UV_BIN=${UV_BIN}
EOF

echo "synching runner environment..."
uv sync --project "$RUNNER_DIR" --frozen

install -m 644 "$CADDY_SOURCE" "$CADDY_TARGET"

cat >"$CADDY_DROPIN" <<EOF
[Service]
EnvironmentFile=${RUNNER_ENV}
EOF

cat >"$RUNNER_SERVICE" <<EOF
[Unit]
Description=ax runner
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=${RUNNER_ENV}
WorkingDirectory=${RUNNER_DIR}
ExecStart=${RUNNER_DIR}/.venv/bin/python -m runner.main
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now caddy
systemctl restart caddy
systemctl enable --now ax-runner
systemctl restart ax-runner

echo ""
echo "Health check from your laptop:"
echo "  ax health"
echo "Server checks:"
echo "  systemctl status ax-runner"
echo "  systemctl status caddy"
echo ""
echo "On your laptop:"
echo "  ax login ${PLATFORM_BASE_DOMAIN}"
echo "  ax init"
echo "  ax deploy"
