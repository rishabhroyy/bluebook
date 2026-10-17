#!/usr/bin/env bash
# Build a .ehpk that talks to ONE laptop's server (no sideload/dev server needed on the phone):
#   ./scripts/pack-g2.sh                                   # uses this laptop's tailnet name
#   BB_ORIGIN=https://other-laptop.tailXXXX.ts.net ./scripts/pack-g2.sh
# The whitelist can't use wildcards, so each laptop packs its own build; the committed
# apps/g2/app.json keeps a placeholder and is never edited per person.
set -euo pipefail
cd "$(dirname "$0")/../apps/g2"

TS=$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)
if [ -z "${BB_ORIGIN:-}" ]; then
  BB_ORIGIN="https://$("$TS" status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
fi
echo "packing for $BB_ORIGIN"

# ponytail: sed on the placeholder; switch to a JSON tool if app.json gets more templated fields.
# Both schemes: whether the https entry also covers wss:// is undocumented (DESIGN §8 S4); pack accepts both.
sed "s|\"https://REPLACE-laptop.REPLACE-tailnet.ts.net\"|\"$BB_ORIGIN\", \"${BB_ORIGIN/https:/wss:}\"|" app.json > app.local.json
VITE_BB_SERVER="${BB_ORIGIN/https:/wss:}/ws/glasses" pnpm build
pnpm exec evenhub pack app.local.json dist -o "bluebook-$(echo "$BB_ORIGIN" | sed 's|https://||; s|\..*||').ehpk"
