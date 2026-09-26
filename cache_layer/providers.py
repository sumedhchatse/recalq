"""
providers.py — resolves a short alias (from client.yaml) or a raw litellm
model string into an LLM call, in-process. Replaces the old LiteLLM-proxy
hop: any provider litellm supports (OpenAI, Anthropic, Gemini, Groq,
Mistral, Cohere, Bedrock, Azure, Ollama for local models, 100+ others)
works just by setting its API key env var — no proxy server required.
"""
import os
import socket
import logging
import threading
import time
import contextlib
import contextvars
import yaml

# This host's IPv6 route is a black hole (SYNs vanish, no RST) while IPv4
# works fine. Python's http clients (urllib, httpx, ...) take the first
# address getaddrinfo returns and don't retry like curl's Happy Eyeballs
# does, so they hang for minutes on any host with an AAAA record. Force
# IPv4-only resolution process-wide — every module that imports memlayer
# (recalq, telegram_bot.py) picks this up transitively via this import.
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = _ipv4_only_getaddrinfo

# litellm fetches its model pricing table from GitHub on import by default —
# hangs for minutes (or forever) on a restricted network. Use its bundled
# copy instead; we don't use its cost-tracking features anyway.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
import litellm
litellm.suppress_debug_info = True
litellm.set_verbose = False
# litellm logs "LiteLLM completion() model=..." at INFO on its own logger,
# with its own colored handler, for every single call — noisy for an
# interactive CLI/chat reply. Our own audit.record() already captures
# which model/provider served each request, so this is pure duplication;
# drop it everywhere rather than fight litellm's handler wiring.
logging.getLogger("LiteLLM").setLevel(logging.WARNING)

log = logging.getLogger("memlayer")

_CLIENT_YAML = os.path.join(os.path.dirname(os.path.dirname(__file__)), "client.yaml")


def _load_providers():
    if not os.path.exists(_CLIENT_YAML):
        return {}, None, None
    with open(_CLIENT_YAML) as f:
        cfg = yaml.safe_load(f) or {}
    return (cfg.get("providers", {}) or {}, cfg.get("auto_provider"), cfg.get("architect_provider"),
            cfg.get("agent_provider"))


_PROVIDERS, _AUTO, _ARCHITECT, _AGENT = _load_providers()


def default_model() -> str:
    """Alias to use when none is specified."""
    if _AUTO in _PROVIDERS and _PROVIDERS[_AUTO].get("enabled"):
        return _AUTO
    return next((k for k, p in _PROVIDERS.items() if p.get("enabled")), "gemini")


def architect_provider() -> str:
    """Alias to use for the /agent planning pass (see client.yaml's
    `architect_provider`), or None if unset/disabled — architect mode is
    opt-in, off by default so it never silently adds cost."""
    if _ARCHITECT and _ARCHITECT in _PROVIDERS and _PROVIDERS[_ARCHITECT].get("enabled"):
        return _ARCHITECT
    return None


def agent_provider() -> str:
    """Alias /agent runs on (client.yaml `agent_provider`), or None to use
    the chat model — lets chat stay on a free model while code changes go
    to a stronger one."""
    if _AGENT and _AGENT in _PROVIDERS and _PROVIDERS[_AGENT].get("enabled"):
        return _AGENT
    return None


def provider_registry() -> dict:
    """Raw {alias: config} from client.yaml, enabled providers only —
    for callers that need more than the alias->litellm-model mapping
    (cost ranking, display names, audit source normalization)."""
    return {k: p for k, p in _PROVIDERS.items() if p.get("enabled")}


def list_providers():
    """[(alias, litellm_model, ready)] for every enabled provider.
    ready=True if its API key env var is set (or it needs none, e.g. ollama)."""
    out = []
    for alias, p in _PROVIDERS.items():
        if not p.get("enabled", False):
            continue
        key_env = p.get("api_key_env")
        ready = (not key_env) or bool(os.getenv(key_env))
        out.append((alias, f"{p['provider_type']}/{p['model']}", ready))
    return out


