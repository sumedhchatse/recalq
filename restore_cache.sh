#!/bin/bash
# Recalq — restore Redis cache from a backup snapshot
# Usage: ./restore_cache.sh dump_20260701_020000.rdb
set -e

BDIR="$HOME/memlayer-backups/redis"

if [ -z "$1" ]; then
    echo "Available backups:"
    ls -t "$BDIR"/dump_*.rdb 2>/dev/null | head -20
    echo ""
    echo "Usage: $0 <dump_filename.rdb>"
    exit 1
fi

BACKUP="$BDIR/$1"
if [ ! -f "$BACKUP" ]; then
    echo "ERROR: $BACKUP not found"
    exit 1
fi

echo "This will REPLACE current cache with $1"
read -p "Continue? (yes/no): " confirm
if [ "$confirm" != "yes" ]; then
    echo "Aborted."
    exit 0
fi

# Stop the stack, swap the dump file, restart
cd "$HOME/memlayer"
./stop.sh 2>/dev/null || true
sleep 2

cp "$BACKUP" "$HOME/memlayer/data/redis/dump.rdb"
echo "Restored dump.rdb from $1"

# Note: if AOF is enabled, Redis prefers AOF over RDB on startup.
# To restore from RDB, AOF must be temporarily disabled or the AOF
# rebuilt from this RDB. Handled below.
echo "Rebuilding AOF from restored snapshot..."

./start.sh
sleep 8

# Force AOF rewrite so it matches the restored RDB
set -a; source .env; set +a
podman exec memlayer_redis_1 redis-cli -a "$REDIS_PASSWORD" BGREWRITEAOF >/dev/null 2>&1 || true

COUNT=$(podman exec memlayer_redis_1 redis-cli -a "$REDIS_PASSWORD" DBSIZE 2>/dev/null | tr -d '\r')
echo "Restore complete. Cache now has ${COUNT} keys."
