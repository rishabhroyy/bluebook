#!/usr/bin/env bash
# Run all of bluebook on THIS laptop, independent of anyone else's:
#   ./scripts/dev.sh          server + glasses app dev server + QR to sideload on your phone
#   ./scripts/dev.sh --sim    same, plus the Even Hub simulator (no glasses needed)
# Everything that differs per laptop (.env, data/, tailnet hostname) is local and gitignored.
set -euo pipefail
cd "$(dirname "$0")/.."

need() { command -v "$1" >/dev/null || { echo "missing $1 — install: $2" >&2; exit 1; }; }
need uv "https://docs.astral.sh/uv/getting-started/installation/"
need pnpm "npm i -g pnpm"

[ -f .env ] || { cp .env.example .env; echo "created .env — put your own GEMINI_API_KEY in it"; }
uv sync -q
(cd apps/g2 && pnpm install --silent)
mkdir -p data/skills
[ -n "$(ls -A data/skills)" ] || cp -r fixtures/skills/* data/skills/  # first run: sample skill

TS=$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)
lan_ip() { ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}'; }

trap 'kill 0' EXIT  # stop everything this script started on Ctrl-C
uv run bluebook-server &
(cd apps/g2 && pnpm dev >/dev/null) &
sleep 2

if [ -x "$TS" ] && "$TS" status >/dev/null 2>&1; then
  "$TS" serve --bg 8787 >/dev/null  # https://<this-laptop>.<tailnet>.ts.net -> 127.0.0.1:8787
  HOST=$("$TS" status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')
  QR_URL="http://$("$TS" ip -4 | head -1):5173"
  echo "scene camera (tripod phone, Safari): https://$HOST/camera?id=scene"
else
  QR_URL="http://$(lan_ip):5173"
  echo "tailscale not running: phone must be on the same Wi-Fi; phone camera page needs https (tailscale)."
  echo "scene camera via this laptop's webcam: http://localhost:8787/camera?id=scene"
fi

[ "${1:-}" = "--sim" ] && (cd apps/g2 && pnpm sim) &
echo "glasses app: scan in the Even Realities app -> $QR_URL"
(cd apps/g2 && pnpm exec evenhub qr --url "$QR_URL")
wait