def _key_groups() -> dict:
    """{api_key_env: [alias, ...]} for enabled providers — several aliases
    sharing one key (e.g. six NVIDIA-hosted models under one NVIDIA key) is
    normal, not a config error, but worth surfacing: a bad key there breaks
    all of them at once, and testing one live-checks the rest for free."""
    groups = {}
    for alias, p in _PROVIDERS.items():
        if p.get("enabled") and p.get("api_key_env"):
            groups.setdefault(p["api_key_env"], []).append(alias)
    return groups


def provider_status(live: bool = False) -> list:
    """Status per enabled provider: key presence, which other aliases share
    its key (free, no call needed), and (if live=True) a real completion
    call to test whether THIS specific provider/model/key actually works.

    Deliberately tests every provider individually rather than reusing one
    result per shared key: a bad key breaks every provider on it the same
    way, but a dead/decommissioned model breaks only that one provider even
    on a perfectly valid shared key — collapsing by key would hide that.
    Also calls litellm directly instead of chat_completion(), which would
    silently follow fallback_to on failure and report the FALLBACK's
    success as if the provider being tested had worked."""
    groups = _key_groups()
    out = []
    for alias, p in _PROVIDERS.items():
        if not p.get("enabled"):
            continue
        key_env = p.get("api_key_env")
        ready = (not key_env) or bool(os.getenv(key_env))
        shared_with = [a for a in groups.get(key_env, []) if a != alias] if key_env else []
        live_ok, live_error = None, None
        if live and ready:
            model, api_key, api_base, _fallback = _resolve(alias)
            try:
                _call_with_timeout(
                    litellm.completion, REQUEST_TIMEOUT,
                    model=model, messages=[{"role": "user", "content": "hi"}], max_tokens=5,
                    api_key=api_key, api_base=api_base, timeout=REQUEST_TIMEOUT,
                )
                live_ok, live_error = True, None
            except Exception as e:
                live_ok, live_error = False, str(e)[:150]
        out.append({
            "alias": alias, "model": f"{p['provider_type']}/{p['model']}",
            "key_env": key_env, "ready": ready, "shared_with": shared_with,
            "live_ok": live_ok, "live_error": live_error,
        })
    return out


def add_provider(alias: str, provider_type: str, model: str, api_key_env: str = None,
                 api_base: str = None, fallback_to: str = None, display_name: str = None,
                 cost_per_1k_tokens: float = 0.0):
    """Append a new provider block to client.yaml and make it usable
    immediately in this process (no restart) by reloading the registry."""
    global _PROVIDERS, _AUTO, _ARCHITECT, _AGENT
    if alias in _PROVIDERS:
        raise ValueError(f"'{alias}' already exists in client.yaml")

    lines = [f"  {alias}:", "    enabled: true", f"    provider_type: {provider_type}",
             f'    model: "{model}"']
    if api_key_env:
        lines.append(f"    api_key_env: {api_key_env}")
    if api_base:
        lines.append(f'    api_base: "{api_base}"')
    if fallback_to:
        lines.append(f"    fallback_to: {fallback_to}")
    lines.append(f'    display_name: "{display_name or alias}"')
    lines.append(f"    cost_per_1k_tokens: {cost_per_1k_tokens}")
    block = "\n".join(lines) + "\n\n"

    with open(_CLIENT_YAML) as f:
        content = f.read()
    marker = "# ── Which provider is the default choice"
    idx = content.find(marker)
    content = (content[:idx] + block + content[idx:]) if idx != -1 else (content.rstrip() + "\n\n" + block)
    with open(_CLIENT_YAML, "w") as f:
        f.write(content)

    _PROVIDERS, _AUTO, _ARCHITECT, _AGENT = _load_providers()


def save_api_key(env_var: str, value: str):
    """Write/update one KEY=value line in .env and set it in this process's
    environment so a newly-added provider works immediately."""
    env_path = os.path.join(os.path.dirname(_CLIENT_YAML), ".env")
    lines = []
    if os.path.exists(env_path):
        with open(env_path) as f:
            lines = f.readlines()
    for i, line in enumerate(lines):
        if line.startswith(f"{env_var}="):
            lines[i] = f"{env_var}={value}\n"
            break
    else:
        lines.append(f"{env_var}={value}\n")
    with open(env_path, "w") as f:
        f.writelines(lines)
    os.environ[env_var] = value


