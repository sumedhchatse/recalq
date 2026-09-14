"""
MemLayer - Universal Compositional Memory Engine
Provider-agnostic AI shared memory middle layer
All data stays local on your machine
"""
import os, json, hashlib, time, re, logging
from pathlib import Path
from datetime import datetime, timedelta
from dotenv import load_dotenv
# Load .env
_env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path, override=True)

# Force offline mode — never phone home to HuggingFace
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import redis
from sentence_transformers import SentenceTransformer, util
import numpy as np
from guardrails import check_query, check_answer
from providers import (chat_completion, default_model, list_providers,
                       provider_status, add_provider, save_api_key)
import audit
import sop_layer

# ── Logging ──────────────────────────────────────────────────
# Full detail always goes to memlayer.log. The console/terminal only gets
# warnings and errors — an interactive CLI or a chat bot shouldn't be
# scrolled past by "cache miss" / "LiteLLM completion()" noise on every
# turn; that detail is still there in the log file if you need to debug.
_file_handler = logging.FileHandler(os.path.expanduser("~/memlayer/memlayer.log"))
_file_handler.setLevel(logging.INFO)
_console_handler = logging.StreamHandler()
_console_handler.setLevel(logging.WARNING)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[_file_handler, _console_handler],
)
log = logging.getLogger("memlayer")

# ── Config ───────────────────────────────────────────────────
REDIS_HOST        = os.getenv("REDIS_HOST")        or "localhost"
REDIS_PORT        = int(os.getenv("REDIS_PORT")    or 6379)
REDIS_PASSWORD    = os.getenv("REDIS_PASSWORD")
SIMILARITY_CUTOFF = float(os.getenv("SIMILARITY_CUTOFF") or "0.78")
CACHE_TTL_SECS    = int(os.getenv("CACHE_TTL"))
CACHE_PREFIX      = "ml:v1:"

# Universal engine config
STALE_DAYS        = 180
MAX_CONTEXT_CHARS = 400
MAX_CONCEPTS      = 5
TOP_K_SIMILAR     = 3

# ── Synonym map ──────────────────────────────────────────────
SYNONYMS = {
    "k8s":"kubernetes","k3s":"kubernetes lightweight",
    "aix":"IBM AIX unix operating system",
    "lpar":"logical partition IBM power systems",
    "hmc":"IBM HMC hardware management console",
    "vios":"virtual I/O server IBM","nim":"network installation manager AIX",
    "vg":"volume group LVM storage","pv":"physical volume LVM",
    "lv":"logical volume LVM","lvm":"logical volume manager linux storage",
    "rhel":"red hat enterprise linux","rhel9":"red hat enterprise linux 9",
    "rhel8":"red hat enterprise linux 8",
    "vm":"virtual machine","os":"operating system","db":"database",
    "api":"application programming interface",
    "cli":"command line interface","ssl":"SSL TLS certificate encryption",
    "tls":"TLS SSL certificate encryption","ssh":"SSH secure shell remote access",
    "ci":"continuous integration","cd":"continuous deployment",
    "cicd":"CI CD continuous integration deployment pipeline",
    "iac":"infrastructure as code","sre":"site reliability engineering",
    "dr":"disaster recovery","ha":"high availability",
    "rpo":"recovery point objective","rto":"recovery time objective",
    "sla":"service level agreement","slo":"service level objective",
    "az":"Microsoft Azure cloud","aks":"Azure Kubernetes Service",
    "eks":"Amazon EKS Kubernetes AWS","gke":"Google Kubernetes Engine",
    "s3":"Amazon S3 object storage","ec2":"Amazon EC2 virtual machine",
    "iam":"identity access management",
    "rbi":"Reserve Bank of India","sebi":"Securities Exchange Board India",
    "nbfc":"non banking financial company","kyc":"know your customer",
    "aml":"anti money laundering","upi":"unified payments interface",
    "neft":"national electronic funds transfer",
    "rtgs":"real time gross settlement",
}

def normalise_query(query: str) -> str:
    words = query.lower().split()
    expanded = []
    for word in words:
        clean = word.strip("?.,!:;")
        expanded.append(SYNONYMS.get(clean, word))
    return " ".join(expanded)

# ── Connections ──────────────────────────────────────────────
log.info("Connecting to Redis...")
r = redis.Redis(
    host=REDIS_HOST, port=REDIS_PORT,
    password=REDIS_PASSWORD, db=0,
    decode_responses=True, socket_connect_timeout=5
)
r.ping()
log.info("Redis connected ✓")

log.info("Loading embedding model (first run downloads ~90MB)...")
_local_embedder = SentenceTransformer("all-MiniLM-L6-v2")

# ── Embedding service client (with local fallback) ────────────
import requests as _requests

EMBED_SERVICE_URL = os.getenv("EMBED_SERVICE_URL", "http://localhost:8081")
_EMBED_SERVICE_ENABLED = os.getenv("USE_EMBED_SERVICE", "1") == "1"

class _EmbedderClient:
    """
    Drop-in replacement for the sentence-transformers model.
    Calls the embedding service over HTTP (which batches across users),
    falling back to the in-process model if the service is unreachable.
    Supports the same .encode(text_or_list, convert_to_tensor=..., convert_to_numpy=...)
    signature used across memlayer.py.
    """
    def __init__(self, service_url, local_model):
        self.service_url = service_url.rstrip("/")
        self.local = local_model
        self._service_ok = _EMBED_SERVICE_ENABLED

    def encode(self, texts, convert_to_tensor=False, convert_to_numpy=False, **kwargs):
        single = isinstance(texts, str)
        batch = [texts] if single else list(texts)

        # Try the service first (unless disabled or previously failed this call)
        if self._service_ok:
            try:
                resp = _requests.post(
                    f"{self.service_url}/embed",
                    json={"inputs": batch},
                    timeout=15,
                )
                resp.raise_for_status()
                vecs = resp.json()["embeddings"]
                arr = np.asarray(vecs, dtype=np.float32)
                result = arr[0] if single else arr
                if convert_to_tensor:
                    # Callers using util.cos_sim need a tensor — fall back locally
                    # only for that specific need (rare on the hot path now).
                    import torch
                    return torch.tensor(result)
                return result
            except Exception as e:
                log.warning(f"Embed service unavailable ({e}); using local model")
                # Don't permanently disable — just fall through for THIS call

        # Local fallback
        return self.local.encode(
            texts,
            convert_to_tensor=convert_to_tensor,
            convert_to_numpy=convert_to_numpy,
            **kwargs
        )

embedder = _EmbedderClient(EMBED_SERVICE_URL, _local_embedder)
log.info(f"Embedder using service at {EMBED_SERVICE_URL} (fallback: local model)")
log.info("Embedding model ready ✓")

log.info("LLM providers resolved from client.yaml + litellm ✓")

# ── Namespace helpers (Step 3) ────────────────────────────────
DEFAULT_NAMESPACE = "default"

def _ns(namespace: str = None) -> str:
    """Normalise a namespace string. None/empty -> default namespace."""
    if not namespace or not namespace.strip():
        return DEFAULT_NAMESPACE
    return namespace.strip().lower().replace(" ", "-")

# ── Cache helpers ────────────────────────────────────────────
def _cache_key(entry_id: str, namespace: str = None) -> str:
    return f"{CACHE_PREFIX}{_ns(namespace)}:{entry_id}"

def _meta_key(namespace: str = None) -> str:
    return f"{CACHE_PREFIX}{_ns(namespace)}:__meta__"

def _save_stats(stats: dict, namespace: str = None):
    r.set(_meta_key(namespace), json.dumps(stats))

def get_stats(namespace: str = None) -> dict:
    raw = r.get(_meta_key(namespace))
    if raw:
        return json.loads(raw)
    return {"total_queries":0,"cache_hits":0,"tokens_saved_est":0,"llm_calls":0}

def list_cache_entries(namespace: str = None, all_namespaces: bool = False) -> list:
    """
    namespace=None        -> uses DEFAULT_NAMESPACE
    all_namespaces=True   -> ignores namespace filter, returns everything (admin use)
    """
    if all_namespaces:
        pattern = f"{CACHE_PREFIX}*"
    else:
        pattern = f"{CACHE_PREFIX}{_ns(namespace)}:*"

    keys = r.keys(pattern)
    entries = []
    for k in keys:
        if "__meta__" in k or ":exact:" in k or "__audit__" in k:
            continue
        try:
            raw = r.get(k)
        except Exception:
            # Skip non-string keys (lists, hashes, etc.)
            continue
        if raw:
            try:
                entries.append(json.loads(raw))
            except Exception:
                pass
    return sorted(entries, key=lambda x: x.get("hits", 0), reverse=True)


# ── Context-dependent query detection ─────────────────────────────
# A follow-up like "how do I install that" has no standalone meaning: "that"
# is defined by the previous turn. Two unrelated conversations produce the
# identical string, so caching it poisons the cache across conversations and
# namespaces. The safety family can't catch this (the strings match exactly),
# so it must be excluded from caching entirely.
_ANAPHORA_RE = re.compile(
    r"\b(that|this|it|these|those|them|they|its|their|him|her|he|she)\b", re.I)

