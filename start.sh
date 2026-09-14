#!/bin/bash
set -e
MDIR="$HOME/memlayer"
echo "🚀 Starting MemLayer..."

# Start containers
cd "$MDIR"
podman-compose --env-file .env -f podman-compose.yml up -d

# Wait for Redis
echo "⏳ Waiting for Redis..."
for i in {1..20}; do
  podman exec memlayer_redis_1 redis-cli -a memlayer_redis_pass ping &>/dev/null && break
  sleep 1
done
echo "✅ Redis ready"

# Activate venv (needed for the embedding service and the CLI)
source "$MDIR/.venv/bin/activate"
set -a
source "$MDIR/.env"
set +a
# ── Start embedding service (dedicated batching process) ─────
echo "⏳ Starting embedding service..."
cd "$MDIR"
nohup uvicorn embedding_service:app --host 0.0.0.0 --port 8081 --workers 1 \
  > "$MDIR/embedding_service.log" 2>&1 &
echo $! > "$MDIR/.embed.pid"
for i in {1..30}; do
  curl -sf http://localhost:8081/health &>/dev/null && break
  sleep 1
done
if curl -sf http://localhost:8081/health &>/dev/null; then
  echo "✅ Embedding service ready (http://localhost:8081)"
else
  echo "⚠  Embedding service not responding — memlayer will use local fallback"
fi

echo ""
echo "✅ MemLayer backend running:"
echo "   Embeddings→ http://localhost:8081"
echo ""
echo "   Start the CLI → ./recalq"
echo "   Logs           → tail -f ~/memlayer/memlayer.log"
echo "   Stop backend   → ~/memlayer/stop.sh"