def _resolve(alias_or_model: str):
    """(litellm_model, api_key, api_base, fallback_alias) for a client.yaml
    alias, or the string as-is for a raw 'provider/model' litellm spec
    (e.g. 'ollama/llama3.1') — litellm reads that provider's own env var."""
    p = _PROVIDERS.get(alias_or_model)
    if p is None:
        return alias_or_model, None, None, None
    key_env = p.get("api_key_env")
    api_key = os.getenv(key_env) if key_env else None
    if key_env and not api_key:
        raise RuntimeError(
            f"provider '{alias_or_model}' needs ${key_env} set — "
            f"export it, or pick another (see: providers)"
        )
    model = f"{p['provider_type']}/{p['model']}"
    return model, api_key, p.get("api_base"), p.get("fallback_to")


REQUEST_TIMEOUT = float(os.getenv("LLM_REQUEST_TIMEOUT", "30"))


def _call_with_timeout(fn, _timeout, *args, **kwargs):
    """Some provider SDKs litellm wraps (e.g. Gemini's) don't reliably honor
    litellm's own `timeout=` kwarg — enforce a hard wall-clock cutoff
    ourselves so a stuck provider can't hang the CLI forever. Uses a daemon
    thread (not ThreadPoolExecutor: its atexit hook blocks process exit
    until every submitted thread finishes, even ones we've given up on)."""
    box = {}
    def target():
        try:
            box["value"] = fn(*args, **kwargs)
        except Exception as e:
            box["error"] = e
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join(_timeout)
    if t.is_alive():
        raise TimeoutError(f"provider did not respond within {_timeout:.0f}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


# Circuit breaker: a provider that just failed (bad key, out of credits,
# overloaded) is skipped for COOLDOWN_SECS instead of eating a full
# REQUEST_TIMEOUT on every request. Kept in Redis once use_redis() is called
# (memlayer does), so the CLI, Telegram bot and MCP server share what they
# learn; falls back to this process's own memory if Redis is unavailable.
COOLDOWN_SECS = float(os.getenv("PROVIDER_COOLDOWN", "300"))
MAX_ATTEMPTS = int(os.getenv("PROVIDER_MAX_ATTEMPTS", "3"))
_cooldown_until = {}  # alias -> unix ts (local fallback)
_store = None
_COOL_KEY = "recalq:cooldown:"

# Set (via free_only()) while serving someone over their budget: only
# providers with cost_per_1k_tokens == 0 may answer.
_FREE_ONLY = contextvars.ContextVar("recalq_free_only", default=False)


def use_redis(r):
    global _store
    _store = r


def _mark_failed(alias):
    _cooldown_until[alias] = time.time() + COOLDOWN_SECS
    if _store is not None:
        try:
            _store.set(_COOL_KEY + alias, 1, ex=max(1, int(COOLDOWN_SECS)))
        except Exception:
            pass


def _mark_ok(alias):
    _cooldown_until.pop(alias, None)
    if _store is not None:
        try:
            _store.delete(_COOL_KEY + alias)
        except Exception:
            pass


def _healthy(alias):
    if _store is not None:
        try:
            return not _store.exists(_COOL_KEY + alias)
        except Exception:
            pass
    return _cooldown_until.get(alias, 0) <= time.time()


def cooldowns() -> dict:
    """{alias: seconds_left} for providers currently being skipped."""
    if _store is not None:
        try:
            return {a: _store.ttl(_COOL_KEY + a) for a in _PROVIDERS if not _healthy(a)}
        except Exception:
            pass
    now = time.time()
    return {a: int(t - now) for a, t in _cooldown_until.items() if t > now}


@contextlib.contextmanager
def free_only(active=True):
    token = _FREE_ONLY.set(bool(active))
    try:
        yield
    finally:
        _FREE_ONLY.reset(token)


# Set (via exact_only()) while benchmarking: no fallback, no skipping — a
# provider must answer itself or fail, or a broken one would "pass" on a
# neighbour's answer.
_EXACT_ONLY = contextvars.ContextVar("recalq_exact_only", default=False)


@contextlib.contextmanager
def exact_only():
    token = _EXACT_ONLY.set(True)
    try:
        yield
    finally:
        _EXACT_ONLY.reset(token)


def set_agent_provider(alias):
    """Point client.yaml's agent_provider at `alias` (adds the line if
    missing) and apply it to this process."""
    global _AGENT
    import re as _re
    with open(_CLIENT_YAML) as f:
        content = f.read()
    line = f"agent_provider: {alias}"
    if _re.search(r"(?m)^agent_provider:.*$", content):
        content = _re.sub(r"(?m)^agent_provider:.*$", line, content, count=1)
    else:
        content = content.rstrip() + "\n\n" + line + "\n"
    with open(_CLIENT_YAML, "w") as f:
        f.write(content)
    _AGENT = alias


def _is_free(alias):
    p = _PROVIDERS.get(alias)
    if p is None:  # raw litellm string: only a local ollama model is known-free
        return alias.startswith("ollama/")
    return (p.get("cost_per_1k_tokens") or 0) == 0


def ranked_providers() -> list:
    """Enabled aliases whose key is set, cheapest first (client.yaml order
    breaks ties), recently-failed ones last."""
    ready = [(a, p) for a, p in _PROVIDERS.items() if p.get("enabled")
             and (not p.get("api_key_env") or os.getenv(p["api_key_env"]))]
    ready.sort(key=lambda ap: (not _healthy(ap[0]), ap[1].get("cost_per_1k_tokens", 999)))
    return [a for a, _ in ready]


def _candidates(alias_or_model):
    """Order to try: the requested model, its fallback_to chain, then every
    other ready provider cheapest-first. Cooled-down ones are dropped unless
    that leaves nothing (then retry them all — maybe the network is back)."""
    if _EXACT_ONLY.get():
        return [alias_or_model]
    chain = []
    cur = alias_or_model
    while cur and cur not in chain:  # fallback chains can cycle (a->b->a)
        chain.append(cur)
        cur = (_PROVIDERS.get(cur) or {}).get("fallback_to")
    order = chain + [a for a in ranked_providers() if a not in chain]
    if _FREE_ONLY.get():
        order = [a for a in order if _is_free(a)]
        if not order:
            raise RuntimeError("budget reached and no free provider is configured — "
                               "ask an admin to raise RECALQ_BUDGET_USD or add a free model")
    healthy = [a for a in order if _healthy(a)]
    return (healthy or order)[:MAX_ATTEMPTS]


def chat_completion(alias_or_model: str, messages: list, max_tokens: int = 800,
                    tools: list = None):
    """Drop-in for the old proxy's `client.chat.completions.create(...)`.
    Same response shape — litellm mirrors the OpenAI SDK's response object,
    so `resp.model` still says which model actually answered after a
    fallback. `tools`, if given, is an OpenAI-style function-calling tool
    list — not every provider/model supports it (litellm raises if the
    target doesn't, which just moves on to the next candidate)."""
    last_err = None
    for alias in _candidates(alias_or_model):
        try:
            model, api_key, api_base, _ = _resolve(alias)
            kwargs = dict(model=model, messages=messages, max_tokens=max_tokens,
                          api_key=api_key, api_base=api_base, timeout=REQUEST_TIMEOUT)
            if tools:
                kwargs["tools"] = tools
            resp = _call_with_timeout(litellm.completion, REQUEST_TIMEOUT, **kwargs)
            _mark_ok(alias)
            return resp
        except Exception as e:
            last_err = e
            _mark_failed(alias)
            log.warning(f"provider '{alias}' failed ({str(e)[:150]}) — skipping it for "
                        f"{COOLDOWN_SECS:.0f}s, trying next")
    raise last_err
