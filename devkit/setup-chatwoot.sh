#!/bin/sh
# Adds the Chatwoot secrets to .env (once) and prints the hosts line. Run after ./setup.sh.
set -eu
cd "$(dirname "$0")"

[ -f .env ] || { echo "Run ./setup.sh first: there is no .env yet." >&2; exit 1; }
[ -d ../chatwoot ] || { echo "Expected the Chatwoot checkout at ../chatwoot." >&2; exit 1; }
[ -d ../chatwoot-mcp-server ] || { echo "Expected the chatwoot-mcp-server checkout at ../chatwoot-mcp-server." >&2; exit 1; }

add() {
  grep -q "^$1=" .env || { echo "$1=$(openssl rand -hex "$2")" >> .env; echo "added $1 to .env"; }
}
add CHATWOOT_SECRET_KEY_BASE 64
add CHATWOOT_POSTGRES_PASSWORD 24

HOSTS="127.0.0.1 foss.local.dev auth.foss.local.dev whoami.foss.local.dev chatwoot.foss.local.dev chat-mcp.foss.local.dev"
if grep -q "chatwoot.foss.local.dev" /etc/hosts; then
  echo "/etc/hosts already has the Chatwoot names."
else
  echo "Add this line to /etc/hosts (replace any earlier foss.local.dev line):"
  echo "  sudo sh -c 'echo \"$HOSTS\" >> /etc/hosts'"
fi
