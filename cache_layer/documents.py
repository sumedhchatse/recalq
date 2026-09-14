"""
documents.py — Document ingestion & RAG for Recalq
Place at: ~/memlayer/cache_layer/documents.py

Reuses existing Recalq machinery:
  - embedder (embedding service client) for chunk + query embeddings
  - guardrails.check_query for PII scanning
  - _batch_cosine for relevance search
  - r (Redis) for storage

Storage model (namespace-shared, persistent):
  recalq:doc:<namespace>:<doc_id>:meta          -> JSON doc metadata
  recalq:doc:<namespace>:<doc_id>:chunk:<n>     -> JSON {text, embedding}
  recalq:docs:<namespace>                        -> SET of doc_ids in namespace
"""
import os, json, time, hashlib, uuid, logging

log = logging.getLogger("recalq.documents")

CHUNK_WORDS   = 400        # target words per chunk
CHUNK_OVERLAP = 50         # word overlap between chunks (preserves context)
TOP_CHUNKS    = 5          # how many chunks to feed the LLM per question
MAX_DOC_MB    = 25         # reject files larger than this

DOC_PREFIX = "recalq:doc:"
DOCS_SET   = "recalq:docs:"


# ── Text extraction ──────────────────────────────────────────
def extract_text(filepath: str, filename: str) -> str:
    """Extract plain text from PDF, docx, or txt. Raises ValueError on unsupported."""
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""

    if ext == "txt":
        with open(filepath, "r", errors="ignore") as f:
            return f.read()

    if ext == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError:
            from PyPDF2 import PdfReader
        reader = PdfReader(filepath)
        return "\n\n".join((page.extract_text() or "") for page in reader.pages)

    if ext in ("docx",):
        import docx
        d = docx.Document(filepath)
        return "\n".join(p.text for p in d.paragraphs)

    raise ValueError(f"Unsupported file type: .{ext}. Supported: pdf, docx, txt")


# ── Chunking ─────────────────────────────────────────────────
def chunk_text(text: str) -> list:
    """Split text into overlapping word-chunks. Returns list of strings."""
    words = text.split()
    if not words:
        return []
    chunks = []
    step = CHUNK_WORDS - CHUNK_OVERLAP
    for i in range(0, len(words), step):
        chunk = " ".join(words[i:i + CHUNK_WORDS])
        if chunk.strip():
            chunks.append(chunk)
        if i + CHUNK_WORDS >= len(words):
            break
    return chunks


# ── Ingestion ────────────────────────────────────────────────
def _supersede_existing(r, ns, filename):
    """
    Delete any existing document with this filename in this namespace, so a
    re-upload replaces rather than duplicates. Returns the count removed.
    """
    removed = 0
    for did in list(r.smembers(f"{DOCS_SET}{ns}") or []):
        did = did.decode() if isinstance(did, bytes) else did
        raw = r.get(f"{DOC_PREFIX}{ns}:{did}:meta")
        if not raw:
            continue
        try:
            meta = json.loads(raw)
        except Exception:
            continue
        if meta.get("filename") == filename:
            delete_document(r, ns, did)
            removed += 1
    if removed:
        log.info(f"Superseded {removed} existing '{filename}' in [{ns}] before re-ingest")
    return removed