_FILLER_WORDS = {"more", "ok", "okay", "yes", "no", "sure", "next",
                 "again", "continue", "go", "then", "and", "also", "please"}
_CTX_STOPWORDS = {
    "how", "do", "does", "did", "i", "we", "you", "what", "why", "when",
    "where", "which", "who", "is", "are", "was", "were", "be", "been",
    "the", "a", "an", "to", "on", "in", "at", "of", "for", "and", "or",
    "can", "could", "should", "would", "will", "shall", "may", "might",
    "please", "tell", "me", "about", "more", "again", "then", "so", "now",
}


# Namespaces with this prefix never touch the shared commons cache. Agents act
# on answers, so an agent must not read poisoned commons entries nor write ones
# other callers could execute. Strict per-agent isolation.
_NO_COMMONS_PREFIXES = ("agent_", "test_", "test")

def _shares_commons(namespace):
    ns = _ns(namespace)
    if ns == "commons":
        return False
    return not any(ns.startswith(pfx) for pfx in _NO_COMMONS_PREFIXES)

def save_to_cache(query: str, answer: str, model_used: str, tokens_used: int = 0, namespace: str = None, query_had_pii: bool = False):
    # Never cache a query that has no meaning without its conversation —
    # the identical string from a different conversation would hit it at 1.0.
    if is_context_dependent(query):
        log.info(f"NOT CACHED (context-dependent): '{query[:60]}'")
        return
    # Scrub any PII that the LLM might have echoed/hallucinated
    answer_check = check_answer(answer)
    if not answer_check["safe"]:
        answer = answer_check["clean_text"]
    entry_id = hashlib.sha256(query.encode()).hexdigest()[:20]
    key = _cache_key(entry_id, namespace)
    entry = {
        "id":          entry_id,
        "query":       query,
        "answer":      answer,
        "model":       model_used,
        "timestamp":   time.time(),
        "hits":        0,
        "tokens_used": tokens_used,
        "last_hit":    None,
        "thumbs_up":   0,
        "thumbs_down": 0,
        "flagged":     False,
        "intent":      detect_intent(query),
        "namespace":   _ns(namespace),
        "embedding":   embedder.encode(normalise_query(query.lower()),
                                       convert_to_tensor=False).tolist()
    }
    r.set(key, json.dumps(entry), ex=CACHE_TTL_SECS)
    log.info(f"Cached [{_ns(namespace)}]: '{query[:60]}'")
    register_exact_match(query, entry_id, namespace)

    # Also save to shared commons cache (org-wide reuse), unless this
    # query originally contained PII (even though it's now redacted,
    # we keep PII-originated queries namespace-private as a precaution)
    if _shares_commons(namespace) and not query_had_pii:
        commons_key = _cache_key(entry_id, "commons")
        r.set(commons_key, json.dumps(entry), ex=CACHE_TTL_SECS)
        register_exact_match(query, entry_id, "commons")
        log.info(f"Also cached to commons: '{query[:60]}'")

    return entry_id

# ── Exact-match short-circuit ──────────────────────────────
def _exact_key(query: str, namespace: str = None) -> str:
    """Hash of normalized query for O(1) exact lookup, scoped to namespace."""
    norm = normalise_query(query.lower().strip())
    return f"{CACHE_PREFIX}{_ns(namespace)}:exact:" + hashlib.sha256(norm.encode()).hexdigest()[:20]

def find_exact_match(query: str, namespace: str = None):
    if is_context_dependent(query):
        log.info(f"CACHE BYPASS (context-dependent): '{query[:60]}'")
        return None, 0.0
    """O(1) Redis GET — no embedding model touched. Scoped to namespace."""
    key = _exact_key(query, namespace)
    entry_id = r.get(key)
    if not entry_id:
        return None, 0.0

    raw = r.get(_cache_key(entry_id, namespace))
    if not raw:
        r.delete(key)
        return None, 0.0

    try:
        entry = json.loads(raw)
    except Exception:
        return None, 0.0

    entry["hits"] = entry.get("hits", 0) + 1
    entry["last_hit"] = time.time()
    r.set(_cache_key(entry["id"], namespace), json.dumps(entry), ex=CACHE_TTL_SECS)
    log.info(f"EXACT HIT [{_ns(namespace)}] | query='{query[:50]}'")
    return entry, 1.0

def register_exact_match(query: str, entry_id: str, namespace: str = None):
    """Call this whenever save_to_cache() stores a new entry."""
    key = _exact_key(query, namespace)
    r.set(key, entry_id, ex=CACHE_TTL_SECS)
def _entry_embedding(entry):
    """Return stored embedding as np array, or compute+persist if missing (old entries)."""
    emb = entry.get("embedding")
    if emb is not None:
        return np.asarray(emb, dtype=np.float32)
    # Backfill for pre-vector-index entries
    vec = embedder.encode(normalise_query(entry.get("query","").lower()),
                          convert_to_tensor=False)
    entry["embedding"] = vec.tolist()
    try:
        ns = entry.get("namespace")
        r.set(_cache_key(entry["id"], ns), json.dumps(entry), ex=CACHE_TTL_SECS)
    except Exception:
        pass
    return np.asarray(vec, dtype=np.float32)


def _batch_cosine(q_vec, entries):
    """Single vectorized cosine of q_vec against all entries' stored embeddings.
    Returns list of (entry, score) in the same order as entries."""
    if not entries:
        return []
    mat = np.vstack([_entry_embedding(e) for e in entries])          # (n, dim)
    q = np.asarray(q_vec, dtype=np.float32)                          # (dim,)
    qn = q / (np.linalg.norm(q) + 1e-9)
    mn = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
    scores = mn @ qn                                                 # (n,)
    return [(entries[i], float(scores[i])) for i in range(len(entries))]



# ── Polarity / negation awareness (the wedge) ────────────────
# Antonym pairs where a match would give the OPPOSITE (wrong) answer.
_POLARITY_PAIRS = [
    ("enable", "disable"), ("enabled", "disabled"), ("allow", "deny"),
    ("allow", "block"), ("increase", "decrease"), ("add", "remove"),
    ("start", "stop"), ("open", "close"), ("mount", "unmount"),
    ("grant", "revoke"), ("activate", "deactivate"), ("connect", "disconnect"),
    ("install", "uninstall"), ("lock", "unlock"), ("show", "hide"),
    ("expand", "shrink"), ("attach", "detach"), ("bind", "unbind"),
    ("up", "down"), ("on", "off"), ("accept", "reject"), ("include", "exclude"),
]
# Standalone negation tokens
_NEG_TOKENS = {"not", "no", "never", "without", "cannot", "can't", "don't", "avoid", "prevent", "stop"}

def _polarity_signature(tokens):
    """Return (set of polarity-side markers, negation present)."""
    tset = set(tokens)
    sides = set()
    for a, b in _POLARITY_PAIRS:
        if a in tset: sides.add(("PAIR", a, b, "a"))
        if b in tset: sides.add(("PAIR", a, b, "b"))
    neg = bool(tset & _NEG_TOKENS)
    return sides, neg

def _polarity_conflict(q_tokens, c_tokens):
    """
    True if the two token lists take OPPOSITE polarity on a shared action.
    e.g. 'enable ssh' vs 'disable ssh' -> conflict.
    """
    q_sides, q_neg = _polarity_signature(q_tokens)
    c_sides, c_neg = _polarity_signature(c_tokens)
    # Antonym-pair conflict: one uses side a, other uses side b of same pair
    for (_, a, b, side_q) in q_sides:
        for (_, a2, b2, side_c) in c_sides:
            if a == a2 and b == b2 and side_q != side_c:
                return True
    # Negation asymmetry: one query negates, the other doesn't (and they're otherwise similar)
    if q_neg != c_neg:
        return True
    return False




import re as _re

def _extract_numbers(tokens):
    """Numbers with optional units, e.g. '5gb', '22', '8022', '3.11'."""
    nums = set()
    for t in tokens:
        m = _re.match(r'^(\d+(?:\.\d+)?)([a-z%]*)$', t)
        if m:
            nums.add((m.group(1), m.group(2)))
    return nums

def _number_conflict(q_tokens, c_tokens):
    """
    True if both queries contain numbers but they DIFFER.
    e.g. 'increase to 5gb' vs 'increase to 50gb' -> conflict.
    Only fires when BOTH have numbers (else it's not a numeric question).
    """
    qn = _extract_numbers(q_tokens)
    cn = _extract_numbers(c_tokens)
    if not qn or not cn:
        return False
    # If the numeric values sets differ at all, it's a conflict.
    q_vals = {v for v, _ in qn}
    c_vals = {v for v, _ in cn}
    if q_vals and c_vals and q_vals != c_vals:
        return True
    return False

