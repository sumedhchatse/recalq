#!/bin/bash
echo "🛑 Stopping MemLayer..."
# Stop embedding service
[ -f "$HOME/memlayer/.embed.pid" ] && kill $(cat "$HOME/memlayer/.embed.pid") 2>/dev/null && rm "$HOME/memlayer/.embed.pid"
pkill -f "uvicorn embedding_service" 2>/dev/null
cd "$HOME/memlayer"
podman-compose --env-file .env -f podman-compose.yml stop
echo "✅ MemLayer stopped. Data preserved in Redis volume."
