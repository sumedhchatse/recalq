#!/usr/bin/env bash
# install.sh — set Recalq up on a (team) server in one go. Re-run to upgrade:
# code is replaced, your .env / client.yaml / data / venv are kept.
#
#   ./install.sh                     # into /opt/recalq, with services
#   ./install.sh --prefix ~/recalq   # somewhere you own
#   ./install.sh --no-services       # files + venv only, start things yourself
#   ./install.sh --venv PATH         # reuse an existing virtualenv
#   ./install.sh --projects DIR      # where the Telegram bot's agent starts (default: $HOME)
#
# Services run as systemd --user units of whoever installs (no root needed):
# recalq-redis, recalq-embed, and recalq-telegram if a bot token is set.
# Steps that do need root are printed at the end, not run.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
PREFIX=/opt/recalq
VENV=""
SERVICES=1
PROJECTS="$HOME"
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2 ;;
    --venv) VENV="$2"; shift 2 ;;
    --no-services) SERVICES=0; shift ;;
    --projects) PROJECTS="$2"; shift 2 ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
PREFIX="$(realpath -m "$PREFIX")"
PROJECTS="$(realpath -m "$PROJECTS")"
say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mxx\033[0m %s\n' "$*" >&2; exit 1; }

# ── 1. prerequisites ─────────────────────────────────────────
command -v python3 >/dev/null || die "python3 not found"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' || die "python3 >= 3.9 needed"
command -v git >/dev/null || die "git not found"
if command -v podman-compose >/dev/null; then COMPOSE="$(command -v podman-compose)"
elif command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then COMPOSE="$(command -v docker) compose"
else die "need podman-compose or docker compose (Recalq keeps its cache in Redis)"; fi
command -v gh >/dev/null || say "note: gh (GitHub CLI) not found — /pr will commit but not open PRs"

# ── 2. install dir ───────────────────────────────────────────
if ! { mkdir -p "$PREFIX" 2>/dev/null && [ -w "$PREFIX" ]; }; then
  die "can't write to $PREFIX. Run this once as root, then re-run ./install.sh:
     sudo mkdir -p $PREFIX && sudo chown $USER $PREFIX
   (or use --prefix somewhere you own)"
fi

# ── 3. code: committed files only — never .env, data, logs or venvs ──
if [ "$SRC" != "$PREFIX" ]; then
  [ -z "$(git -C "$SRC" status --porcelain)" ] || say "note: uncommitted changes in $SRC are NOT installed (only HEAD is)"
  say "installing code into $PREFIX"
  [ -f "$PREFIX/client.yaml" ] && cp -p "$PREFIX/client.yaml" "$PREFIX/.client.yaml.keep"
  git -C "$SRC" archive HEAD | tar -x -C "$PREFIX"
  [ -f "$PREFIX/.client.yaml.keep" ] && mv "$PREFIX/.client.yaml.keep" "$PREFIX/client.yaml"
fi

# ── 4. python env ────────────────────────────────────────────
if [ -n "$VENV" ]; then
  [ -e "$PREFIX/.venv" ] && [ ! -L "$PREFIX/.venv" ] && die "$PREFIX/.venv exists — remove it to use --venv"
  ln -sfn "$(realpath "$VENV")" "$PREFIX/.venv"
elif [ ! -x "$PREFIX/.venv/bin/python3" ]; then
  python3 -c 'import ensurepip' 2>/dev/null || die "python3 can't create virtualenvs. Install it, then re-run:
     Ubuntu/Debian:  sudo apt install python3-venv
     RHEL/Rocky:     sudo dnf install python3-pip"
  say "creating virtualenv"
  python3 -m venv "$PREFIX/.venv"
fi
PY="$PREFIX/.venv/bin/python3"
"$PY" -m pip install -q --upgrade pip
REQ="$PREFIX/requirements.txt"
if ! command -v nvidia-smi >/dev/null; then
  # No GPU: the CPU build of torch is ~200 MB instead of ~3 GB of CUDA
  # libraries that would never be used (embeddings run fine on CPU). Skip
  # the CUDA packages on every run, not just the first — but only install
  # CPU torch when torch is missing, so an existing working torch is kept.
  if ! "$PY" -c "import torch" 2>/dev/null; then
    say "no GPU found — installing CPU-only torch"
    "$PY" -m pip install -q "$(grep '^torch==' "$REQ")" --index-url https://download.pytorch.org/whl/cpu
  fi
  REQ="$(mktemp)"; grep -vE '^(nvidia-|triton)' "$PREFIX/requirements.txt" > "$REQ"
fi
say "installing Python packages"
"$PY" -m pip install -q -r "$REQ"

mkdir -p "$PREFIX/data/redis"   # gitignored, so not in the archive; the Redis bind mount needs it

# ── 5. settings ──────────────────────────────────────────────
if [ ! -f "$PREFIX/.env" ]; then
  cp "$PREFIX/.env.example" "$PREFIX/.env"
  sed -i "s|^REDIS_PASSWORD=.*|REDIS_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')|" "$PREFIX/.env"
  NEW_ENV=1
fi
# The embedding model lives in the install, not each user's ~/.cache —
# Recalq runs with Hugging Face offline, so a teammate's first run would
# otherwise find no model at all.
grep -q '^HF_HOME=' "$PREFIX/.env" || echo "HF_HOME=$PREFIX/models" >> "$PREFIX/.env"
chmod 600 "$PREFIX/.env"
HF_HOME="$(sed -n 's/^HF_HOME=//p' "$PREFIX/.env")"
say "fetching the embedding model into $HF_HOME"
HF_HOME="$HF_HOME" HF_HUB_OFFLINE=0 "$PY" -c \
  "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')" >/dev/null

# ── 6. team access: members of group 'recalq' can run it ─────
if getent group recalq >/dev/null; then
  say "giving group 'recalq' access"
  chgrp -R recalq "$PREFIX"
  chmod -R g+rX,o-rwx "$PREFIX"
  chmod 640 "$PREFIX/.env"  # teammates' own recalq processes call the LLMs, so they need the keys
fi

# ── 7. services ──────────────────────────────────────────────
if [ "$SERVICES" = 1 ]; then
  UNITS="$HOME/.config/systemd/user"
  mkdir -p "$UNITS"
  cat > "$UNITS/recalq-redis.service" <<EOF
[Unit]
Description=Recalq Redis (cache, memory, audit)

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=$PREFIX
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$COMPOSE -p recalq --env-file $PREFIX/.env -f $PREFIX/podman-compose.yml up -d
ExecStop=$COMPOSE -p recalq --env-file $PREFIX/.env -f $PREFIX/podman-compose.yml stop

[Install]
WantedBy=default.target
EOF
  cat > "$UNITS/recalq-embed.service" <<EOF
[Unit]
Description=Recalq embedding service
After=recalq-redis.service

[Service]
WorkingDirectory=$PREFIX
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=$PREFIX/.env
ExecStart=$PREFIX/.venv/bin/uvicorn embedding_service:app --host 127.0.0.1 --port 8081 --workers 1
Restart=on-failure

[Install]
WantedBy=default.target
EOF
  cat > "$UNITS/recalq-telegram.service" <<EOF
[Unit]
Description=Recalq Telegram bot (+ scheduled jobs)
After=recalq-redis.service recalq-embed.service
Requires=recalq-redis.service

[Service]
# The bot's agent starts here (until /cd) — a projects dir, never the
# install itself, so an unqualified /agent can't edit Recalq's own code.
WorkingDirectory=$PROJECTS
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=$PREFIX/.env
ExecStart=$PY $PREFIX/telegram_bot.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  for port in 6379 8081; do
    if ss -ltn | grep -q ":$port "; then
      die "port $port is already in use (another Recalq or Redis?). Stop it, then:
     systemctl --user enable --now recalq-redis recalq-embed recalq-telegram"
    fi
  done
  say "starting services"
  systemctl --user enable --now recalq-redis.service recalq-embed.service
  if grep -q '^TELEGRAM_BOT_TOKEN=.' "$PREFIX/.env"; then
    systemctl --user enable --now recalq-telegram.service
  else
    say "no TELEGRAM_BOT_TOKEN in .env — Telegram bot not started (enable later: systemctl --user enable --now recalq-telegram)"
  fi
fi

# ── done ─────────────────────────────────────────────────────
echo
say "Recalq installed in $PREFIX"
[ "${NEW_ENV:-0}" = 1 ] && echo "   1. Put at least one provider API key in $PREFIX/.env (see client.yaml for which)."
cat <<EOF
   Try it:          cd some/project && $PREFIX/recalq
   Run the tests:   $PREFIX/recalq test

   For a team (as root, once):
     sudo groupadd recalq && sudo usermod -aG recalq <each teammate>
     sudo ln -sf $PREFIX/recalq /usr/local/bin/recalq
     sudo loginctl enable-linger $USER     # keep services running after you log out
   then re-run ./install.sh so the files get group access.
   Everyone in 'recalq' can read the API keys in .env — their recalq calls the LLMs directly.
EOF