# Version tokens like 'v1','v2','python2','python3','3.11'
_VERSION_RE = _re.compile(r'^(v\d+|\d+\.\d+|python\d+|py\d+|node\d+)$')
def _version_tokens(tokens):
    return {t for t in tokens if _VERSION_RE.match(t)}

def _version_conflict(q_tokens, c_tokens):
    qv = _version_tokens(q_tokens)
    cv = _version_tokens(c_tokens)
    if qv and cv and qv != cv:
        return True
    return False

_TEMPORAL = {"before": "after", "after": "before", "pre": "post", "post": "pre"}
def _temporal_conflict(q_tokens, c_tokens):
    qset, cset = set(q_tokens), set(c_tokens)
    for word, opposite in _TEMPORAL.items():
        if word in qset and opposite in cset:
            return True
    return False


# Common places/entities where a swap means a DIFFERENT answer.
# Not exhaustive — the distinct-proper-noun heuristic covers the rest.
_KNOWN_ENTITIES = {
    # countries
    "india","nepal","pakistan","china","usa","america","uk","britain","germany",
    "france","japan","canada","australia","singapore","dubai","uae","bangladesh",
    "srilanka","bhutan","russia","brazil","italy","spain","mexico","indonesia",
    # common tech entities that change the answer
    "nginx","apache","mysql","postgres","postgresql","redis","mongodb","kafka",
    "docker","kubernetes","podman","aws","azure","gcp","oracle","windows","linux",
    "ubuntu","centos","rhel","debian","fedora","python2","python3",
}

def _entities_in(tokens):
    return {t for t in tokens if t in _KNOWN_ENTITIES}

def _entity_conflict(q_tokens, c_tokens):
    """
    True if each query references a DIFFERENT known entity that the other
    does not — a swap (india vs nepal, nginx vs apache) implying different answers.
    """
    qe = _entities_in(q_tokens)
    ce = _entities_in(c_tokens)
    if not qe or not ce:
        return False
    # If neither shares any entity, and each has its own -> swap.
    if qe != ce and not (qe & ce):
        return True
    # If they share some but each also has a distinct one -> still a swap.
    only_q = qe - ce
    only_c = ce - qe
    if only_q and only_c:
        return True
    return False

def _entity_omission(q_tokens, c_tokens):
    """
    True if the QUERY names a known entity that the CACHED query never
    mentions — i.e. the cached question is strictly more general.

        q='build linux vm in azure' {linux,azure}
        c='build vm in azure'       {azure}        -> only_q={linux} -> True

    Asymmetric on purpose: the reverse (query generic, cached more specific)
    is allowed, because a specific answer still answers a general question.

        q='restart nginx'           {nginx}
        c='restart nginx on ubuntu' {nginx,ubuntu} -> only_q={}      -> False
    """
    qe = _entities_in(q_tokens)
    ce = _entities_in(c_tokens)
    only_q = qe - ce
    only_c = ce - qe
    # Both sides distinct -> that's a swap; _entity_conflict owns it.
    return bool(only_q) and not only_c


# Role pairs: same topic, DIFFERENT position in an architecture -> different
# config. 'master' and 'node' are not antonyms and not different entities, so
# polarity/entity checks miss them; but serving one's answer for the other is
# wrong. Each inner set is a group of mutually-exclusive roles.
_ROLE_GROUPS = [
    {"master", "node", "worker", "agent"},
    {"primary", "replica", "secondary", "standby"},
    {"leader", "follower"},
    {"source", "target", "destination"},
    {"client", "server"},
    {"active", "passive"},
    {"controller", "managed"},
    {"publisher", "subscriber"},
]

def _roles_in(tokens):
    """Map each token to the role-group(s) it belongs to. Returns
    set of (group_index, role) for roles present."""
    out = set()
    tset = set(tokens)
    for gi, group in enumerate(_ROLE_GROUPS):
        for role in group:
            if role in tset:
                out.add((gi, role))
    return out

def _role_conflict(q_tokens, c_tokens):
    """
    True if the CACHED query names a role the QUERY does not share, within the
    same role group — so the cached answer is about a different position in the
    architecture than what was asked.

        q='configure nginx on the two node vms'  -> group0: {node}
        c='configure nginx on the master node'   -> group0: {master, node}
        cs - qs = {master}  ->  cached is about the master; query asked nodes
        -> conflict (this is the real bug: master config served for a node stage)

    Asymmetric, like entity omission: a MORE-specific query served a general
    cached answer is allowed; a query served an answer about a role it never
    named is not.
    """
    qr = _roles_in(q_tokens)
    cr = _roles_in(c_tokens)
    if not qr or not cr:
        return False
    from collections import defaultdict
    q_by_g = defaultdict(set)
    c_by_g = defaultdict(set)
    for gi, role in qr:
        q_by_g[gi].add(role)
    for gi, role in cr:
        c_by_g[gi].add(role)
    for gi in set(q_by_g) & set(c_by_g):
        qs, cs = q_by_g[gi], c_by_g[gi]
        if qs != cs and (cs - qs):
            return True
    return False

def _semantic_safety_conflict(q_tokens, c_tokens):
    """Combined check. Returns (conflict_bool, reason_str)."""
    if _polarity_conflict(q_tokens, c_tokens):
        return True, "polarity"
    if _number_conflict(q_tokens, c_tokens):
        return True, "number/quantity"
    if _version_conflict(q_tokens, c_tokens):
        return True, "version"
    if _temporal_conflict(q_tokens, c_tokens):
        return True, "temporal"
    if _entity_omission(q_tokens, c_tokens):
        return True, "entity omission (query is more specific than the cached one)"
    if _entity_conflict(q_tokens, c_tokens):
        return True, "entity/location"
    if _role_conflict(q_tokens, c_tokens):
        return True, "role (e.g. master vs node — same topic, different position)"
    return False, ""

def find_semantic_match(query: str, namespace: str = None):
    if is_context_dependent(query):
        return None, 0.0
    """
    Two-layer cache validation:
    Layer 1 — semantic similarity (existing)
    Layer 2 — concept overlap check (new)
    Both must pass for a cache hit.
    """
    entries = list_cache_entries(namespace)
    if not entries:
        return None, 0.0

    q_norm     = normalise_query(query.lower())
    q_vec      = embedder.encode(q_norm, convert_to_tensor=False)
    q_concepts = set([
        w for w in q_norm.split()
        if w not in STOP_WORDS and len(w) > 2
    ])
    # Batch-score all entries in ONE vectorized pass (no per-entry encode)
    scored = _batch_cosine(q_vec, [e for e in entries if "query" in e])
    best_score  = 0.0
    best_entry  = None
    best_overlap = 0.0
    # Compute the query's intent ONCE, using the fast keyword classifier
    # (NEVER an LLM call — this runs on every cache lookup, hot path)
    new_intent = _detect_intent_keywords(query)
    for entry, score in scored:
        # Layer 1 — semantic similarity (precomputed)
        c_norm = normalise_query(entry["query"].lower())
        if score < SIMILARITY_CUTOFF:
            continue

        # Layer 2 — concept overlap
        # At least ONE key concept from new query must exist
        # in cached query or cached answer
        c_concepts = set([
            w for w in c_norm.split()
            if w not in STOP_WORDS and len(w) > 2
        ])
        answer_words = set(
            entry.get("answer","").lower().split()
        )

        # Overlap between query concepts and cached query concepts
        query_overlap = len(q_concepts & c_concepts)

        # Also check if key concepts appear in the cached answer
        answer_overlap = len(q_concepts & answer_words)

        # Total overlap score — normalised
                # ── Strict two-sided concept matching ────────────
        # How much of NEW query is covered by cache?
        q_coverage = query_overlap / max(len(q_concepts), 1)
        # How much of CACHE query is covered by new query?
        c_coverage = query_overlap / max(len(c_concepts), 1)
        # Harmonic mean — both sides must match, not just one
        if q_coverage + c_coverage == 0:
            overlap = 0.0
        else:
            overlap = 2 * (q_coverage * c_coverage) / (q_coverage + c_coverage)

        # Hard reject — if major concepts from new query
        # are absent from BOTH cached query and cached answer
        major_concepts = [w for w in q_concepts if len(w) > 3]
        missing_major  = [
            c for c in major_concepts
            if c not in c_concepts and c not in answer_words
        ]
        missing_ratio = len(missing_major) / max(len(major_concepts), 1)

        log.info(
            f"  Cache check: score={score:.3f} overlap={overlap:.2f} "
            f"missing={missing_ratio:.2f} "
            f"| q='{query[:40]}' vs cached='{entry['query'][:40]}'"
        )

        # Reject if more than 50% of key concepts are missing
        if missing_ratio > 0.5:
            log.info(f"  HARD REJECT: missing {missing_major}")
            continue
        # ── Polarity / negation check (the wedge) ──
        # Reject high-similarity hits that flip meaning: enable vs disable,
        # increase vs decrease, "with" vs "without". No other cache does this.
        q_tokens_full = q_norm.split()
        c_tokens_full = c_norm.split()
        _conflict, _reason = _semantic_safety_conflict(q_tokens_full, c_tokens_full)
        if _conflict:
            log.info(f"  SAFETY REJECT ({_reason}): meaning differs | q='{query[:40]}' vs '{entry['query'][:40]}'")
            continue
        # Layer 3 — Intent must match
        # Layer 3 — Intent must match
        # new_intent computed ONCE before the loop (see above) — never inside it
        # For cached entries missing intent, use the FAST keyword version
        # (never an LLM call inside the hot cache-lookup path)
        cached_intent = entry.get("intent") or _detect_intent_keywords(entry.get("query", ""))
        INTENT_CONFLICTS = {
            "dependency": ["comparison", "procedural", "definition"],
            "comparison": ["dependency", "procedural", "definition"],
            "procedural": ["relational", "dependency", "comparison", "definition"],
            "definition": ["dependency", "comparison", "procedural", "relational"],
        }
        blocked = INTENT_CONFLICTS.get(new_intent, [])
        if cached_intent in blocked:
            log.info(
                f"  INTENT MISMATCH: new={new_intent} "
                f"cached={cached_intent} → skip"
            )
            continue
        # Must have BOTH high similarity AND concept overlap
        if score > best_score and overlap >= 0.5:
            best_score   = score
            best_entry   = entry
            best_overlap = overlap

    if best_entry:
        best_entry["hits"]     = best_entry.get("hits", 0) + 1
        best_entry["last_hit"] = time.time()
        r.set(
            _cache_key(best_entry["id"], namespace),
            json.dumps(best_entry),
            ex=CACHE_TTL_SECS
        )
        log.info(
            f"CACHE HIT | score={best_score:.3f} "
            f"overlap={best_overlap:.2f} | saved"
        )
        return best_entry, best_score

    log.info(
        f"CACHE MISS | best_score={best_score:.3f} "
        f"| no sufficient concept overlap"
    )
    return None, best_score

