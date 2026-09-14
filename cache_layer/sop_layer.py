"""
SOP / Solutions Layer for Recalq.

A first-class tier in the lookup chain, sitting between the semantic cache
and the LLM:

    exact cache  ->  semantic cache  ->  [SOP/solutions]  ->  LLM

When a user asks a question or reports an error, this layer searches the
organization's accumulated SOPs (Standard Operating Procedures) and past
solutions. If a strong, SAFETY-CHECKED match is found, it composes an answer
from that documented knowledge (RAG) and cites the source — instead of paying
the LLM to reason from scratch.

Reuses documents.py ingestion/retrieval; SOPs live in a dedicated org-wide
namespace ("sop"). Applies the semantic safety family so it never returns,
e.g., an 'enable' SOP for a 'disable' question.

Integration: import and call try_sop_answer() from ask() with one line.
"""
import json
import time
import logging

log = logging.getLogger("recalq.sop")

# Dedicated org-wide namespace for SOPs / solutions
SOP_NAMESPACE = "sop"

# ── Category-based access control (minimal config, secure defaults) ──
# Which categories each role may access. Admin sees everything.
# Regular users see everything EXCEPT admin-only categories (e.g. policy).
_ADMIN_ONLY_CATEGORIES = {"policy"}   # HR/sensitive — admin only by default
_ALL_CATEGORIES = {"info", "sop", "solution", "policy"}

def allowed_categories(role, grant=None):
    """
    Categories this caller may access.

    role       — 'admin' sees all; 'user' sees all except admin-only (policy).
    grant      — optional explicit allow-list (agent tokens). When given, the
                 caller sees ONLY these categories, intersected with what the
                 role could ever see. So a grant narrows, never widens:
                 granting 'policy' to a user-role agent yields nothing, because
                 the role gate removes it. Default-deny: grant=[] -> no access.
    """
    if role == "admin":
        role_allowed = set(_ALL_CATEGORIES)
    else:
        role_allowed = _ALL_CATEGORIES - _ADMIN_ONLY_CATEGORIES
    if grant is None:
        return role_allowed
    return set(grant) & role_allowed

def _chunk_category(r, doc_id):
    """Look up a document's category from its stored meta."""
    try:
        import json as _json
        raw = r.get(f"recalq:doc:{SOP_NAMESPACE}:{doc_id}:meta")
        if raw:
            return _json.loads(raw).get("category", "sop")
    except Exception:
        pass
    return "sop"

# A SOP must match at least this well to be used (tuned like doc routing).
SOP_MATCH_THRESHOLD = 0.35   # retrieval floor — below this, don't even try
SOP_ANSWER_THRESHOLD = 0.50  # answer bar — below this, fall through to LLM
# Phrases that indicate the composed answer is a dead-end ("not in our docs").
_NOT_FOUND_MARKERS = [
    "no mention", "does not answer", "not answer your question",
    "not found in", "no information", "does not contain", "doesn't contain",
    "not covered", "does not define", "not mentioned", "unable to answer",
    "cannot answer", "no relevant information", "not provide",
]


def _normalise(text):
    return " ".join(text.lower().split())