def ingest_document(r, embedder, guardrails, filepath, filename,
                    namespace, uploaded_by):
    """
    Full ingestion pipeline. Returns (doc_id, meta) or raises.
    - Extracts text
    - PII scans (redacts; raises if CRITICAL pii found -> caller blocks)
    - Chunks, embeds, stores namespace-shared
    """
    size_mb = os.path.getsize(filepath) / (1024 * 1024)
    if size_mb > MAX_DOC_MB:
        raise ValueError(f"File too large ({size_mb:.1f} MB). Max {MAX_DOC_MB} MB.")

    text = extract_text(filepath, filename)
    if not text.strip():
        raise ValueError("No extractable text found in document.")

    # PII scan the whole document. Critical PII (aadhaar/PAN/card) is MASKED
    # to xxxxxx at ingestion and the upload CONTINUES — so a user uploading,
    # e.g., an income statement that happens to contain an Aadhaar is helped
    # (document processed, number hidden) rather than blocked. Because the PII
    # is masked BEFORE storage, the chunks and any cached answer are safe to
    # share: there is no real number stored anywhere.
    scan = guardrails.check_query(text, mode="redact")
    found_types = {f["type"] for f in scan.get("findings", [])}
    if found_types:
        log.info(f"PII masked to xxxxxx in upload: {sorted(found_types)}")
    # Always use the redacted text when any PII (critical or not) was found.
    clean_text = scan["clean_text"] if scan.get("findings") else text

    chunks = chunk_text(clean_text)
    if not chunks:
        raise ValueError("Document produced no usable text chunks.")

    doc_id = hashlib.sha256(f"{filename}{time.time()}".encode()).hexdigest()[:16]
    ns = namespace or "default"

    # Replace any existing document of the same name in this namespace, so a
    # re-upload (v2) supersedes v1 instead of both living side by side.
    _supersede_existing(r, ns, filename)

    # Batch-embed all chunks at once (uses your embedding service)
    embeddings = embedder.encode(chunks, convert_to_numpy=True)

    # Store each chunk
    for i, (chunk, emb) in enumerate(zip(chunks, embeddings)):
        key = f"{DOC_PREFIX}{ns}:{doc_id}:chunk:{i}"
        r.set(key, json.dumps({
            "text": chunk,
            "embedding": emb.tolist() if hasattr(emb, "tolist") else list(emb),
        }))

    meta = {
        "doc_id":       doc_id,
        "filename":     filename,
        "namespace":    ns,
        "uploaded_by":  uploaded_by,
        "uploaded_at":  time.time(),
        "chunk_count":  len(chunks),
        "had_pii":      bool(scan.get("findings")),
        "size_mb":      round(size_mb, 2),
    }
    r.set(f"{DOC_PREFIX}{ns}:{doc_id}:meta", json.dumps(meta))
    r.sadd(f"{DOCS_SET}{ns}", doc_id)

    log.info(f"Ingested doc '{filename}' [{ns}] {len(chunks)} chunks, pii={meta['had_pii']}")
    return doc_id, meta


# ── Listing ──────────────────────────────────────────────────
def list_documents(r, namespace):
    """List all documents in a namespace."""
    ns = namespace or "default"
    doc_ids = r.smembers(f"{DOCS_SET}{ns}")
    docs = []
    for did in doc_ids:
        raw = r.get(f"{DOC_PREFIX}{ns}:{did}:meta")
        if raw:
            try:
                docs.append(json.loads(raw))
            except Exception:
                pass
    docs.sort(key=lambda d: d.get("uploaded_at", 0), reverse=True)
    return docs


def delete_document(r, namespace, doc_id):
    """Remove a document and all its chunks."""
    ns = namespace or "default"
    # Delete all chunk keys
    meta_raw = r.get(f"{DOC_PREFIX}{ns}:{doc_id}:meta")
    if meta_raw:
        meta = json.loads(meta_raw)
        for i in range(meta.get("chunk_count", 0)):
            r.delete(f"{DOC_PREFIX}{ns}:{doc_id}:chunk:{i}")
    r.delete(f"{DOC_PREFIX}{ns}:{doc_id}:meta")
    r.srem(f"{DOCS_SET}{ns}", doc_id)
    log.info(f"Deleted doc {doc_id} [{ns}]")
    return True


# ── Retrieval (RAG core) ─────────────────────────────────────
def find_relevant_chunks(r, embedder, batch_cosine_fn, question,
                         namespace, doc_id=None, top_k=TOP_CHUNKS):
    """
    Find the most relevant chunks for a question, across all docs in the
    namespace (or one specific doc if doc_id given).
    Returns list of {text, score, filename, doc_id}.
    """
    import numpy as np
    ns = namespace or "default"

    # Gather candidate doc_ids
    if doc_id:
        doc_ids = [doc_id]
    else:
        doc_ids = list(r.smembers(f"{DOCS_SET}{ns}"))

    if not doc_ids:
        return []

    # Collect all chunks with their embeddings
    entries = []
    filenames = {}
    for did in doc_ids:
        meta_raw = r.get(f"{DOC_PREFIX}{ns}:{did}:meta")
        if not meta_raw:
            continue
        meta = json.loads(meta_raw)
        filenames[did] = meta.get("filename", "?")
        for i in range(meta.get("chunk_count", 0)):
            raw = r.get(f"{DOC_PREFIX}{ns}:{did}:chunk:{i}")
            if raw:
                cd = json.loads(raw)
                entries.append({
                    "text": cd["text"],
                    "embedding": cd["embedding"],
                    "doc_id": did,
                })
    if not entries:
        return []

    # Embed the question, score against all chunks in one batched pass
    q_vec = embedder.encode(question, convert_to_numpy=True)
    q = np.asarray(q_vec, dtype=np.float32)
    mat = np.vstack([np.asarray(e["embedding"], dtype=np.float32) for e in entries])
    qn = q / (np.linalg.norm(q) + 1e-9)
    mn = mat / (np.linalg.norm(mat, axis=1, keepdims=True) + 1e-9)
    scores = mn @ qn

    ranked = sorted(
        [{"text": entries[i]["text"], "score": float(scores[i]),
          "doc_id": entries[i]["doc_id"],
          "filename": filenames.get(entries[i]["doc_id"], "?")}
         for i in range(len(entries))],
        key=lambda x: x["score"], reverse=True
    )
    return ranked[:top_k]