# ── Universal engine ─────────────────────────────────────────

STOP_WORDS = {
    "what","is","are","the","a","an","and","or","but","in","on","at",
    "to","for","of","with","by","from","how","why","when","where","who",
    "which","this","that","these","those","do","does","did","can","could",
    "would","should","will","tell","me","about","explain","describe",
    "give","show","its","their","our","my","your","between","relation",
    "relationship","common","similar","difference","compare","vs","use",
    "used","using","also","not","just","it","its","they","them","we",
    "he","she","work","works","thing","things","way","ways","type",
    "types","kind","role","please","thanks","help","need","want","like"
}




# ── Intent detection via LLM (accurate) ──────────────────────
_intent_cache = {}   # local memory cache — avoid repeat LLM calls

def detect_intent(query: str) -> str:
    """
    Use LLM to detect query intent accurately.
    Falls back to keyword matching if LLM unavailable.
    Results cached in memory to avoid repeat calls.
    """
    q = query.lower().strip()

    # Check local memory cache first
    if q in _intent_cache:
        return _intent_cache[q]

    # Try LLM classification — costs ~50 tokens
    try:
        response = chat_completion(
            default_model(),   # cheap/fast default for classification
            messages=[{
                "role": "user",
                "content": f"""Classify this query into exactly ONE intent category.

Query: "{query}"

Categories:
- comparison   : asks about differences, which is better, contrast
- similarity   : asks what is common, shared, same, alike between things  
- dependency   : asks how dependent, relies on, requires, can work without
- relational   : asks how things relate, interact, work together, connect
- procedural   : asks how to do something, steps, setup, configure, fix
- definition   : asks what something is, explain, describe, meaning
- general      : anything else

Reply with ONLY the category word, nothing else."""
            }],
            max_tokens=10
        )
        intent = response.choices[0].message.content.strip().lower()

        # Validate it's a known intent
        valid = {"comparison","similarity","dependency",
                 "relational","procedural","definition","general"}
        if intent not in valid:
            intent = "general"

        # Cache result in memory
        _intent_cache[q] = intent
        log.info(f"Intent detected: {intent} | '{query[:50]}'")
        return intent

    except Exception as e:
        log.warning(f"Intent LLM failed ({e}) — using keyword fallback")
        return _detect_intent_keywords(query)


def _detect_intent_keywords(query: str) -> str:
    """Keyword fallback — used only if LLM unavailable."""
    q = query.lower()
    if any(w in q for w in ["dependent","dependency","relies","requires","without"]):
        return "dependency"
    if any(w in q for w in ["common","similar","same","alike","overlap","share"]):
        return "similarity"
    if any(w in q for w in ["compare","vs","versus","difference","differ","contrast"]):
        return "comparison"
    if any(w in q for w in ["relation","between","interact","work together","connect"]):
        return "relational"
    if any(w in q for w in ["how to","steps","setup","install","configure","fix","debug"]):
        return "procedural"
    if any(w in q for w in ["what is","what are","define","explain","describe"]):
        return "definition"
    return "general"

def extract_concepts_universal(query: str) -> list:
    q = normalise_query(query.lower())
    tokens = re.findall(r'\b[a-z][a-z0-9_\-]*\b', q)
    meaningful = [t for t in tokens if t not in STOP_WORDS and len(t) > 2]
    concepts = []
    for i in range(len(meaningful) - 1):
        concepts.append(f"{meaningful[i]} {meaningful[i+1]}")
    concepts.extend(meaningful)
    seen = set()
    unique = []
    for c in concepts:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    return unique[:MAX_CONCEPTS]

def is_stale(entry: dict) -> bool:
    age = datetime.now() - datetime.fromtimestamp(entry.get("timestamp", 0))
    return age > timedelta(days=STALE_DAYS)

def age_label(entry: dict) -> str:
    days = (datetime.now() - datetime.fromtimestamp(entry.get("timestamp", 0))).days
    if days == 0:  return "today"
    if days == 1:  return "yesterday"
    if days < 7:   return f"{days}d ago"
    if days < 30:  return f"{days//7}w ago"
    if days < 365: return f"{days//30}mo ago"
    return f"{days//365}y ago"

def find_top_k_similar(query: str, k: int = TOP_K_SIMILAR, namespace: str = None):
    entries = list_cache_entries(namespace)
    if not entries:
        return []
    q_vec = embedder.encode(normalise_query(query), convert_to_tensor=False)
    all_scored = _batch_cosine(q_vec, [e for e in entries if "query" in e])
    scored = [(e, s) for e, s in all_scored if s >= 0.55]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]

def find_top_k_similar_batch(concepts: list, k: int = TOP_K_SIMILAR, namespace: str = None):
    """
    Embed ALL concepts in ONE batched pass, then score each against the
    cache. Returns {concept: [(entry, score), ...]}. Replaces N separate
    find_top_k_similar calls with a single embedding batch.
    """
    entries = list_cache_entries(namespace)
    if not entries or not concepts:
        return {concept: [] for concept in concepts}

    valid_entries = [e for e in entries if "query" in e]
    if not valid_entries:
        return {concept: [] for concept in concepts}

    # One batched embedding call for ALL concepts at once
    concept_vecs = embedder.encode(
        [normalise_query(c) for c in concepts],
        convert_to_tensor=False
    )
    # Pre-stack cache embeddings once (reused across all concepts)
    entry_mat = np.vstack([_entry_embedding(e) for e in valid_entries])
    entry_norms = entry_mat / (np.linalg.norm(entry_mat, axis=1, keepdims=True) + 1e-9)

    out = {}
    for i, concept in enumerate(concepts):
        q = np.asarray(concept_vecs[i], dtype=np.float32)
        qn = q / (np.linalg.norm(q) + 1e-9)
        scores = entry_norms @ qn
        scored = [(valid_entries[j], float(scores[j]))
                  for j in range(len(valid_entries)) if scores[j] >= 0.55]
        scored.sort(key=lambda x: x[1], reverse=True)
        out[concept] = scored[:k]
    return out


