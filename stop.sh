#!/bin/bash
MDIR="$(cd "$(dirname "$0")" && pwd)"
echo "🛑 Stopping MemLayer..."
# Stop embedding service
[ -f "$MDIR/.embed.pid" ] && kill $(cat "$MDIR/.embed.pid") 2>/dev/null && rm "$MDIR/.embed.pid"
pkill -f "uvicorn embedding_service" 2>/dev/null
cd "$MDIR"
podman-compose --env-file .env -f podman-compose.yml stop
echo "✅ MemLayer stopped. Data preserved in Redis volume."
