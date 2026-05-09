#!/usr/bin/env bash
# Bootstrap ax on a Hetzner (or any) Linux host with Docker.
# Prompts for PLATFORM_BASE_DOMAIN and RUNNER_TOKEN when unset and not in infra/.env.
#
# Pairing: on your laptop run `ax generate` once, use the same token here (paste or export RUNNER_TOKEN).
# Non-interactive: export PLATFORM_BASE_DOMAIN and RUNNER_TOKEN before running.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="${REPO_ROOT}/infra"
ENV_FILE="${INFRA_DIR}/.env"
COMPOSE_FILE="${INFRA_DIR}/docker-compose.yml"

die() {
  echo "error: $*" >&2
  exit 1
}

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || die "missing '$1'. Install it and retry."
}

if [[ ! -f "$COMPOSE_FILE" ]]; then
  die "compose file not found: ${COMPOSE_FILE} (run this from a clone of the ax repo)"
fi

# Load existing infra/.env so we only prompt for missing values.
if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

PLATFORM_BASE_DOMAIN="${PLATFORM_BASE_DOMAIN:-}"
RUNNER_TOKEN="${RUNNER_TOKEN:-}"

if [[ -z "$PLATFORM_BASE_DOMAIN" ]]; then
  if [[ ! -t 0 ]]; then
    die "PLATFORM_BASE_DOMAIN is not set and stdin is not a TTY. Export it or create ${ENV_FILE}."
  fi
  read -r -p "Platform base (hostname e.g. example.com, or IPv4 — no https://): " PLATFORM_BASE_DOMAIN
fi

if [[ -z "$RUNNER_TOKEN" ]]; then
  if [[ ! -t 0 ]]; then
    die "RUNNER_TOKEN is not set and stdin is not a TTY. Export it (e.g. from \`ax generate\` on your laptop) or create ${ENV_FILE}."
  fi
  echo "Runner API token: use the value from your laptop (\`ax generate\` → ~/.config/ax/runner-token), or enter a new secret."
  _suggest=""
  if command -v openssl >/dev/null 2>&1; then
    _suggest="$(openssl rand -hex 24)"
  fi
  read -r -p "RUNNER_TOKEN [empty = use suggested random]: " RUNNER_TOKEN
  RUNNER_TOKEN="${RUNNER_TOKEN:-$_suggest}"
fi

[[ -n "$PLATFORM_BASE_DOMAIN" ]] || die "PLATFORM_BASE_DOMAIN is empty."
[[ -n "$RUNNER_TOKEN" ]] || die "RUNNER_TOKEN is empty."

# Normalize: strip accidental scheme / trailing slash
PLATFORM_BASE_DOMAIN="${PLATFORM_BASE_DOMAIN#https://}"
PLATFORM_BASE_DOMAIN="${PLATFORM_BASE_DOMAIN#http://}"
PLATFORM_BASE_DOMAIN="${PLATFORM_BASE_DOMAIN%/}"

umask 077
cat >"$ENV_FILE" <<EOF
PLATFORM_BASE_DOMAIN=${PLATFORM_BASE_DOMAIN}
RUNNER_TOKEN=${RUNNER_TOKEN}
EOF
echo "wrote ${ENV_FILE} (mode 600)"

require_cmd python3
SUBDOMAIN_FILE="${INFRA_DIR}/caddy-runner-subdomain.caddy"
python3 -c "
import ipaddress
import pathlib
import sys

h = sys.argv[1].strip()
path = pathlib.Path(sys.argv[2])
try:
    ipaddress.ip_address(h.strip('[]'))
except ValueError:
    path.write_text(
        f'runner.{h} {{\n\treverse_proxy ax-runner:8080\n}}\n',
        encoding='utf-8',
    )
else:
    path.write_text(
        f'# PLATFORM_BASE_DOMAIN is an IP ({h}). Runner API: http://{h}/v1\n',
        encoding='utf-8',
    )
" "$PLATFORM_BASE_DOMAIN" "$SUBDOMAIN_FILE"
echo "wrote ${SUBDOMAIN_FILE}"

require_cmd docker
if ! docker compose version >/dev/null 2>&1; then
  die "docker compose plugin missing. Install docker-compose-plugin."
fi

echo "building and starting stack..."
docker compose -f "$COMPOSE_FILE" up --build -d

echo ""
echo "Health checks:"
export _AX_PBD="$PLATFORM_BASE_DOMAIN"
if python3 -c "import ipaddress, os; ipaddress.ip_address(os.environ['_AX_PBD'].strip('[]'))" 2>/dev/null; then
  echo "  curl -fsS http://${PLATFORM_BASE_DOMAIN}/health"
else
  echo "  curl -fsS https://${PLATFORM_BASE_DOMAIN}/_ax/health"
  echo "  curl -fsS https://runner.${PLATFORM_BASE_DOMAIN}/health"
  echo "  (optional) curl -fsS http://${PLATFORM_BASE_DOMAIN}/health"
fi
unset _AX_PBD
echo ""
echo "On your laptop, install the CLI and log in:"
echo "  uv tool install -e ${REPO_ROOT}/cli"
echo "  ax login ${PLATFORM_BASE_DOMAIN}"
echo "  (token: value of RUNNER_TOKEN in ${ENV_FILE}, or run ax generate first)"
echo ""