def ask_universal(query: str, model: str = "gemini", history: list = None, namespace: str = None, query_had_pii: bool = False, user: str = None, intent: str = None) -> dict:
    _audit_start = time.time()
    concepts = extract_concepts_universal(query)
    # Intent is passed in from ask() — only detect if called directly
    if intent is None:
        intent = detect_intent(query)
    log.info(f"[UNIVERSAL] intent={intent} | concepts={concepts}")

    all_relevant = {}
    covered      = set()
    # Batch: embed all concepts in ONE pass, score against cache once
    concept_hits = find_top_k_similar_batch(concepts, namespace=namespace)
    for concept in concepts:
        hits = concept_hits.get(concept, [])
        fresh = []
        for entry, score in hits:
            eid = entry.get("id")
            if eid not in covered:
                covered.add(eid)
                fresh.append((entry, score))
        if fresh:
            all_relevant[concept] = fresh

    flat_relevant = []
    seen_ids = set()
    for concept, hits in all_relevant.items():
        for entry, score in hits:
            eid = entry.get("id")
            if eid not in seen_ids:
                seen_ids.add(eid)
                flat_relevant.append({
                    "concept": concept,
                    "entry":   entry,
                    "score":   score,
                    "stale":   is_stale(entry),
                    "age":     age_label(entry)
                })
    flat_relevant.sort(key=lambda x: x["score"], reverse=True)

    missing_concepts = [c for c in concepts if c not in all_relevant]

    context_lines = []
    for item in flat_relevant[:6]:
        e     = item["entry"]
        stale = " ⚠ MAY BE OUTDATED" if item["stale"] else ""
        summary = e["answer"][:MAX_CONTEXT_CHARS].replace("\n", " ")
        context_lines.append(
            f"[CONTEXT '{e['query']}' · {item['age']}{stale}]\n{summary}..."
        )

    context_section = (
        "RELEVANT KNOWLEDGE FROM ORGANISATIONAL MEMORY:\n" +
        "\n\n".join(context_lines)
    ) if context_lines else "No prior knowledge found in cache."

    missing_note = (
        f"\nNo cached info for: {', '.join(missing_concepts)}. Use your training knowledge."
    ) if missing_concepts else ""

    if intent == "relational":
        task = ("Explain RELATIONSHIPS, PATTERNS and CONNECTIONS between these concepts. "
                "Do NOT just define each separately. Show how they interact and what they share.")
    elif intent == "procedural":
        task = "Give practical STEPS. Use context as background but provide current actionable guidance."
    else:
        task = "Answer clearly and concisely using context as background knowledge."

    prompt = f"""You are an expert assistant with access to organisational memory.

{context_section}
{missing_note}

USER QUESTION: {query}

TASK: {task}

RULES:
- Use context as BACKGROUND only — synthesize, do not quote or copy-paste
- If context is marked ⚠ MAY BE OUTDATED, use your own knowledge instead
- Write ONE coherent answer, not separate sections per concept
- Be specific and practical
"""

    log.info(f"[UNIVERSAL] LLM call | contexts={len(flat_relevant)} | missing={len(missing_concepts)}")
    try:
        response = chat_completion(
            model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000
        )
        answer     = response.choices[0].message.content
        model_used = response.model
        _kb_exists = _sop_has_knowledge()
        tokens_in  = getattr(response.usage, "prompt_tokens",  0)
        tokens_out = getattr(response.usage, "completion_tokens", 0)
        total_toks = tokens_in + tokens_out
        tokens_saved = max(0, len(flat_relevant) * 600 - total_toks)
        save_to_cache(query, answer, model_used, total_toks, namespace, query_had_pii)
        log.info(f"[UNIVERSAL] cached | tokens={total_toks} | saved~{tokens_saved}")
        audit.record(r, user=user, query=query, model=model_used,
                     source=_normalize_source(model_used), namespace=namespace, tokens_used=total_toks,
                     tokens_saved=tokens_saved,
                     latency_ms=int((time.time()-_audit_start)*1000))
        return {
            "answer":             answer,
            "source":             model_used,
            "intent":             intent,
            "concepts":           concepts,
            "cache_entries_used": len(flat_relevant),
            "missing_concepts":   missing_concepts,
            "stale_used":         sum(1 for x in flat_relevant if x["stale"]),
            "tokens_used":        total_toks,
            "tokens_saved":       tokens_saved,
            "cached":             False,
            "cached_now":         True,
            "compositional":      True,
            "searched_llm": True,
            "llm_note": ("Not found in company knowledge — answered by AI. "
                         "Verify company-specific details with the relevant team.") if _kb_exists else None,
        }
    except Exception as e:
        log.error(f"[UNIVERSAL] failed: {e}")
        return {"answer": f"Error: {str(e)}", "source": "error", "cached": False}

# ── Context-dependent query detection ────────────────────────
CONTEXT_DEPENDENT_PREFIXES = [
    "give me", "show me", "tell me more", "explain more",
    "step by step", "example", "elaborate", "continue",
    "go on", "and then", "what about", "how about",
    # "and in Azure" after "...in AWS" — 3 words, no anaphora, so neither the
    # length rule nor the anaphora rule catches it.
    "and in", "and on", "and for", "and with", "what about in", "same for",
    "more detail", "can you", "please explain", "expand on"
]
def is_context_dependent(query: str) -> bool:
    """Returns True if query relies on previous conversation context."""
    q = query.lower().strip()

    # Must start with a context-dependent phrase to count
    if any(q.startswith(p) for p in CONTEXT_DEPENDENT_PREFIXES):
        return True

    # Very short AND vague (no question word, no topic noun)
    # e.g. "continue", "more", "ok go on" — these have no standalone meaning
    QUESTION_WORDS = ["what", "how", "why", "when", "where", "who",
                       "explain", "compare", "define", "describe",
                       "list", "show", "tell"]
    has_question_word = any(w in q for w in QUESTION_WORDS)

    # A short query is context-dependent only when genuinely contentless
    # ("continue", "more"). A short imperative ("restart nginx", "enable ssh")
    # is self-contained and MUST stay cacheable — flagging it silently drops
    # common commands from the cache.
    if len(q.split()) < 3 and not has_question_word:
        _content = [w for w in re.findall(r"[a-z0-9][a-z0-9\-\.]*", q)
                    if w not in _CTX_STOPWORDS
                    and w not in _FILLER_WORDS
                    and not _ANAPHORA_RE.fullmatch(w)]
        if not _content:
            return True

    # Anaphora with no subject of its own — "how do I install that".
    # Added after a real cache-poisoning bug: that query, asked after
    # "what is nginx", cached an nginx answer under a string that ANY
    # conversation produces. A later kubernetes conversation hit it EXACT
    # (score 1.0) and was served the nginx answer — and via commons it
    # crossed namespaces. The prefix checks above miss it entirely: it
    # starts with "how", contains a question word, and is 5 words long,
    # so it looks self-contained. The semantic-safety family can't help
    # either — the strings are identical, so there is no flip to detect.
    if _ANAPHORA_RE.search(q):
        words = re.findall(r"[a-z0-9][a-z0-9\-\.]*", q)
        content = [w for w in words
                   if w not in _CTX_STOPWORDS and not _ANAPHORA_RE.fullmatch(w)]
        if len(content) <= 2:
            return True

    return False


def _trim_history_for_llm(history: list, query: str) -> list:
    """Token-saving: cap conversation history to the last 2 exchanges and
    truncate each turn to MAX_CONTEXT_CHARS, so one long prior answer
    doesn't get re-billed verbatim on every subsequent turn of a long
    conversation. (Tried gating this on is_context_dependent() — skip
    history entirely for queries that don't look like follow-ups — but
    that heuristic is tuned for cache-key decisions and misses common
    real follow-ups like "what is my name?"/"what is my order number?"
    that don't use this/that/it. Silently dropping context there would
    be a correctness bug, not a savings — not worth it for a few tokens.)"""
    if not history:
        return []
    trimmed = []
    for h in history[-4:]:
        trimmed.append({
            "role": h.get("role", "user"),
            "content": (h.get("content") or "")[:MAX_CONTEXT_CHARS],
        })
    return trimmed

# ── Main ask() ───────────────────────────────────────────────

_AUTO_PREF_ORDER = ["groq", "nvidia", "nvidia_kimi-k2", "nvidia_deepseek-v4-flash", "deepseek", "gemini", "claude"]

def _resolve_auto_provider():
    """
    Pick the lowest-cost enabled provider. Ties (e.g. multiple free
    providers at 0.0) are broken by a fast/reliable preference order.
    Falls back to 'groq' if the registry can't be read.
    """
    from providers import provider_registry
    try:
        provs = provider_registry()
        if not provs:
            return "groq"
        # lowest cost first; tie-break by preference order (lower index = preferred)
        def _rank(item):
            key, p = item
            cost = p.get("cost_per_1k_tokens", 999)
            try:
                pref = _AUTO_PREF_ORDER.index(key)
            except ValueError:
                pref = 999
            return (cost, pref)
        best_key, best = sorted(provs.items(), key=_rank)[0]
        log.info(f"AUTO resolved -> {best_key} (cost={best.get('cost_per_1k_tokens')})")
        return best_key
    except Exception as _e:
        log.warning(f"AUTO resolve failed ({_e}); using groq")
        return "groq"


def _sop_has_knowledge():
    """True if the org has ANY knowledge loaded in the sop namespace."""
    try:
        return bool(r.smembers("recalq:docs:sop"))
    except Exception:
        return False


# ── Audit source normalization ────────────────────────────────────
# The LLM API returns a model string (e.g. "llama-3.3-70b-versatile").
# Recording that as `source` splits one provider across several names in
# the audit log and dashboard. Map it back to the client.yaml key.
_SOURCE_MAP = None

