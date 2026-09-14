#!/bin/bash
# Recalq — snapshot Redis cache to timestamped backup
# Run manually anytime, or via cron. Safe to run while system is live.
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

# Force Redis to persist current state to disk
podman exec memlayer_redis_1 redis-cli -a "$REDIS_PASSWORD" BGSAVE >/dev/null 2>&1 || true
sleep 3

# Copy the persisted files
if [ -f "$HOME/memlayer/data/redis/dump.rdb" ]; then
    cp "$HOME/memlayer/data/redis/dump.rdb" "$BDIR/dump_${DATE}.rdb"
fi
if [ -d "$HOME/memlayer/data/redis/appendonlydir" ]; then
    tar -czf "$BDIR/aof_${DATE}.tar.gz" -C "$HOME/memlayer/data/redis" appendonlydir 2>/dev/null || true
fi

# Retain only the last 30 of each
ls -t "$BDIR"/dump_*.rdb    2>/dev/null | tail -n +31 | xargs -r rm
ls -t "$BDIR"/aof_*.tar.gz  2>/dev/null | tail -n +31 | xargs -r rm

COUNT=$(podman exec memlayer_redis_1 redis-cli -a "$REDIS_PASSWORD" DBSIZE 2>/dev/null | tr -d '\r')
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Backed up cache (${COUNT} keys) -> $BDIR/dump_${DATE}.rdb"
