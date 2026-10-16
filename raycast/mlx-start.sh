#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Start MLX Model Server
# @raycast.mode compact

# Optional parameters:
# @raycast.icon 🟢
# @raycast.packageName Local Models
# @raycast.description Serve the local MLX model with mlx_lm.server (OpenAI-compatible API on 127.0.0.1:8080).

# Raycast runs scripts with a minimal PATH, so mlx_lm.server is called by its full path.
SERVER=/opt/homebrew/bin/mlx_lm.server
MODEL="$HOME/.lmstudio/models/lmstudio-community/gemma-4-E4B-it-MLX-4bit"
PORT=8080
LOG="$HOME/Library/Logs/mlx-lm-server.log"

if pids=$(pgrep -f "mlx_lm[.]server"); then
  echo "✅ MLX server already running (pid $(echo $pids | tr '\n' ' '))"
  exit 0
fi

# Detach fully (stdin from /dev/null, output to a log) so Raycast doesn't wait on it.
nohup "$SERVER" --model "$MODEL" --host 127.0.0.1 --port "$PORT" \
  </dev/null >>"$LOG" 2>&1 &
disown

# Wait for the HTTP endpoint so "started" means ready to take requests.
for _ in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:$PORT/v1/models" >/dev/null; then
    echo "✅ MLX server ready on :$PORT ($(basename "$MODEL"))"
    exit 0
  fi
  pgrep -f "mlx_lm[.]server" >/dev/null || { echo "❌ MLX server exited — see $LOG"; exit 1; }
  sleep 1
done

echo "⏳ MLX server still starting after 60s — see $LOG"
