#!/usr/bin/env bash
# Stops and disables every portal service, tunnel and Docker backends included.
set -uo pipefail

UNITS=(cloudflared-tunnel.service portal.service portal-backends.service
       portal-omaha.service portal-arcade.service portal-fof.service
       portal-fof-guest.service portal-aiprep.service portal-fridge.service)
for u in "${UNITS[@]}"; do
    systemctl --user disable --now "$u" 2>/dev/null
done
systemctl --user daemon-reload
docker compose -f "$(cd "$(dirname "$0")" && pwd)/compose.yaml" down 2>/dev/null
echo "All portal services stopped and disabled."