def _load_source_map():
    """Build {model_string: key} and the set of known keys from client.yaml."""
    global _SOURCE_MAP
    if _SOURCE_MAP is not None:
        return _SOURCE_MAP
    from providers import provider_registry
    mapping, keys = {}, set()
    try:
        for key, prov in provider_registry().items():
            keys.add(key)
            model = prov.get("model")
            if model:
                mapping[model] = key
                # some providers return a bare name without the vendor prefix
                if "/" in model:
                    mapping[model.split("/", 1)[1]] = key
    except Exception as e:
        log.warning(f"source map unavailable, using raw model strings: {e}")
    _SOURCE_MAP = (mapping, keys)
    return _SOURCE_MAP

def _normalize_source(model_string):
    """
    Return the stable provider key for an audit `source`.
    Passes through strings that are already keys; maps known model strings;
    leaves anything unrecognized untouched rather than guessing.
    """
    if not model_string:
        return model_string
    mapping, keys = _load_source_map()
    if model_string in keys:          # already a key (e.g. "nvidia_kimi-k2")
        return model_string
    if model_string in mapping:       # a known model string
        return mapping[model_string]
    if "/" in model_string:           # try without the vendor prefix
        tail = model_string.split("/", 1)[1]
        if tail in mapping:
            return mapping[tail]
    return model_string               # unknown -> keep raw, don't invent


def _extract_topic(history, max_words: int = 3):
    """
    Pull the conversation's topic from the last user turn — pure text, no LLM.

        [{"role":"user","content":"what is nginx"}, ...]  -> "nginx"
        [{"role":"user","content":"what is kubernetes"}]  -> "kubernetes"

    Returns None when there's nothing usable, in which case the caller leaves
    the query uncontextualized and it falls through to the LLM as before.

    This only ever builds a cache KEY. A wrong topic means a cache miss, not a
    wrong answer — the similarity threshold and safety family still apply.
    """
    if not history:
        return None
    text = None
    for msg in reversed(history):
        try:
            if msg.get("role") == "user":
                text = msg.get("content") or ""
                break
        except AttributeError:
            continue          # tolerate unexpected history shapes
    if not text:
        return None
    words = re.findall(r"[a-z0-9][a-z0-9\-\.]*", text.lower())
    content = [w for w in words
               if w not in _CTX_STOPWORDS
               and not _ANAPHORA_RE.fullmatch(w)
               and len(w) > 2]
    if not content:
        return None
    return "-".join(content[:max_words])


def _contextualize(query, history):
    """
    Return the string to use as the cache key for this query.
    Self-contained queries are returned unchanged.
    """
    if not history or not is_context_dependent(query):
        return query
    topic = _extract_topic(history)
    if not topic:
        return query          # nothing to anchor to — stays uncached
    return f"[CTX:{topic}] {query}"

# ── Documents (RAG) ─────────────────────────────────────────────
def ingest_document(filepath: str, filename: str, namespace: str = None, uploaded_by: str = None):
    """Attach a PDF/DOCX/TXT file so later questions can be answered from it.
    Returns (doc_id, meta). See documents.ingest_document for details —
    this just wires it to the shared redis/embedder/guardrails instances."""
    import documents as _docs_mod
    import guardrails as _guardrails_mod
    return _docs_mod.ingest_document(
        r, embedder, _guardrails_mod, filepath, filename,
        namespace or DEFAULT_NAMESPACE, uploaded_by,
    )


_PROJECT_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                      "dist", "build", ".next", "target", ".pytest_cache"}
_PROJECT_KEY_FILES = ["README.md", "README.rst", "README.txt", "README",
                      "package.json", "pyproject.toml", "requirements.txt",
                      "Cargo.toml", "go.mod", "composer.json", "Gemfile",
                      "setup.py", "pom.xml"]


def scan_project(root: str, max_files: int = 200, max_file_chars: int = 3000,
                 max_total_chars: int = 15000) -> str:
    """A cheap stand-in for Claude Code's filesystem awareness: no agentic
    tool loop, just a one-shot text summary (file tree + key manifest/readme
    contents) of the current directory, meant to be ingested as a doc so
    'analyze this project' has something real to answer from instead of
    the LLM guessing blind about a folder it's never seen."""
    lines = [f"PROJECT DIRECTORY: {root}", ""]
    tree = []
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in sorted(dirnames)
                       if d not in _PROJECT_SKIP_DIRS and not d.startswith(".")]
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        if depth > 3:
            dirnames[:] = []
            continue
        for f in sorted(filenames):
            if count >= max_files:
                break
            path = os.path.join(rel, f) if rel != "." else f
            tree.append(path)
            count += 1
    lines.append(f"FILE TREE ({count}{'+' if count >= max_files else ''} files):")
    lines.extend(tree)
    lines.append("")

    total = sum(len(l) for l in lines)
    for name in _PROJECT_KEY_FILES:
        path = os.path.join(root, name)
        if not os.path.isfile(path) or total >= max_total_chars:
            continue
        try:
            with open(path, "r", errors="ignore") as f:
                content = f.read(max_file_chars)
            block = f"\n── {name} ──\n{content}"
            lines.append(block)
            total += len(block)
        except Exception:
            pass
    return "\n".join(lines)


# ── Vision (image Q&A) ────────────────────────────────────────────
# Not part of the semantic cache (that's text-embedding only) — a direct,
# uncached call to a vision-capable model. Default provider is overridable
# per-call; pick one whose underlying model actually supports images
# (gemini and claude both do) if you point this at something else.
VISION_DEFAULT_MODEL = os.getenv("VISION_MODEL", "gemini")

def ask_image(image_bytes: bytes, mime_type: str, question: str, model: str = None,
              namespace: str = None, user: str = None) -> dict:
    import base64
    model = model or VISION_DEFAULT_MODEL
    b64 = base64.b64encode(image_bytes).decode()
    messages = [{
        "role": "user",
        "content": [
            {"type": "text", "text": question or "Describe this image."},
            {"type": "image_url", "image_url": {"url": f"data:{mime_type};base64,{b64}"}},
        ],
    }]
    _t0 = time.time()
    try:
        response = chat_completion(model, messages, max_tokens=800)
        answer = response.choices[0].message.content
        model_used = response.model
        tokens = getattr(getattr(response, "usage", None), "total_tokens", 0) or 0
        audit.record(r, user=user, query=f"[image] {question or ''}", model=model_used,
                     source=_normalize_source(model_used), namespace=namespace or DEFAULT_NAMESPACE,
                     tokens_used=tokens, latency_ms=int((time.time() - _t0) * 1000))
        return {"answer": answer, "source": model_used, "tokens_used": tokens, "cached": False}
    except Exception as e:
        log.error(f"ask_image failed: {e}")
        return {"answer": f"Error: {e}", "source": "error", "cached": False}


