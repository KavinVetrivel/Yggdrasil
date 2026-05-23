"""Persist uploaded resource chunks in ChromaDB."""

import hashlib
import math
import re
import os
import sys
from typing import List, Dict, Any

import chromadb

if __package__ in {None, ""}:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from config import (
        CHROMA_BACKEND,
        CHROMA_CLOUD_API_KEY,
        CHROMA_CLOUD_DATABASE,
        CHROMA_CLOUD_HOST,
        CHROMA_CLOUD_PORT,
        CHROMA_CLOUD_SSL,
        CHROMA_CLOUD_TENANT,
        CHROMA_PERSIST_DIR,
        CHROMA_COLLECTION_NAME,
        CHROMA_EMBEDDING_DIMENSIONS,
        SENTENCE_TRANSFORMERS_MODEL,
    )
    from parser import RawChunk
else:
    from .config import (
        CHROMA_BACKEND,
        CHROMA_CLOUD_API_KEY,
        CHROMA_CLOUD_DATABASE,
        CHROMA_CLOUD_HOST,
        CHROMA_CLOUD_PORT,
        CHROMA_CLOUD_SSL,
        CHROMA_CLOUD_TENANT,
        CHROMA_PERSIST_DIR,
        CHROMA_COLLECTION_NAME,
        CHROMA_EMBEDDING_DIMENSIONS,
        SENTENCE_TRANSFORMERS_MODEL,
    )
    from .parser import RawChunk


# ── ChromaDB client ───────────────────────────────────────────────────────────

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+-]*")
_chroma_client = None
_collection = None
_encoder = None


def _create_client():
    backend = (CHROMA_BACKEND or "cloud").strip().lower()

    if backend == "local":
        return chromadb.PersistentClient(path=CHROMA_PERSIST_DIR)

    if backend in {"http", "remote"}:
        headers = {}
        if CHROMA_CLOUD_API_KEY:
            headers["Authorization"] = f"Bearer {CHROMA_CLOUD_API_KEY}"
        client_kwargs = {
            "host": CHROMA_CLOUD_HOST,
            "port": CHROMA_CLOUD_PORT,
            "ssl": CHROMA_CLOUD_SSL,
        }
        if headers:
            client_kwargs["headers"] = headers
        if CHROMA_CLOUD_TENANT:
            client_kwargs["tenant"] = CHROMA_CLOUD_TENANT
        if CHROMA_CLOUD_DATABASE:
            client_kwargs["database"] = CHROMA_CLOUD_DATABASE
        return chromadb.HttpClient(**client_kwargs)

    if not CHROMA_CLOUD_API_KEY:
        raise RuntimeError(
            "Chroma Cloud is the default backend. Set CHROMA_API_KEY, CHROMA_TENANT, and CHROMA_DATABASE, "
            "or set CHROMA_BACKEND=local to keep using the on-disk store."
        )

    client_kwargs = {"api_key": CHROMA_CLOUD_API_KEY}
    if CHROMA_CLOUD_TENANT:
        client_kwargs["tenant"] = CHROMA_CLOUD_TENANT
    if CHROMA_CLOUD_DATABASE:
        client_kwargs["database"] = CHROMA_CLOUD_DATABASE
    if CHROMA_CLOUD_HOST:
        client_kwargs["cloud_host"] = CHROMA_CLOUD_HOST
    if CHROMA_CLOUD_PORT:
        client_kwargs["cloud_port"] = CHROMA_CLOUD_PORT
    client_kwargs["enable_ssl"] = CHROMA_CLOUD_SSL
    return chromadb.CloudClient(**client_kwargs)


