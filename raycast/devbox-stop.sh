#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Stop Devbox
# @raycast.mode compact

# Optional parameters:
# @raycast.icon 🔴
# @raycast.packageName Devbox
# @raycast.description Deallocate vm-devbox-lee so it stops incurring compute charges.

# Raycast runs scripts with a minimal PATH, so az is called by its full path.
AZ=/opt/homebrew/bin/az
RG=RG-DEVBOX-LEE-DEV-UKS
VM=vm-devbox-lee

state=$("$AZ" vm get-instance-view -g "$RG" -n "$VM" \
  --query "instanceView.statuses[?starts_with(code,'PowerState')].displayStatus | [0]" -o tsv 2>&1) \
  || { echo "❌ Could not read devbox state (run 'az login'?)"; exit 1; }

if [ "$state" = "VM deallocated" ]; then
  echo "✅ Devbox already stopped"
  exit 0
fi

# Deallocate rather than stop: a stopped-but-allocated VM is still billed.
"$AZ" vm deallocate -g "$RG" -n "$VM" >/dev/null 2>&1 \
  && echo "✅ Devbox stopped (deallocated)" \
  || { echo "❌ Devbox failed to stop (was: $state)"; exit 1; }
