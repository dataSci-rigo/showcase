#!/usr/bin/env bash
# Installs the portal's systemd USER units (symlinks into ~/.config/systemd/user)
# and starts everything. Safe to re-run.
#
#   ./install_services.sh          -> Docker mode (default): portal + cloudflared
#                                     on the host, the five app backends via
#                                     docker compose (needs docker group access:
#                                     sudo usermod -aG docker $USER; re-login)
#   ./install_services.sh --bare   -> no Docker: each backend as its own
#                                     systemd user unit on the host
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

BARE_UNITS=(portal-omaha.service portal-arcade.service portal-fof.service portal-fof-guest.service portal-aiprep.service portal-fridge.service)
if [[ "${1:-}" == "--bare" ]]; then
    UNITS=(portal.service "${BARE_UNITS[@]}" cloudflared-tunnel.service)
    OFF=(portal-backends.service)
else
    if ! docker ps >/dev/null 2>&1; then
        echo "docker is not usable by $USER yet (sudo usermod -aG docker $USER; re-login)," >&2
        echo "or run  ./install_services.sh --bare  for the no-Docker fallback." >&2
        exit 1
    fi
    UNITS=(portal.service portal-backends.service cloudflared-tunnel.service)
    OFF=("${BARE_UNITS[@]}")
fi

mkdir -p "$UNIT_DIR"
for u in "${UNITS[@]}"; do
    ln -sf "$HERE/systemd/$u" "$UNIT_DIR/$u"
done
systemctl --user daemon-reload

# make sure the other mode's units aren't also holding the ports
for u in "${OFF[@]}"; do
    systemctl --user disable --now "$u" 2>/dev/null || true
done

for u in "${UNITS[@]}"; do
    systemctl --user enable --now "$u"
done

# keep user services running after logout / before login
loginctl enable-linger "$USER" || true

echo
systemctl --user --no-pager --plain list-units 'portal*' 'cloudflared*'
echo
echo "Portal:  http://127.0.0.1:8100/_portal/  (public URL comes from the Cloudflare tunnel)"