def ask(query: str, model: str = "gemini", history: list = None, namespace: str = None, user: str = None, recent_doc_id: str = None, has_attachments: bool = False, role: str = "user", knowledge_grant: list = None) -> dict:
    _audit_start = time.time()
    # Resolve 'auto' to the real lowest-cost provider so the rest of the
    # app (dashboard, audit, response) reports the ACTUAL model used.
    if model == "auto":
        model = _resolve_auto_provider()
    _audit_original_query = query
    stats = get_stats()
    stats["total_queries"] += 1

    # Step -1 — guardrails check BEFORE anything else touches this query
    guard = check_query(query, mode="redact")
    if guard["action"] == "block":
        log.warning(f"BLOCKED by guardrails: '{query[:50]}'")
        _save_stats(stats)
        audit.record(r, user=user, query="[BLOCKED QUERY]", model=model,
                     source="guardrails", namespace=namespace, blocked=True,
                     pii_types=[f["type"] for f in guard.get("findings", [])],
                     latency_ms=int((time.time()-_audit_start)*1000))
        return {
            "answer":  guard["blocked_message"],
            "source":  "guardrails",
            "cached":  False,
            "blocked": True
        }
    # Cache key for this turn. A follow-up ("how do I install that") gets its
    # topic prepended so it can't collide with the same words from a different
    # conversation — and so a repeat of the same follow-up on the same topic
    # can hit the cache for free. The LLM still receives the real query.
    _cache_query = _contextualize(query, history)
    if _cache_query != query:
        log.info(f"CONTEXTUALIZED: '{query[:40]}' -> '{_cache_query[:60]}'")

    query_had_pii = False
    if guard["action"] == "redact":
        log.info(f"Query redacted by guardrails | types={[f['type'] for f in guard['findings']]}")
        query = guard["clean_text"]
        query_had_pii = True

    # Step 0 — exact match short-circuit (fast path, no embeddings)
    exact_cached, exact_score = find_exact_match(_cache_query, namespace)
    if not exact_cached and _shares_commons(namespace):
        exact_cached, exact_score = find_exact_match(_cache_query, "commons")
    if exact_cached:
        est_saved = len(query.split()) * 4 + 500
        stats["cache_hits"]       += 1
        stats["tokens_saved_est"] += est_saved
        _save_stats(stats)
        log.info(f"EXACT CACHE HIT | saved~{est_saved}")
        audit.record(r, user=user, query=query, model="cache",
                     source="cache", namespace=namespace, tokens_saved=est_saved,
                     latency_ms=int((time.time()-_audit_start)*1000))
        return {
            "answer":         exact_cached["answer"],
            "source":         "cache",
            "similarity":     1.0,
            "original_query": exact_cached["query"],
            "hits":           exact_cached["hits"],
            "tokens_saved":   est_saved,
            "cached":         True,
            "exact":          True
        }

    # Step 1 — semantic cache check (slower, embedding-based)
    cached, score = find_semantic_match(_cache_query, namespace)
    if not cached and _shares_commons(namespace):
        cached, score = find_semantic_match(_cache_query, "commons")
    if cached:
        est_saved = len(query.split()) * 4 + 500
        stats["cache_hits"]       += 1
        stats["tokens_saved_est"] += est_saved
        _save_stats(stats)
        log.info(f"CACHE HIT | score={score:.3f} | saved~{est_saved}")
        audit.record(r, user=user, query=query, model="cache",
                     source="cache", namespace=namespace, tokens_saved=est_saved,
                     latency_ms=int((time.time()-_audit_start)*1000))
        return {
            "answer":         cached["answer"],
            "source":         "cache",
            "similarity":     round(score, 3),
            "original_query": cached["query"],
            "hits":           cached["hits"],
            "tokens_saved":   est_saved,
            "cached":         True
        }

    # Step 1.5 — SOP / solutions layer (org knowledge before the LLM)
    try:
        import documents as _docs_mod
        _sop = sop_layer.try_sop_answer(
            query, model, user=user, query_had_pii=query_had_pii, role=role,
            knowledge_grant=knowledge_grant,
            r=r, embedder=embedder, client=chat_completion,
            documents_mod=_docs_mod,
            safety_conflict_fn=_semantic_safety_conflict,
            save_to_cache_fn=save_to_cache, audit_mod=audit,
            stats=stats, save_stats_fn=_save_stats,
            audit_start=_audit_start)
        if _sop:
            return _sop
    except Exception as _e:
        log.warning(f'SOP layer skipped: {_e}')

    # Step 1.7 — attached documents (Option 3: try the doc first when one is
    # attached or strongly matches or it's an explicit doc-command; fall
    # through to the LLM if the composed answer is a dead-end).
    try:
        import documents as _docs_mod
        _ql = query.lower()
        _is_doc_cmd = any(kw in _ql for kw in [
            "summarize", "summarise", "review", "what's in", "whats in",
            "what is in", "key points", "tl;dr", "overview of", "explain this",
            "explain the document", "this document", "this file", "the file",
            "the document", "the pdf", "the doc", "attached",
            "this project", "this repo", "this repository", "this codebase",
            "this folder", "this directory"])
        _docs_exist = bool(_docs_mod.list_documents(r, namespace))
        if _docs_exist:
            _target = recent_doc_id if recent_doc_id else None
            _chunks = _docs_mod.find_relevant_chunks(r, embedder, None, query,
                                                     namespace, doc_id=_target, top_k=6)
            _DOC_TH = 0.60      # strong match — always use
            _DOC_FLOOR = 0.45   # below this, the doc doesn't really address it
            _top = _chunks[0]["score"] if _chunks else 0.0
            _strong = _top >= _DOC_TH
            # Option 3 with a floor: an attached doc is TRIED, but only USED if
            # it is at least loosely relevant (>= floor). Explicit doc-commands
            # (summarize/review THIS file) are trusted even below the floor.
            # A weak match (e.g. 0.30) means the doc doesn't cover the question
            # -> fall through to the LLM instead of forcing irrelevant chunks.
            _try_docs = _is_doc_cmd or _strong or (has_attachments and _top >= _DOC_FLOOR)
            if _try_docs:
                _dr = _docs_mod.ask_document(r, embedder, None, chat_completion, query,
                        namespace, model=model, doc_id=_target,
                        save_to_cache_fn=save_to_cache, user=user)
                _ans = (_dr.get("answer","") or "").lower()
                _dead = any(m in _ans for m in [
                    "no mention","does not answer","not answer your question",
                    "not found in","no information","does not contain",
                    "doesn't contain","not covered","do not contain",
                    "does not define","not mentioned","cannot answer",
                    "not provide","no relevant information"])
                # Trust explicit commands; else require not-dead-end.
                if _dr.get("chunks_used",0) > 0 and (_is_doc_cmd or not _dead):
                    _dr["source"] = "documents"
                    return _dr
    except Exception as _e:
        log.warning(f"Document layer skipped: {_e}")

    # Step 2 — route to universal engine if relational/multi-concept
    intent   = detect_intent(query)
    concepts = extract_concepts_universal(query)
    # Route to compositional engine ONLY for genuinely multi-concept
    # relational queries — NOT for simple definitions/procedures that
    # happen to contain several words. Simple questions get one fast
    # direct LLM call instead of the expensive multi-call synthesis path.
    COMPOSITIONAL_INTENTS = {"relational", "comparison", "similarity", "dependency"}
    # The LLM intent classifier over-tags ordinary questions as "relational"
    # (e.g. "what happens to images in X" is a simple lookup, not a synthesis).
    # Require an EXPLICIT relational/comparison keyword so only genuine
    # multi-concept synthesis questions ("compare X and Y", "how does X relate
    # to Y", "difference between") take the expensive compositional path.
    _REL_KEYWORDS = (
        "compare", "comparison", "versus", " vs ", " vs.", "difference between",
        "differences between", "relate", "relationship", "related to",
        "interact", "depend on", "depends on", "rely on", "work together",
        "connection between", "how does", "how do", "contrast", "trade-off",
        "tradeoff", "better than", "which is better",
    )
    _has_rel_kw = any(k in query.lower() for k in _REL_KEYWORDS)
    if intent in COMPOSITIONAL_INTENTS and len(concepts) >= 2 and _has_rel_kw:
        log.info(f"ROUTING TO UNIVERSAL | intent={intent} | concepts={len(concepts)} | rel_kw=yes")
        stats["llm_calls"] += 1
        _save_stats(stats)
        return ask_universal(query, model, history, namespace, query_had_pii, user, intent)

    # Step 3 — direct LLM call
    log.info(f"DIRECT LLM | model={model}")
    try:
        # Build messages with conversation history
        messages = []
        # Anti-hallucination guard: if the org HAS a knowledge base but this
        # question did not match it, the LLM must NOT invent company-specific
        # facts (policies, numbers, procedures). It answers from general
        # knowledge only and flags that it is not from company records.
        if _sop_has_knowledge():
            messages.append({
                "role": "system",
                "content": (
                    "The organization has an internal knowledge base, but it did "
                    "NOT contain an answer to this question. Do NOT invent or guess "
                    "company-specific facts such as leave counts, policy numbers, "
                    "internal procedures, URLs, or product details. If the question "
                    "asks for a company-specific fact, clearly state that it was not "
                    "found in the company knowledge base and that the user should "
                    "verify with the relevant team. You may answer general, "
                    "non-company-specific questions normally."
                )
            })
        messages.extend(_trim_history_for_llm(history, query))
        messages.append({"role": "user", "content": query})

        response = chat_completion(
            model,
            messages=messages,
            max_tokens=800
        )
        answer     = response.choices[0].message.content
        model_used = response.model
        tokens_in  = getattr(response.usage, "prompt_tokens",  0)
        tokens_out = getattr(response.usage, "completion_tokens", 0)
        total_toks = tokens_in + tokens_out

        # Only cache self-contained queries
        # Context-dependent queries (e.g. "give me step by step")
        # are useless to cache — they mean nothing without prior context
        if not is_context_dependent(_cache_query):
            save_to_cache(_cache_query, answer, model_used, total_toks, namespace, query_had_pii)
            log.info(f"Cached new entry: '{query[:50]}'")
        else:
            log.info(f"Context-dependent — skipping cache: '{query[:50]}'")

        stats["llm_calls"]        += 1
        stats["tokens_saved_est"] += 0
        _save_stats(stats)

        audit.record(r, user=user, query=query, model=model_used,
                     source=_normalize_source(model_used), namespace=namespace, tokens_used=total_toks,
                     latency_ms=int((time.time()-_audit_start)*1000))
        return {
            "answer":      answer,
            "source":      model_used,
            "tokens_used": total_toks,
            "cached":      False,
            "cached_now":  True,
            "searched_llm": True,
            "llm_note": ("Not found in company knowledge — answered by AI. "
                         "Verify company-specific details with the relevant team.") if _sop_has_knowledge() else None,
        }
    except Exception as e:
        log.error(f"Direct LLM failed: {e}")
        stats["llm_calls"] += 1
        _save_stats(stats)
        return {"answer": f"Error: {str(e)}", "source": "error", "cached": False}