def _get_collection():
    global _chroma_client, _collection
    if _collection is None:
        _chroma_client = _create_client()
        _collection = _chroma_client.get_or_create_collection(
            name=CHROMA_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def get_collection():
    return _get_collection()


def _get_encoder():
    global _encoder
    if _encoder is not None:
        return _encoder

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        _encoder = None
        return None

    try:
        _encoder = SentenceTransformer(SENTENCE_TRANSFORMERS_MODEL)
    except Exception:
        _encoder = None
    return _encoder


# ── ID generation (deterministic, no duplicates on re-upload) ─────────────────

def _make_chunk_id(subject_code: str, source_file: str, chunk_index: int) -> str:
    raw = f"{subject_code}::{source_file}::{chunk_index}"
    return hashlib.md5(raw.encode()).hexdigest()


def _hash_embed_text(text: str) -> List[float]:
    vector = [0.0] * CHROMA_EMBEDDING_DIMENSIONS
    for token in _TOKEN_RE.findall((text or "").lower()):
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=16).digest()
        bucket = int.from_bytes(digest[:4], "little") % CHROMA_EMBEDDING_DIMENSIONS
        sign = 1.0 if digest[4] % 2 else -1.0
        weight = 1.0 + min(len(token), 12) / 12.0
        vector[bucket] += sign * weight

    norm = math.sqrt(sum(value * value for value in vector))
    if norm:
        vector = [value / norm for value in vector]
    return vector


def _embed_texts(texts: List[str]) -> List[List[float]]:
    encoder = _get_encoder()
    if encoder is None:
        return [_hash_embed_text(text) for text in texts]

    embeddings = encoder.encode(texts, normalize_embeddings=True, show_progress_bar=False)
    return [embedding.tolist() if hasattr(embedding, "tolist") else list(embedding) for embedding in embeddings]


# ── Metadata builder ──────────────────────────────────────────────────────────

def _build_metadata(
    chunk: RawChunk,
    subject_code: str,
    subject_name: str,
    semester: int,
    source_type: str,       # "ppt" | "syllabus" | "textbook"
) -> Dict[str, Any]:
    return {
        "subject_code": subject_code,
        "subject_name": subject_name,
        "semester": semester,
        "source_type": source_type,
        "source_file": chunk.source_file,
        "chunk_index": chunk.chunk_index,
        "topic_hint": chunk.topic_hint,
        "page_or_slide": chunk.page_or_slide,
    }


# ── Main upsert function ───────────────────────────────────────────────────────

def upsert_chunks(
    chunks: List[RawChunk],
    subject_code: str,
    subject_name: str,
    semester: int,
    source_type: str,
    batch_size: int = 64,
) -> Dict[str, int]:
    """
    Embeds and upserts chunks into ChromaDB in batches.
    Returns a summary: {total, upserted, skipped_empty}
    """
    collection = _get_collection()

    ids, documents, metadatas = [], [], []
    skipped = 0

    for chunk in chunks:
        if not chunk.text.strip():
            skipped += 1
            continue

        chunk_id = _make_chunk_id(subject_code, chunk.source_file, chunk.chunk_index)
        metadata = _build_metadata(chunk, subject_code, subject_name, semester, source_type)

        ids.append(chunk_id)
        documents.append(chunk.text)
        metadatas.append(metadata)

    # Upsert in batches to avoid hitting API rate limits
    total_upserted = 0
    for i in range(0, len(ids), batch_size):
        batch_ids = ids[i : i + batch_size]
        batch_docs = documents[i : i + batch_size]
        batch_meta = metadatas[i : i + batch_size]

        batch_embeddings = _embed_texts(batch_docs)

        collection.upsert(
            ids=batch_ids,
            documents=batch_docs,
            metadatas=batch_meta,
            embeddings=batch_embeddings,
        )
        total_upserted += len(batch_ids)

    return {
        "total_chunks": len(chunks),
        "upserted": total_upserted,
        "skipped_empty": skipped,
    }


# ── Query helper (used later by retrieval layer) ───────────────────────────────

def query_chunks(
    query_text: str,
    subject_codes: List[str],
    top_k: int = 5,
) -> List[Dict[str, Any]]:
    """
    Retrieve top_k chunks filtered to specific subject codes.
    Used by the query pipeline, not the ingestion pipeline.
    """
    collection = _get_collection()
    query_kwargs = {
        "query_embeddings": _embed_texts([query_text]),
        "n_results": top_k,
        "include": ["documents", "metadatas", "distances"],
    }
    if subject_codes:
        query_kwargs["where"] = {"subject_code": {"$in": subject_codes}}

    results = collection.query(**query_kwargs)

    hits = []
    for doc, meta, dist in zip(
        results["documents"][0],
        results["metadatas"][0],
        results["distances"][0],
    ):
        hits.append({
            "text": doc,
            "metadata": meta,
            "score": round(1 - dist, 4),    # cosine similarity
        })

    return hits