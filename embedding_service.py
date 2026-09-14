"""
Recalq Embedding Service
A lightweight FastAPI service that runs the sentence-transformers model
in its OWN process with request batching. Your gunicorn workers call this
over HTTP instead of loading the model in-process.

WHY THIS SOLVES "user 2 waits":
  - Runs as a separate process — embedding no longer competes with your
    web workers' GIL.
  - Batches requests arriving within a short window into ONE model call,
    so 10 concurrent users get embedded together, not one-after-another.
  - Web workers become lightweight (no 90MB model each) — you can run
    MORE of them for I/O concurrency.

Run it standalone:
  uvicorn embedding_service:app --host 0.0.0.0 --port 8081 --workers 1
"""
import asyncio
import os
import time
import logging
from typing import List, Union

from fastapi import FastAPI
from pydantic import BaseModel

# Force offline — model is already local, never hit network
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

from sentence_transformers import SentenceTransformer

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("recalq.embed")

app = FastAPI(title="Recalq Embedding Service")

# ── Load model once, in this process ──────────────────────────
log.info("Loading embedding model (all-MiniLM-L6-v2)...")
_model = SentenceTransformer("all-MiniLM-L6-v2")
log.info("Embedding model ready")

# ── Micro-batching queue ──────────────────────────────────────
# Requests arriving within BATCH_WINDOW_MS get embedded together.
BATCH_WINDOW_MS = int(os.getenv("EMBED_BATCH_WINDOW_MS", "10"))
MAX_BATCH       = int(os.getenv("EMBED_MAX_BATCH", "64"))

_queue = None   # created on startup


class _PendingItem:
    __slots__ = ("text", "future")
    def __init__(self, text, future):
        self.text = text
        self.future = future


class EmbedRequest(BaseModel):
    inputs: Union[List[str], str]


class EmbedResponse(BaseModel):
    embeddings: List[List[float]]
    count: int
    batched: bool


async def _batch_worker():
    """Background task: drain the queue in batches and embed together."""
    while True:
        first: _PendingItem = await _queue.get()
        batch = [first]

        # Wait a tiny window to collect more requests into this batch
        deadline = time.monotonic() + BATCH_WINDOW_MS / 1000.0
        while len(batch) < MAX_BATCH:
            timeout = deadline - time.monotonic()
            if timeout <= 0:
                break
            try:
                nxt = await asyncio.wait_for(_queue.get(), timeout=timeout)
                batch.append(nxt)
            except asyncio.TimeoutError:
                break

        texts = [item.text for item in batch]
        # Embedding is CPU-bound — run in a thread so we don't block the loop
        vectors = await asyncio.to_thread(
            _model.encode, texts, convert_to_numpy=True
        )
        for item, vec in zip(batch, vectors):
            if not item.future.done():
                item.future.set_result(vec.tolist())
        log.info(f"Embedded batch of {len(batch)}")


@app.on_event("startup")
async def _startup():
    global _queue
    _queue = asyncio.Queue()
    asyncio.create_task(_batch_worker())
    log.info(f"Batch worker started (window={BATCH_WINDOW_MS}ms, max={MAX_BATCH})")


@app.get("/health")
async def health():
    return {"status": "ok", "model": "all-MiniLM-L6-v2", "dim": 384}


@app.post("/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest):
    texts = [req.inputs] if isinstance(req.inputs, str) else req.inputs
    loop = asyncio.get_event_loop()

    futures = []
    for t in texts:
        fut = loop.create_future()
        await _queue.put(_PendingItem(t, fut))
        futures.append(fut)

    embeddings = await asyncio.gather(*futures)
    return EmbedResponse(
        embeddings=embeddings,
        count=len(embeddings),
        batched=len(texts) > 1
    )
