"""
main.py
FastAPI ingestion pipeline.

Endpoints:
  GET  /subjects               → list all subjects (for frontend dropdown)
  POST /ingest                 → upload file + subject_code, runs full pipeline
  GET  /health                 → sanity check
"""

import os
import sys
import tempfile
from typing import Dict

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

if __package__ in {None, ""}:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from config import CHUNK_SIZE_TOKENS, CHUNK_OVERLAP_TOKENS
    from parser import parse_file, detect_source_type
    from vector_store import upsert_chunks
    from graph_store import get_subject, get_all_subjects, attach_resource_node
    from rag_pipeline import ask as rag_ask
else:
    from .config import CHUNK_SIZE_TOKENS, CHUNK_OVERLAP_TOKENS
    from .parser import parse_file, detect_source_type
    from .vector_store import upsert_chunks
    from .graph_store import get_subject, get_all_subjects, attach_resource_node
    from .rag_pipeline import ask as rag_ask

app = FastAPI(title="Curriculum Ingestion API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # tighten this when you go to prod
    allow_methods=["*"],
    allow_headers=["*"],
)

ALLOWED_EXTENSIONS = {"pdf", "ppt", "pptx"}

# ── Helpers ───────────────────────────────────────────────────────────────────

def _ext(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _detect_source_type(filepath: str, ext: str) -> str:
    del ext
    return detect_source_type(filepath)


class ChatRequest(BaseModel):
    question: str
    subject_code: str


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/subjects")
def list_subjects():
    """Return all subjects grouped by semester for the upload UI dropdown."""
    subjects = get_all_subjects()
    grouped: Dict[int, list] = {}
    for s in subjects:
        grouped.setdefault(s["semester"], []).append(s)
    return {"semesters": grouped}


@app.post("/ingest")
async def ingest(
    file: UploadFile = File(...),
    subject_code: str = Form(...),
):
    """
    Main ingestion endpoint.
    1. Validate subject_code against Neo4j
    2. Save upload to temp file
    3. Parse → chunk
    4. Embed + upsert to ChromaDB
    5. Attach Resource node in Neo4j
    6. Return summary
    """
    # ── Validate file type ────────────────────────────────────────────────────
    ext = _ext(file.filename or "")
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '.{ext}'. Allowed: {ALLOWED_EXTENSIONS}",
        )

    # ── Validate subject exists in graph ─────────────────────────────────────
    subject = get_subject(subject_code.strip().upper())
    if subject is None:
        raise HTTPException(
            status_code=404,
            detail=f"Subject code '{subject_code}' not found in the knowledge graph.",
        )

    # ── Save to temp file ─────────────────────────────────────────────────────
    suffix = f".{ext}"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        # ── Parse ─────────────────────────────────────────────────────────────
        chunks = parse_file(tmp_path, chunk_size=CHUNK_SIZE_TOKENS, overlap=CHUNK_OVERLAP_TOKENS)

        if not chunks:
            raise HTTPException(
                status_code=422,
                detail="No extractable content found in the uploaded file.",
            )

        # ── Detect source type ────────────────────────────────────────────────
        source_type = _detect_source_type(tmp_path, ext)

        # ── Stamp filename on chunks (use original filename, not temp path) ───
        original_filename = file.filename or f"upload.{ext}"
        for chunk in chunks:
            chunk.source_file = original_filename

        # ── Embed + upsert to ChromaDB ────────────────────────────────────────
        summary = upsert_chunks(
            chunks=chunks,
            subject_code=subject["code"],
            subject_name=subject["name"],
            semester=subject["semester"],
            source_type=source_type,
        )

        # ── Attach Resource node in Neo4j ─────────────────────────────────────
        attach_resource_node(
            subject_code=subject["code"],
            source_file=original_filename,
            source_type=source_type,
            chunk_count=summary["upserted"],
        )

    finally:
        os.unlink(tmp_path)     # always clean up temp file

    return {
        "status": "success",
        "subject": subject,
        "source_type": source_type,
        "file": original_filename,
        "chunks": summary,
    }


@app.post("/chat")
def chat(payload: ChatRequest):
    try:
        result = rag_ask(payload.question, payload.subject_code.strip().upper())
    except ValueError as error:
        msg = str(error).lower()
        if "not found" in msg or "not found:" in msg:
            raise HTTPException(status_code=404, detail=str(error))
        # Other validation errors should be mapped to 400 Bad Request so callers can correct input
        raise HTTPException(status_code=400, detail=str(error))
    graph_context = result.get("graph_context_used", {})
    return {
        "answer": result.get("answer", ""),
        "sources": result.get("sources", []),
        "prereqs_used": graph_context.get("prerequisites", []),
    }


# ── Dev runner ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn

    module_name = "main" if __package__ in {None, ""} else f"{__package__}.main"
    uvicorn.run(f"{module_name}:app", host="0.0.0.0", port=8000, reload=True)