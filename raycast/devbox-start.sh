#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Start Devbox
# @raycast.mode compact

# Optional parameters:
# @raycast.icon 🟢
# @raycast.packageName Devbox
# @raycast.description Start vm-devbox-lee, which routes the private t1448 Azure endpoints over Tailscale.

# Raycast runs scripts with a minimal PATH, so az is called by its full path.
AZ=/opt/homebrew/bin/az
RG=RG-DEVBOX-LEE-DEV-UKS
VM=vm-devbox-lee

state=$("$AZ" vm get-instance-view -g "$RG" -n "$VM" \
  --query "instanceView.statuses[?starts_with(code,'PowerState')].displayStatus | [0]" -o tsv 2>&1) \
  || { echo "❌ Could not read devbox state (run 'az login'?)"; exit 1; }

if [ "$state" = "VM running" ]; then
  echo "✅ Devbox already running"
  exit 0
fi

"$AZ" vm start -g "$RG" -n "$VM" >/dev/null 2>&1 \
  && echo "✅ Devbox started" \
  || { echo "❌ Devbox failed to start (was: $state)"; exit 1; }
