#!/usr/bin/env bash
# Optional helper: serve WGPendler over real HTTPS on your tailnet with `tailscale serve`.
# The app does not depend on this. Usage: scripts/phone-https.sh [start|stop|status] [port]
set -euo pipefail

cmd="${1:-start}"
port="${2:-8000}"

if ! command -v tailscale >/dev/null 2>&1; then
  echo "tailscale not found. Install it from https://tailscale.com/download and run 'tailscale up'." >&2
  exit 1
fi

case "$cmd" in
  start)
    # Tailscale terminates TLS and proxies to the local app; no --host 0.0.0.0 needed,
    # so the app stays unreachable from the plain LAN.
    tailscale serve --bg "$port"
    echo
    tailscale serve status
    echo
    echo "Open the https://<machine>.<tailnet>.ts.net URL above on your phone (Tailscale app on)."
    echo "If HTTPS is not enabled for your tailnet, enable it in the admin console (DNS > HTTPS Certificates)."
    if [ -n "${WGP_ACCESS_TOKEN:-}" ]; then
      echo "Token is set: first visit https://<that-url>/?token=\$WGP_ACCESS_TOKEN once."
    fi
    ;;
  stop)
    tailscale serve reset
    ;;
  status)
    tailscale serve status
    ;;
  *)
    echo "usage: $0 [start|stop|status] [port]" >&2
    exit 2
    ;;
esac
