#!/bin/bash
# Recalq — snapshot Redis cache to timestamped backup
# Uses redis-cli --rdb (runs inside container, avoids host permission issues)
set -e

BDIR="$HOME/memlayer-backups/redis"
mkdir -p "$BDIR"
DATE=$(date +%Y%m%d_%H%M%S)

# Load REDIS_PASSWORD if not already in env
if [ -z "$REDIS_PASSWORD" ]; then
    set -a
    source "$HOME/memlayer/.env"
    set +a
fi

# Method 1: dump full DB via redis-cli --rdb to a path inside the container,
# then podman cp it out (container user owns it, cp via podman works)
podman exec memlayer_redis_1 sh -c \
  "redis-cli -a '$REDIS_PASSWORD' --rdb /tmp/backup_${DATE}.rdb" >/dev/null 2>&1

podman cp "memlayer_redis_1:/tmp/backup_${DATE}.rdb" "$BDIR/dump_${DATE}.rdb"

# Clean up the temp file inside the container
podman exec memlayer_redis_1 rm -f "/tmp/backup_${DATE}.rdb" 2>/dev/null || true

# Retain only the last 30 backups
ls -t "$BDIR"/dump_*.rdb 2>/dev/null | tail -n +31 | xargs -r rm

COUNT=$(podman exec memlayer_redis_1 redis-cli -a "$REDIS_PASSWORD" DBSIZE 2>/dev/null | tr -d '\r')
SIZE=$(ls -lh "$BDIR/dump_${DATE}.rdb" 2>/dev/null | awk '{print $5}')
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Backed up cache (${COUNT} keys, ${SIZE}) -> $BDIR/dump_${DATE}.rdb"
