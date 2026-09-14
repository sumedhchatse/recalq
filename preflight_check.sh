#!/usr/bin/env bash
# preflight_check.sh — run this ON THE TARGET VM before installing Recalq.
#
# Purpose: find blockers BEFORE the install visit, not during it. The most
# common killer in a corporate network is outbound HTTPS being blocked or
# proxied — Recalq will install perfectly and then answer nothing.
#
# Safe to run: read-only, changes nothing, needs no sudo (except one check).
#
#   bash preflight_check.sh
#
# Send the output back before the install date.

PASS=0; WARN=0; FAIL=0
ok()   { echo "  [ OK ]  $1"; PASS=$((PASS+1)); }
warn() { echo "  [WARN]  $1"; WARN=$((WARN+1)); }
fail() { echo "  [FAIL]  $1"; FAIL=$((FAIL+1)); }
hdr()  { echo; echo "── $1 ────────────────────────────────────"; }

echo "=========================================="
echo "  Recalq pre-flight check"
echo "  host: $(hostname)   date: $(date '+%Y-%m-%d %H:%M')"
echo "=========================================="

hdr "Operating system"
if [ -f /etc/os-release ]; then
  . /etc/os-release
  echo "  detected: $PRETTY_NAME"
  case "$ID" in
    rhel|rocky|almalinux|centos) ok "RHEL-family — supported" ;;
    ubuntu|debian)               warn "Debian-family — supported, but install steps differ (apt not dnf)" ;;
    *)                           warn "Untested distro — may work, expect surprises" ;;
  esac
else
  fail "cannot identify OS (/etc/os-release missing)"
fi

hdr "Hardware"
CORES=$(nproc 2>/dev/null || echo 0)
RAM_GB=$(awk '/MemTotal/ {printf "%.1f", $2/1024/1024}' /proc/meminfo 2>/dev/null || echo 0)
DISK_GB=$(df -BG --output=avail / 2>/dev/null | tail -1 | tr -dc '0-9' || echo 0)

echo "  cores: $CORES   RAM: ${RAM_GB}GB   free disk: ${DISK_GB}GB"
[ "$CORES" -ge 4 ] && ok "CPU cores sufficient" || warn "only $CORES cores — 4+ recommended"
awk -v r="$RAM_GB" 'BEGIN{exit !(r>=7.5)}' && ok "RAM sufficient" \
  || fail "RAM ${RAM_GB}GB — 8GB minimum (the embedding model needs ~2GB)"
[ "${DISK_GB:-0}" -ge 30 ] && ok "disk sufficient" || warn "only ${DISK_GB}GB free — 40GB+ recommended"

hdr "Python"
if command -v python3 >/dev/null 2>&1; then
  PV=$(python3 -c 'import sys; print("%d.%d"%sys.version_info[:2])')
  echo "  python3: $PV"
  python3 -c 'import sys; sys.exit(0 if sys.version_info>=(3,9) else 1)' \
    && ok "Python 3.9+" || fail "Python $PV — 3.9+ required"
  python3 -c 'import venv' 2>/dev/null && ok "venv module present" \
    || fail "venv missing — install python3-venv"
else
  fail "python3 not found"
fi

hdr "Container runtime"
if command -v podman >/dev/null 2>&1; then
  ok "podman: $(podman --version 2>/dev/null)"
  if podman info >/dev/null 2>&1; then
    ok "podman runs rootless as $(whoami)"
  else
    fail "podman present but 'podman info' fails — rootless not configured"
  fi
else
  fail "podman not installed  (sudo dnf install podman podman-compose -y)"
fi
command -v podman-compose >/dev/null 2>&1 \
  && ok "podman-compose present" \
  || fail "podman-compose not installed"

hdr "SELinux (RHEL gotcha)"
if command -v getenforce >/dev/null 2>&1; then
  SE=$(getenforce)
  echo "  status: $SE"
  [ "$SE" = "Enforcing" ] && warn "Enforcing — container volumes need the :Z flag (handled in compose, just be aware)" \
    || ok "not enforcing"
else
  ok "SELinux not present"
fi

hdr "Outbound HTTPS to LLM providers  ← THE CRITICAL ONE"
echo "  If these fail, Recalq installs fine and answers NOTHING."
for h in generativelanguage.googleapis.com api.groq.com api.anthropic.com integrate.api.nvidia.com; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 "https://$h" 2>/dev/null)
  if [ -n "$code" ] && [ "$code" != "000" ]; then
    ok "$h reachable (HTTP $code)"
  else
    fail "$h UNREACHABLE — blocked, or needs a proxy"
  fi
done

hdr "Proxy configuration"
if [ -n "$http_proxy$https_proxy$HTTP_PROXY$HTTPS_PROXY" ]; then
  warn "proxy env vars are set:"
  echo "        https_proxy=$https_proxy$HTTPS_PROXY"
  echo "        (LiteLLM and pip must be configured for this proxy)"
else
  ok "no proxy env vars — direct egress (if the checks above passed)"
fi

hdr "Package index reachability (for pip install)"
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 https://pypi.org/simple/ 2>/dev/null)
[ "$code" = "200" ] && ok "pypi.org reachable" \
  || fail "pypi.org unreachable — cannot install dependencies (internal mirror needed?)"

hdr "Ports"
for p in 5000 4000 6379 8081; do
  if (command -v ss >/dev/null && ss -ltn 2>/dev/null | grep -q ":$p ") \
     || (command -v netstat >/dev/null && netstat -ltn 2>/dev/null | grep -q ":$p "); then
    fail "port $p already in use"
  else
    ok "port $p free"
  fi
done

hdr "Privileges"
if sudo -n true 2>/dev/null; then
  ok "passwordless sudo available"
elif groups | grep -qE '\bwheel\b|\bsudo\b'; then
  warn "sudo available (will prompt for password)"
else
  warn "no sudo — fine IF podman + python are already installed"
fi

echo
echo "=========================================="
echo "  PASS: $PASS   WARN: $WARN   FAIL: $FAIL"
echo "=========================================="
if [ "$FAIL" -gt 0 ]; then
  echo "  Blockers found. Resolve the [FAIL] items before the install date."
  echo "  The outbound-HTTPS failures matter most — without egress to at"
  echo "  least one LLM provider, Recalq cannot answer anything."
else
  echo "  No blockers. Good to install."
fi
echo
echo "  Also needed before install day:"
echo "    - an API key for at least one provider (Gemini free tier is easiest)"
echo "    - your SOPs/policies as PDF/DOCX/TXT — Recalq is only useful"
echo "      once your own documents are in it"
echo