def build_rag_prompt(question, chunks):
    """Assemble the LLM prompt from question + retrieved chunks."""
    context = "\n\n".join(
        f"[From '{c['filename']}']\n{c['text']}" for c in chunks
    )
    return f"""You are answering a question using excerpts from the user's uploaded documents.

DOCUMENT EXCERPTS:
{context}

QUESTION: {question}

INSTRUCTIONS:
- Answer using ONLY the document excerpts above.
- If the excerpts don't contain the answer, say so clearly — do not invent.
- Cite which document the answer comes from when relevant.
- Be specific and accurate.
"""


# ── Full document Q&A entry point ────────────────────────────
def ask_document(r, embedder, batch_cosine_fn, client, question,
                 namespace, model="gemini", doc_id=None,
                 save_to_cache_fn=None, user=None):
    """
    Complete RAG query: retrieve relevant chunks, ask the LLM, return answer.
    Caches the answer NAMESPACE-PRIVATE only (never commons) per the
    security decision — document answers must not leak org-wide.
    """
    chunks = find_relevant_chunks(r, embedder, batch_cosine_fn,
                                  question, namespace, doc_id)

    # For summarize/review-style commands, semantic match may be weak.
    # Fall back to the first chunks of the document(s) so we can still
    # summarize actual content rather than returning "nothing found".
    ql = question.lower()
    is_command = any(kw in ql for kw in [
        "summarize", "summarise", "review", "key points", "tl;dr",
        "overview", "what's in", "whats in", "what is in", "explain this",
        "this document", "this file", "the document", "the file"
    ])
    if (not chunks or (chunks and chunks[0]["score"] < 0.35)) and is_command:
        # Pull the leading chunks of each doc in the namespace as context
        import json as _json
        ns = namespace or "default"
        doc_ids = ([doc_id] if doc_id else list(r.smembers(f"{DOCS_SET}{ns}")))
        fallback = []
        for did in doc_ids:
            meta_raw = r.get(f"{DOC_PREFIX}{ns}:{did}:meta")
            if not meta_raw:
                continue
            meta = _json.loads(meta_raw)
            for i in range(min(3, meta.get("chunk_count", 0))):
                raw = r.get(f"{DOC_PREFIX}{ns}:{did}:chunk:{i}")
                if raw:
                    cd = _json.loads(raw)
                    fallback.append({"text": cd["text"], "score": 0.0,
                                     "doc_id": did, "filename": meta.get("filename","?")})
        if fallback:
            chunks = fallback[:6]

    if not chunks:
        return {
            "answer": "No relevant documents found. Please upload a document first, "
                      "or your question may not match any uploaded content.",
            "source": "documents",
            "chunks_used": 0,
            "cached": False,
        }

    prompt = build_rag_prompt(question, chunks)

    try:
        response = client(
            model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1000,
        )
        answer = response.choices[0].message.content
        model_used = response.model
        tokens = getattr(response.usage, "total_tokens", 0)
    except Exception as e:
        return {"answer": f"Error querying document: {e}", "source": "error", "cached": False}

    # Cache NAMESPACE-PRIVATE only — never commons (security decision).
    # Tag the cache key with the SOURCE document(s) so a cached hit is only
    # reused in the correct document context, not served blindly.
    if save_to_cache_fn:
        try:
            src_docs = sorted({c["filename"] for c in chunks})
            src_tag = "|".join(src_docs)[:80]
            # query_had_pii=True forces namespace-private, blocks commons write-through
            save_to_cache_fn(f"[DOC:{src_tag}] {question}", answer,
                             model_used, tokens, namespace, True)
        except Exception as e:
            log.warning(f"Doc answer cache failed: {e}")

    sources = list({c["filename"] for c in chunks})
    return {
        "answer":       answer,
        "source":       model_used,
        "chunks_used":  len(chunks),
        "documents":    sources,
        "tokens_used":  tokens,
        "cached":       False,
    }
