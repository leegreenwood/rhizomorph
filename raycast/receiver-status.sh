#!/bin/bash

# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Rhizomorph Receiver Status
# @raycast.mode fullOutput

# Optional parameters:
# @raycast.icon 🍄
# @raycast.packageName Rhizomorph
# @raycast.description Show whether the Netlify and GitHub webhook receivers are running.

# Read-only: reports process state, never starts or stops anything.
for name in netlify github; do
  if pids=$(pgrep -f "receivers/$name/server.js"); then
    echo "✅ $name running (pid $(echo $pids | tr '\n' ' '))"
  else
    echo "❌ $name not running"
  fi
done