# ── CLI ──────────────────────────────────────────────────────
if __name__ == "__main__":
    import readline  # noqa: F401 — importing it wires input() up with arrow-key history

    _CLI_WORDS = ["quit", "stats", "cache", "providers", "status", "add", "project",
                  "model", "doc", "image"]

    def _cli_completer(text, state):
        buf = readline.get_line_buffer()
        if buf.startswith("model "):
            matches = [a for a, _, _ in list_providers() if a.startswith(text)]
        elif buf.startswith("doc ") or buf.startswith("image "):
            import glob
            matches = glob.glob(text + "*")
        else:
            matches = [w for w in _CLI_WORDS if w.startswith(text)]
        try:
            return matches[state]
        except IndexError:
            return None

    readline.set_completer_delims(" \t\n")  # keep '/', '.', '-' in words — needed for paths and aliases
    readline.set_completer(_cli_completer)
    readline.parse_and_bind("tab: complete")

    print(f"\n🧠 MemLayer CLI — 📁 {os.getcwd()}")
    print("   commands: quit | stats | cache | providers | status | add | project | "
          "model <name> | doc <path> | image <path> [question]")
    print("   (Tab completes commands/models/paths, ↑/↓ for history)\n")
    model = default_model()
    recent_doc_id = None
    _project_scanned = False
    history = []  # rolling chat turns, so follow-ups ("what is THIS for?") have context
    _PROJECT_TRIGGERS = ("analyze this project", "analyse this project", "analyze the project",
                         "explain this project", "explain this codebase", "explain this repo",
                         "what does this project do", "what is this project", "summarize this project",
                         "summarize this repo", "summarize this codebase")
    while True:
        try:
            user_input = input(f"[{model}] You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nBye.")
            break
        if not user_input:        continue
        if user_input == "quit":  break
        if user_input == "stats":
            s = get_stats()
            print(f"\n📊 queries={s['total_queries']} hits={s['cache_hits']} "
                  f"saved~{s['tokens_saved_est']} llm_calls={s['llm_calls']}\n")
            continue
        if user_input == "cache":
            for e in list_cache_entries()[:10]:
                print(f"  [{e.get('hits',0)} hits] {e['query'][:70]}")
            continue
        if user_input == "providers":
            for s in provider_status(live=False):
                mark = "✓" if s["ready"] else "✗ (missing API key)"
                shared = f"  [shares key with: {', '.join(s['shared_with'])}]" if s["shared_with"] else ""
                print(f"  {s['alias']:14s} → {s['model']:35s} {mark}{shared}")
            print("  (or type any litellm model string, e.g. ollama/llama3.1, openai/gpt-4o)")
            print("  'status' does a live check | 'add' registers a new provider\n")
            continue
        if user_input == "status":
            print("\nChecking providers (one real call each — costs a few tokens per provider)...\n")
            results = provider_status(live=True)
            ok       = [s for s in results if s["live_ok"] is True]
            failed   = [s for s in results if s["live_ok"] is False]
            unconfig = [s for s in results if not s["ready"]]

            def _line(s):
                shared = f"  [shares key with: {', '.join(s['shared_with'])}]" if s["shared_with"] else ""
                return f"  {s['alias']:14s} → {s['model']:35s}{shared}"

            print(f"✓ OK ({len(ok)})")
            for s in ok:
                print(_line(s))
            print(f"\n✗ FAILED ({len(failed)})")
            for s in failed:
                print(_line(s))
                print(f"      {s['live_error']}")
            if unconfig:
                print(f"\n○ NOT CONFIGURED — missing API key ({len(unconfig)})")
                for s in unconfig:
                    print(_line(s))
            print()
            continue
        if user_input in ("add", "add provider"):
            try:
                alias = input("  short name (e.g. 'openai'): ").strip()
                if not alias:
                    print("  cancelled\n"); continue
                provider_type = input("  provider type (openai/anthropic/gemini/groq/mistral/"
                                       "cohere/azure/bedrock/ollama): ").strip()
                model_name = input("  model name (e.g. gpt-4o, llama3.1): ").strip()
                api_base = input("  api_base (blank unless it's an OpenAI-compatible endpoint "
                                  "or local Ollama): ").strip() or None
                key_input = input("  API key — paste a new key, or an EXISTING env var name "
                                   "to reuse it (e.g. NVIDIA_MODEL_API_KEY), or blank for none: ").strip()
                key_env = None
                if key_input:
                    existing_key_envs = {s["key_env"] for s in provider_status(live=False) if s["key_env"]}
                    if key_input.isupper() and key_input in existing_key_envs:
                        key_env = key_input
                        print(f"  reusing existing key ${key_env} (shared with other providers using it)")
                    elif key_input.isupper() and key_input.replace("_", "").isalnum() and os.getenv(key_input):
                        key_env = key_input  # a real env var not in client.yaml yet, but already exported
                    else:
                        key_env = f"{alias.upper().replace('-', '_')}_API_KEY"
                        save_api_key(key_env, key_input)
                        print(f"  saved to .env as {key_env}")
                fallback_to = input("  fallback provider alias (blank to skip): ").strip() or None
                add_provider(alias, provider_type, model_name, api_key_env=key_env,
                             api_base=api_base, fallback_to=fallback_to)
                print(f"  Added '{alias}' — ready to use now (no restart needed): model {alias}\n")
            except (KeyboardInterrupt, EOFError):
                print("\n  cancelled\n")
            except Exception as e:
                print(f"  Failed to add provider: {e}\n")
            continue
        if user_input.startswith("model "):
            model = user_input.split(" ",1)[1].strip()
            print(f"Model: {model}\n")
            continue
        if user_input == "project":
            cwd = os.getcwd()
            print(f"\n📁 Scanning {cwd} ...")
            try:
                summary = scan_project(cwd)
                import tempfile as _tempfile
                with _tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
                    f.write(summary)
                    tmp_path = f.name
                doc_id, meta = ingest_document(tmp_path, "PROJECT_OVERVIEW.txt", uploaded_by="cli")
                os.unlink(tmp_path)
                recent_doc_id = doc_id
                _project_scanned = True
                print(f"Indexed {meta['chunk_count']} chunks from this folder. "
                      f"Ask things like 'what does this project do?'\n")
            except Exception as e:
                print(f"Failed to scan project: {e}\n")
            continue
        if user_input.startswith("doc "):
            path = user_input.split(" ", 1)[1].strip().strip('"')
            if not os.path.isfile(path):
                print(f"No such file: {path}\n")
                continue
            try:
                doc_id, meta = ingest_document(path, os.path.basename(path), uploaded_by="cli")
                recent_doc_id = doc_id
                print(f"\n📄 Ingested '{meta['filename']}' — {meta['chunk_count']} chunks. "
                      f"Ask away, it'll use this doc.\n")
            except Exception as e:
                print(f"Failed to ingest: {e}\n")
            continue
        if user_input.startswith("image "):
            rest = user_input.split(" ", 1)[1].strip()
            path, _, question = rest.partition(" ")
            path = path.strip('"')
            if not os.path.isfile(path):
                print(f"No such file: {path}\n")
                continue
            ext = path.lower().rsplit(".", 1)[-1]
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "gif": "image/gif", "webp": "image/webp"}.get(ext)
            if not mime:
                print(f"Unsupported image type: .{ext}\n")
                continue
            with open(path, "rb") as f:
                img_bytes = f.read()
            result = ask_image(img_bytes, mime, question, user="cli")
            print(f"\n◆ VISION [{result['source']}]\n\n{result['answer']}\n")
            history.append({"role": "user", "content": f"[sent an image] {question}".strip()})
            history.append({"role": "assistant", "content": result["answer"]})
            history[:] = history[-12:]
            continue
        if not _project_scanned and any(t in user_input.lower() for t in _PROJECT_TRIGGERS):
            cwd = os.getcwd()
            print(f"\n📁 (auto) scanning {cwd} first, so this isn't answered blind...")
            try:
                summary = scan_project(cwd)
                import tempfile as _tempfile
                with _tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
                    f.write(summary)
                    tmp_path = f.name
                doc_id, _meta = ingest_document(tmp_path, "PROJECT_OVERVIEW.txt", uploaded_by="cli")
                os.unlink(tmp_path)
                recent_doc_id = doc_id
                _project_scanned = True
            except Exception as e:
                print(f"  (project scan failed, answering without it: {e})")

        result = ask(user_input, model, history=history, recent_doc_id=recent_doc_id,
                     has_attachments=bool(recent_doc_id))
        src = result["source"]
        if src == "cache":
            print(f"\n⚡ CACHE HIT | sim={result['similarity']} | hits={result['hits']}")
        elif result.get("compositional"):
            print(f"\n🧩 COMPOSITIONAL | {result.get('cache_entries_used',0)} cached "
                  f"+ {len(result.get('missing_concepts',[]))} new | intent={result.get('intent')}")
        else:
            print(f"\n◆ LLM [{src}] | tokens={result.get('tokens_used',0)}")
        print(f"\n{result['answer']}\n")
        history.append({"role": "user", "content": user_input})
        history.append({"role": "assistant", "content": result["answer"]})
        history[:] = history[-12:]