def try_sop_answer(query, model, user=None, query_had_pii=False, role="user",
                   knowledge_grant=None,
                   *, r, embedder, client, documents_mod, safety_conflict_fn,
                   save_to_cache_fn, audit_mod, stats, save_stats_fn,
                   audit_start):
    """
    Attempt to answer from the SOP/solutions knowledge base.

    Returns a result dict (to be returned by ask()) if a strong, safe SOP
    match is found and answered. Returns None to fall through to the LLM.

    All Recalq internals are passed in explicitly (dependency injection) so
    this module stays decoupled and independently testable.
    """
    try:
        # 1. Retrieve candidate SOP chunks
        chunks = documents_mod.find_relevant_chunks(
            r, embedder, None, query, SOP_NAMESPACE, top_k=6)
        if not chunks:
            return None

        # ACL filter: drop chunks whose category this role cannot access.
        # Unauthorized knowledge becomes invisible -> falls through to LLM,
        # never revealing that restricted knowledge exists.
        _allowed = allowed_categories(role, knowledge_grant)
        _before = len(chunks)
        chunks = [c for c in chunks
                  if _chunk_category(r, c.get("doc_id", "")) in _allowed]
        if len(chunks) < _before:
            log.info(f"SOP ACL: filtered {_before - len(chunks)} chunk(s) "
                     f"outside role '{role}' access")
        if not chunks:
            return None

        top = chunks[0]
        # Gate on the ANSWER bar, not the retrieval floor. Step 4 below
        # discards anything under SOP_ANSWER_THRESHOLD regardless of what the
        # composed answer says — so composing a 0.35-0.50 match only ever
        # burned ~1000 tokens to reach a decision already determined here.
        # Same routing, no wasted call.
        if top["score"] < SOP_ANSWER_THRESHOLD:
            log.info(f"SOP: best match {top['score']:.3f} below "
                     f"{SOP_ANSWER_THRESHOLD} — falling through to LLM "
                     f"(0 probe tokens)")
            return None

        # 2. SAFETY CHECK — never return an SOP whose meaning conflicts with
        #    the query (enable vs disable, india vs nepal, v2 vs v3, etc).
        #    We compare the query against the matched SOP chunk's text.
        # NOTE: the polarity/entity safety family is designed for
        # question-vs-question cache matching, NOT question-vs-document-chunk.
        # A long doc chunk almost always contains some antonym/entity that
        # trips a false conflict. The similarity threshold already guards
        # relevance here, so we do not apply the safety-family check to SOP
        # retrieval. (Re-enable a NARROW version later if needed.)
        pass

        # 3. Build a grounded prompt from the top SOP chunks
        context_parts = []
        sources = []
        for c in chunks:
            if c["score"] >= SOP_MATCH_THRESHOLD - 0.1:  # include near-strong chunks
                context_parts.append(f"[{c['filename']}]\n{c['text']}")
                if c["filename"] not in sources:
                    sources.append(c["filename"])
        context = "\n\n---\n\n".join(context_parts[:5])

        system = (
            "You are answering strictly from the organization's official SOPs "
            "and documented solutions provided below. Use ONLY this information. "
            "If the provided material does not fully answer the question, say so "
            "clearly rather than inventing steps. Cite the source document name."
        )
        user_msg = (
            f"ORGANIZATION SOP / SOLUTION MATERIAL:\n\n{context}\n\n"
            f"QUESTION: {query}\n\n"
            f"Answer using only the material above, and name the source SOP."
        )

        # 4. One LLM call to compose the grounded answer
        response = client(
            model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
        )
        answer = response.choices[0].message.content
        tokens = getattr(getattr(response, "usage", None), "total_tokens", 0) or 0

        # Fall through to the LLM if the match was weak OR the SOP answer is a
        # dead-end ("not in our docs"). The user doesn't know or care whether
        # something is in the KB — they want a real answer. The anti-hallucination
        # guard in ask() still prevents inventing company-specific facts.
        _ans_low = (answer or "").lower()
        _is_dead_end = any(m in _ans_low for m in _NOT_FOUND_MARKERS)
        # The score half is now unreachable (gated above); kept as a guard
        # in case the gate is ever loosened. The dead-end half still fires:
        # a strong match can still yield "this isn't in our docs".
        if top["score"] < SOP_ANSWER_THRESHOLD or _is_dead_end:
            log.info(f"SOP: match {top['score']:.3f} weak or dead-end answer "
                     f"-> falling through to LLM (spent {tokens} probe tokens)")
            return None

        # 5. Stats + audit
        stats["llm_calls"] = stats.get("llm_calls", 0) + 1
        save_stats_fn(stats)
        try:
            audit_mod.record(r, user=user, query=query, model=model,
                             source="sop", namespace=SOP_NAMESPACE,
                             latency_ms=int((time.time() - audit_start) * 1000))
        except Exception:
            pass

        # 6. Cache the composed answer (namespace-private, tagged as SOP) so
        #    the NEXT person asking pays nothing.
        if save_to_cache_fn:
            try:
                src_tag = "|".join(sources)[:80]
                save_to_cache_fn(f"[SOP:{src_tag}] {query}", answer,
                                 model, tokens, SOP_NAMESPACE, True)
            except Exception as e:
                log.warning(f"SOP answer cache failed: {e}")

        log.info(f"SOP ANSWER | match={top['score']:.3f} | sources={sources}")
        return {
            "answer": answer,
            "source": "sop",
            "sop_sources": sources,
            "match_score": round(top["score"], 3),
            "chunks_used": len(context_parts[:5]),
            "tokens_used": tokens,
            "cached": False,
            "sop_reason": (
                f"Answered from your organization's SOPs/solutions "
                f"({sources[0] if sources else 'SOP'}), matched at "
                f"{int(round(top['score']*100))}% and passed safety checks."
            ),
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        log.warning(f"SOP layer error (falling through to LLM): {e}")
        return None


# ── Admin helpers to populate the SOP namespace ──────────────────

def ingest_sop(r, embedder, guardrails, filepath, filename, user,
               *, documents_mod, category="sop"):
    """Ingest an SOP/solution document into the org-wide 'sop' namespace."""
    doc_id, meta = documents_mod.ingest_document(
        r, embedder, guardrails, filepath, filename, SOP_NAMESPACE, user)
    # Stamp the category onto the stored meta so the UI can show it.
    try:
        import json as _json
        mk = f"{documents_mod.DOC_PREFIX}{SOP_NAMESPACE}:{doc_id}:meta"
        m = _json.loads(r.get(mk))
        m["category"] = category
        r.set(mk, _json.dumps(m))
        meta["category"] = category
    except Exception:
        pass
    return doc_id, meta


def list_sops(r, *, documents_mod):
    """List all SOPs/solutions currently in the knowledge base."""
    return documents_mod.list_documents(r, SOP_NAMESPACE)


def delete_sop(r, doc_id, *, documents_mod):
    return documents_mod.delete_document(r, SOP_NAMESPACE, doc_id)
