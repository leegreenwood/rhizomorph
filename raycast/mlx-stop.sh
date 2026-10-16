#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Stop MLX Model Server
# @raycast.mode compact

# Optional parameters:
# @raycast.icon 🔴
# @raycast.packageName Local Models
# @raycast.description Stop mlx_lm.server and free the memory its model holds.

# Match on the server's own name, never a bare "server" pattern (see CLAUDE.md).
if ! pgrep -f "mlx_lm[.]server" >/dev/null; then
  echo "✅ MLX server already stopped"
  exit 0
fi

pkill -f "mlx_lm[.]server"
for _ in $(seq 1 10); do
  pgrep -f "mlx_lm[.]server" >/dev/null || { echo "✅ MLX server stopped"; exit 0; }
  sleep 1
done

pkill -9 -f "mlx_lm[.]server"
echo "✅ MLX server stopped (forced)"
