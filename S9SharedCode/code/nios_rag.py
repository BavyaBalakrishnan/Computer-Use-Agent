"""Standalone RAG over the Infoblox NIOS WAPI 9.x Reference Guide.

Self-contained document QA that reuses the project's existing pieces — the
`VectorIndex` (FAISS cosine) from `vector_index.py` and the V9 gateway for
embeddings and answer synthesis — without touching the agent orchestrator or
the agent's shared `state/memory.json`. The index lives in its own directory
(`state/nios_wapi/`) so it never pollutes the agent's working memory.

Two commands:

    uv run python nios_rag.py ingest [--pdf PATH] [--max-pages N] [--rebuild]
        Extract → chunk → embed every chunk → persist a FAISS index plus a
        sidecar `chunks.json` (chunk text + page). Resumable: re-running picks
        up where a previous run stopped.

    uv run python nios_rag.py ask "your question" [-k 6] [--no-llm]
        Embed the question, retrieve the top-k chunks, and synthesize a
        grounded answer (with page citations) via the gateway. `--no-llm`
        prints the raw retrieved chunks only.

Embeddings: gateway `/v1/embed` (Ollama `nomic-embed-text`, 768-dim).
Answer synthesis: gateway `/v1/chat`, tagged `agent="nios_rag"` for the ledger.
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import gateway
from vector_index import VectorIndex

# ── paths ───────────────────────────────────────────────────────────────────
CODE_DIR = Path(__file__).resolve().parent
DEFAULT_PDF = CODE_DIR.parents[1] / "Infoblox NIOS WAPI 9.x Reference Guide (3).pdf"
STORE_DIR = CODE_DIR / "state" / "nios_wapi"
CHUNKS_PATH = STORE_DIR / "chunks.json"

# ── chunking knobs ──────────────────────────────────────────────────────────
CHUNK_CHARS = 900       # window size; well under the gateway's 8000-char cap
CHUNK_OVERLAP = 150     # sliding-window overlap to avoid splitting facts
MIN_CHUNK_CHARS = 80    # drop near-empty fragments (page furniture, blanks)
EMBED_WORKERS = 4       # concurrent embed POSTs; ordered batches keep resume sane
PERSIST_EVERY = 200     # flush index + chunks.json every N new chunks


# ── extraction + chunking ────────────────────────────────────────────────────
def _extract_pages(pdf_path: Path, max_pages: int | None) -> list[tuple[int, str]]:
    """Return [(page_number, text), ...] for text-bearing pages (1-based)."""
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover - environment guard
        raise SystemExit("pypdf is required. Run: uv add pypdf") from e

    reader = PdfReader(str(pdf_path))
    pages: list[tuple[int, str]] = []
    limit = len(reader.pages) if max_pages is None else min(max_pages, len(reader.pages))
    for i in range(limit):
        text = (reader.pages[i].extract_text() or "").strip()
        if text:
            pages.append((i + 1, text))
    return pages


def _window(text: str) -> list[str]:
    """Split one page's text into overlapping character windows."""
    if len(text) <= CHUNK_CHARS:
        return [text]
    out: list[str] = []
    start = 0
    step = CHUNK_CHARS - CHUNK_OVERLAP
    while start < len(text):
        out.append(text[start : start + CHUNK_CHARS])
        start += step
    return out


def _build_chunks(pages: list[tuple[int, str]]) -> list[dict]:
    """Deterministic chunk list: stable ids let ingestion resume safely."""
    chunks: list[dict] = []
    for page_no, text in pages:
        for piece in _window(text):
            piece = piece.strip()
            if len(piece) < MIN_CHUNK_CHARS:
                continue
            cid = f"nios:{len(chunks):06d}"
            chunks.append({"id": cid, "page": page_no, "text": piece})
    return chunks


# ── persistence helpers ──────────────────────────────────────────────────────
def _load_chunks() -> list[dict]:
    if CHUNKS_PATH.exists():
        return json.loads(CHUNKS_PATH.read_text())
    return []


def _save_chunks(chunks: list[dict]) -> None:
    STORE_DIR.mkdir(parents=True, exist_ok=True)
    CHUNKS_PATH.write_text(json.dumps(chunks, ensure_ascii=False))


