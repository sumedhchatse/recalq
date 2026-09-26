#!/bin/bash
# Recalq — restore Redis (cache, memory, knowledge base, audit) from a backup.
# Usage: ./restore_cache.sh dump_20260701_020000.rdb
#
# Redis runs with appendonly (AOF) on, and with AOF files present Redis
# ignores dump.rdb on startup — so just copying the snapshot in (what this
# script used to do) silently restored nothing. Instead: load the snapshot
# once with AOF off, switch AOF on live so Redis rebuilds it from memory,
# then start the normal container again. The old AOF is kept, renamed.
set -euo pipefail
MDIR="$(cd "$(dirname "$0")" && pwd)"
BDIR="${RECALQ_BACKUP_DIR:-$HOME/memlayer-backups/redis}"

if [ -z "${1:-}" ]; then
    echo "Available backups:"; ls -t "$BDIR"/dump_*.rdb 2>/dev/null | head -20
    echo; echo "Usage: $0 <dump_filename.rdb>"; exit 1
fi
BACKUP="$BDIR/$1"
[ -f "$BACKUP" ] || { echo "ERROR: $BACKUP not found"; exit 1; }

set -a; source "$MDIR/.env"; set +a
C="${REDIS_CONTAINER:-$(podman ps --format '{{.Names}}' | grep -m1 redis || true)}"
[ -n "$C" ] || { echo "ERROR: no running Redis container (start Recalq first, or set REDIS_CONTAINER)"; exit 1; }
IMAGE="$(podman inspect -f '{{.ImageName}}' "$C")"
DATA="$(podman inspect -f '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' "$C")"
[ -n "$DATA" ] || { echo "ERROR: can't find $C's /data volume"; exit 1; }

echo "This will REPLACE everything in Redis ($C, data in $DATA) with $1"
if [ "${RECALQ_RESTORE_YES:-}" != 1 ]; then
    read -r -p "Continue? (yes/no): " confirm
    [ "$confirm" = "yes" ] || { echo "Aborted."; exit 0; }
fi

podman stop -t 10 "$C" >/dev/null
STAMP="$(date +%Y%m%d_%H%M%S)"
podman unshare sh -c "[ -d '$DATA/appendonlydir' ] && mv '$DATA/appendonlydir' '$DATA/appendonlydir.before-restore-$STAMP' || true"
podman unshare cp "$BACKUP" "$DATA/dump.rdb"

podman run -d --rm --name recalq-restore --security-opt label=disable -v "$DATA:/data" "$IMAGE" \
    redis-server --appendonly no --requirepass "$REDIS_PASSWORD" >/dev/null
rcli() { podman exec recalq-restore redis-cli -a "$REDIS_PASSWORD" --no-auth-warning "$@"; }
for _ in $(seq 60); do rcli ping 2>/dev/null | grep -q PONG && break; sleep 1; done
rcli config set appendonly yes >/dev/null
for _ in $(seq 120); do
    rcli info persistence | grep -q 'aof_enabled:1' && rcli info persistence | grep -q 'aof_rewrite_in_progress:0' \
        && ! rcli info persistence | grep -q 'aof_rewrite_scheduled:1' && break
    sleep 1
done
COUNT="$(rcli dbsize | tr -d '\r')"
podman stop -t 10 recalq-restore >/dev/null

podman start "$C" >/dev/null
echo "Restore complete: $COUNT keys. Previous AOF kept as appendonlydir.before-restore-$STAMP"