# ── ingest ───────────────────────────────────────────────────────────────────
def ingest(pdf_path: Path, max_pages: int | None, rebuild: bool) -> None:
    gateway.ensure_gateway()
    client = gateway.LLM()

    STORE_DIR.mkdir(parents=True, exist_ok=True)
    index = VectorIndex(STORE_DIR)

    if rebuild:
        print("[ingest] --rebuild: clearing existing index + chunks")
        index.clear()
        if CHUNKS_PATH.exists():
            CHUNKS_PATH.unlink()

    chunks = _load_chunks()
    if not chunks:
        print(f"[ingest] reading {pdf_path.name}")
        pages = _extract_pages(pdf_path, max_pages)
        print(f"[ingest] {len(pages)} text pages; chunking...")
        chunks = _build_chunks(pages)
        _save_chunks(chunks)
    print(f"[ingest] {len(chunks)} total chunks")

    done = index.size  # chunks are added in id order, so resume from here
    if done >= len(chunks):
        print(f"[ingest] already complete ({done} embedded). Nothing to do.")
        return
    print(f"[ingest] resuming at chunk {done} ({len(chunks) - done} remaining)")

    def _embed(text: str) -> list[float]:
        return client.embed(text, task_type="retrieval_document")["embedding"]

    added_since_flush = 0
    with ThreadPoolExecutor(max_workers=EMBED_WORKERS) as pool:
        i = done
        while i < len(chunks):
            batch = chunks[i : i + EMBED_WORKERS]
            vecs = list(pool.map(_embed, [c["text"] for c in batch]))
            for c, vec in zip(batch, vecs):
                index.add(c["id"], vec)
            i += len(batch)
            added_since_flush += len(batch)
            if added_since_flush >= PERSIST_EVERY or i >= len(chunks):
                index.persist()
                added_since_flush = 0
                pct = 100.0 * i / len(chunks)
                print(f"[ingest] {i}/{len(chunks)} ({pct:.1f}%) embedded + persisted")

    print(f"[ingest] done. index size = {index.size}, store = {STORE_DIR}")


# ── ask ──────────────────────────────────────────────────────────────────────
_SYSTEM = (
    "You answer questions about the Infoblox NIOS WAPI 9.x reference using ONLY "
    "the provided context excerpts. Cite the page number(s) you used in the form "
    "(p. N). If the context does not contain the answer, say so plainly instead "
    "of guessing."
)


def _retrieve(client, question: str, k: int) -> list[dict]:
    index = VectorIndex(STORE_DIR)
    if index.size == 0:
        raise SystemExit("Index is empty. Run `ingest` first.")
    qvec = client.embed(question, task_type="retrieval_query")["embedding"]
    hits = index.search(qvec, k=k)
    by_id = {c["id"]: c for c in _load_chunks()}
    out: list[dict] = []
    for cid, score in hits:
        c = by_id.get(cid)
        if c:
            out.append({**c, "score": round(score, 4)})
    return out


def ask(question: str, k: int, use_llm: bool) -> None:
    gateway.ensure_gateway()
    client = gateway.LLM()
    hits = _retrieve(client, question, k)

    if not hits:
        print("No matching chunks found.")
        return

    if not use_llm:
        print(f"\nTop {len(hits)} chunks for: {question}\n")
        for h in hits:
            print(f"── p.{h['page']}  score={h['score']}  [{h['id']}]")
            print(h["text"].strip()[:600])
            print()
        return

    context = "\n\n".join(f"[p.{h['page']}] {h['text'].strip()}" for h in hits)
    prompt = (
        f"Context excerpts from the NIOS WAPI reference:\n\n{context}\n\n"
        f"Question: {question}\n\nAnswer using only the context above, with page citations."
    )
    reply = client.chat(
        prompt,
        system=_SYSTEM,
        agent="nios_rag",
        max_tokens=800,
        temperature=0.2,
    )
    print(f"\nQ: {question}\n")
    print(reply.get("text", "").strip())
    pages = sorted({h["page"] for h in hits})
    print(f"\nSources (retrieved pages): {', '.join(f'p.{p}' for p in pages)}")


# ── cli ──────────────────────────────────────────────────────────────────────
def main(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description="RAG over the NIOS WAPI 9.x guide.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ing = sub.add_parser("ingest", help="extract, chunk, embed, and index the PDF")
    p_ing.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    p_ing.add_argument("--max-pages", type=int, default=None)
    p_ing.add_argument("--rebuild", action="store_true", help="discard existing index")

    p_ask = sub.add_parser("ask", help="query the indexed document")
    p_ask.add_argument("question")
    p_ask.add_argument("-k", type=int, default=6, help="number of chunks to retrieve")
    p_ask.add_argument("--no-llm", action="store_true", help="show raw chunks only")

    args = parser.parse_args(argv)
    if args.cmd == "ingest":
        ingest(args.pdf, args.max_pages, args.rebuild)
    elif args.cmd == "ask":
        ask(args.question, args.k, use_llm=not args.no_llm)


if __name__ == "__main__":
    main(sys.argv[1:])
