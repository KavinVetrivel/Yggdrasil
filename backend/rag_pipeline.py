"""RAG pipeline for curriculum Q&A, document QA, and summarization."""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

if __package__ in {None, ""}:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from config import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER
else:
    from .config import NEO4J_PASSWORD, NEO4J_URI, NEO4J_USER

try:
    from neo4j import GraphDatabase
except ImportError:  # pragma: no cover - optional runtime dependency
    GraphDatabase = None

try:
    if __package__ in {None, ""}:
        from vector_store import _embed_texts, get_collection
    else:
        from .vector_store import _embed_texts, get_collection
except Exception:  # pragma: no cover - optional runtime dependency
    _embed_texts = None
    get_collection = None


def load_env_file(path: str = ".env") -> None:
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as env_file:
        for line in env_file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue

            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


load_env_file()

_driver = None


def _get_driver():
    global _driver
    if _driver is None:
        if GraphDatabase is None:
            raise RuntimeError("Install the neo4j driver to use graph-backed RAG lookups.")
        if not NEO4J_URI or not NEO4J_USER or not NEO4J_PASSWORD:
            raise RuntimeError("Neo4j connection settings are missing from the environment.")
        _driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    return _driver


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _format_history(history: Optional[List[Dict[str, Any]]], current_question: str, max_messages: int = 6) -> str:
    if not history:
        return ""

    recent_messages = history[-max_messages:]
    normalized_question = _normalize_whitespace(current_question)
    if recent_messages:
        last_message = recent_messages[-1]
        last_role = _normalize_whitespace(str(last_message.get("role", ""))).lower()
        last_content = _normalize_whitespace(str(last_message.get("content", "")))
        if last_role == "user" and last_content == normalized_question:
            recent_messages = recent_messages[:-1]

    if not recent_messages:
        return ""

    lines = ["Conversation so far:"]
    for message in recent_messages:
        role = _normalize_whitespace(str(message.get("role", ""))).lower()
        content = _normalize_whitespace(str(message.get("content", "")))
        if not content:
            continue
        speaker = "Student" if role == "user" else "Assistant"
        lines.append(f"{speaker}: {content}")

    return "\n".join(lines) if len(lines) > 1 else ""


def _extract_unit_label(topic_hint: Optional[str], page_or_slide: Optional[Any]) -> str:
    hint = _normalize_whitespace(str(topic_hint or ""))
    if hint:
        unit_match = re.search(r"\b(unit|module)\s*[-–—:]?\s*([ivx]+|\d+)", hint, re.I)
        if unit_match:
            return f"{unit_match.group(1).title()} {unit_match.group(2).upper()}"
        return hint[:80]

    if page_or_slide not in (None, ""):
        return f"Page/Slide {page_or_slide}"

    return "Unknown"


def _normalize_similarity(distance: float) -> float:
    collection = get_collection() if get_collection is not None else None
    metric = "cosine"
    if collection is not None:
        metadata = getattr(collection, "metadata", None) or {}
        metric = str(metadata.get("hnsw:space", "cosine")).lower()

    if metric == "cosine":
        similarity = 1.0 - (distance / 2.0)
    elif metric in {"l2", "euclidean"}:
        similarity = 1.0 / (1.0 + distance)
    else:
        similarity = 1.0 - distance

    return round(max(0.0, min(1.0, similarity)), 4)


def retrieve_context(question: str, subject_code: str, top_k: int = 5) -> List[Dict[str, Any]]:
    """Query ChromaDB for the most relevant chunks within a subject."""
    if top_k <= 0:
        return []

    if get_collection is None or _embed_texts is None:
        raise RuntimeError("chromadb/vector_store are unavailable in this environment.")

    collection = get_collection()
    query_embeddings = _embed_texts([question])
    results = collection.query(
        query_embeddings=query_embeddings,
        n_results=top_k,
        where={"subject_code": subject_code},
        include=["documents", "metadatas", "distances"],
    )

    documents = (results.get("documents") or [[]])[0]
    metadatas = (results.get("metadatas") or [[]])[0]
    distances = (results.get("distances") or [[]])[0]

    hits: List[Dict[str, Any]] = []
    for document, metadata, distance in zip(documents, metadatas, distances):
        metadata = metadata or {}
        hits.append(
            {
                "text": document,
                "metadata": metadata,
                "score": _normalize_similarity(float(distance)),
                "source_file": metadata.get("source_file", "unknown"),
                "unit": _extract_unit_label(metadata.get("topic_hint"), metadata.get("page_or_slide")),
            }
        )

    return _select_relevant_sources(hits)


def _select_relevant_sources(hits: List[Dict[str, Any]], max_sources: int = 3, min_score: float = 0.22) -> List[Dict[str, Any]]:
    if not hits:
        return []

    ordered_hits = sorted(hits, key=lambda item: (float(item.get("score", 0.0)), str(item.get("source_file", ""))), reverse=True)
    selected: List[Dict[str, Any]] = []
    seen_files = set()

    for hit in ordered_hits:
        source_file = str(hit.get("source_file", "unknown"))
        score = float(hit.get("score", 0.0))
        if source_file in seen_files or score < min_score:
            continue
        selected.append(hit)
        seen_files.add(source_file)
        if len(selected) >= max_sources:
            break

    if selected:
        return selected

    return ordered_hits[:max_sources]


def get_graph_context(subject_code: str, topic_name: Optional[str] = None) -> Dict[str, Any]:
    """Query Neo4j for the subject structure and prerequisite chain."""
    driver = _get_driver()
    with driver.session() as session:
        subject_row = session.run(
            """
            MATCH (s:Subject {code: $subject_code})
            OPTIONAL MATCH (s)-[:BELONGS_TO]->(sem:Semester)
            RETURN s.code AS code,
                   s.name AS name,
                   sem.number AS semester,
                   s.credits AS credits
            """,
            subject_code=subject_code,
        ).single()
        if subject_row is None:
            raise ValueError(f"Subject not found: {subject_code}")

        subject = {
            "code": subject_row["code"],
            "name": subject_row["name"],
            "semester": subject_row["semester"],
            "credits": subject_row["credits"],
        }

        units_query = """
        MATCH (s:Subject {code: $subject_code})
        OPTIONAL MATCH (s)-[:HAS_UNIT]->(u:Unit)
        OPTIONAL MATCH (u)<-[:PART_OF_UNIT]-(t:Topic)
        RETURN u.key AS unit_key,
               u.number AS unit_number,
               u.title AS unit_title,
               collect(DISTINCT t.name) AS topics
        ORDER BY unit_number
        """

        unit_rows = session.run(units_query, subject_code=subject_code)
        units: List[Dict[str, Any]] = []
        topic_names: List[str] = []
        for row in unit_rows:
            topics = sorted(topic for topic in (row["topics"] or []) if topic)
            topic_names.extend(topics)
            units.append(
                {
                    "key": row["unit_key"],
                    "number": row["unit_number"],
                    "title": row["unit_title"],
                    "topics": topics,
                }
            )

        prereq_query = """
        MATCH path = (pre:Subject)-[:PREREQUISITE_OF*1..2]->(s:Subject {code: $subject_code})
        RETURN pre.code AS code,
               pre.name AS name,
               length(path) AS hops
        ORDER BY hops, code
        """

        prereq_rows = session.run(prereq_query, subject_code=subject_code)
        prerequisites: List[Dict[str, Any]] = []
        for row in prereq_rows:
            prerequisites.append(
                {
                    "code": row["code"],
                    "name": row["name"],
                    "hops": row["hops"],
                }
            )

    topic_focus = None
    if topic_name:
        normalized_topic = _normalize_whitespace(topic_name)
        topic_focus = next((topic for topic in topic_names if topic.lower() == normalized_topic.lower()), None)
        if topic_focus is None:
            topic_focus = normalized_topic

    return {
        "subject": subject,
        "topic_focus": topic_focus,
        "units": units,
        "topics": sorted(dict.fromkeys(topic_names)),
        "prerequisites": prerequisites,
    }


def assemble_prompt(
    question: str,
    chunks: List[Dict[str, Any]],
    graph_context: Dict[str, Any],
    history: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Build the final prompt for the OpenRouter model."""
    subject = graph_context.get("subject", {})
    subject_name = subject.get("name") or subject.get("code") or "the subject"
    history_text = _format_history(history, question)

    structure_lines: List[str] = []
    for unit in graph_context.get("units", []):
        topics = unit.get("topics") or []
        topics_text = ", ".join(topics) if topics else "No topics extracted"
        unit_label = f"Unit {unit.get('number')} - {unit.get('title')}".strip(" -")
        structure_lines.append(f"- {unit_label}: {topics_text}")

    if not structure_lines:
        structure_lines.append("- No units found in Neo4j for this subject.")

    prerequisites = graph_context.get("prerequisites", [])
    prereq_lines = []
    for prerequisite in prerequisites:
        prereq_lines.append(
            f"- {prerequisite.get('code', 'Unknown')} - {prerequisite.get('name', 'Unknown')} "
            f"({prerequisite.get('hops', '?')} hop{'s' if prerequisite.get('hops') != 1 else ''})"
        )
    if not prereq_lines:
        prereq_lines.append("- None found")

    chunk_lines: List[str] = []
    for index, chunk in enumerate(chunks, start=1):
        metadata = chunk.get("metadata", {})
        source_file = chunk.get("source_file") or metadata.get("source_file") or "unknown"
        unit_label = chunk.get("unit") or _extract_unit_label(metadata.get("topic_hint"), metadata.get("page_or_slide"))
        score = chunk.get("score")
        score_text = f", relevance: {score}" if score is not None else ""
        chunk_lines.append(
            f"[{index}] source: {source_file}, unit: {unit_label}{score_text}\n{chunk.get('text', '').strip()}"
        )
    if not chunk_lines:
        chunk_lines.append("[No relevant course material chunks were retrieved]")

    topic_focus = graph_context.get("topic_focus")
    topic_focus_line = f"\nFocus topic: {topic_focus}" if topic_focus else ""

    prompt_parts = [f"You are a curriculum assistant for {subject_name}."]
    if history_text:
        prompt_parts.append(history_text)

    prompt_parts.extend(
        [
            f"Curriculum structure:{topic_focus_line}",
            "\n".join(structure_lines),
            "Prerequisites covered:",
            "\n".join(prereq_lines),
            "Relevant content from course materials:",
            "\n\n".join(chunk_lines),
            f"Student question: {question}",
            "Answer based only on the provided content. If content is insufficient, say so.",
        ]
    )
    return "\n\n".join(prompt_parts)


def _call_openrouter(prompt: str, max_tokens: int = 900) -> str:
    api_keys = _get_openrouter_api_keys()
    if not api_keys:
        raise RuntimeError("OPENROUTER_API_KEY is missing.")

    models = _get_openrouter_models(api_keys)
    last_error = None

    for api_key in api_keys:
        for model in models:
            try:
                return _call_openrouter_with_key(prompt, api_key, model, max_tokens=max_tokens)
            except RuntimeError as error:
                last_error = error
                message = str(error)
                if "404" in message and "No endpoints found" in message:
                    continue
                if "401" in message or "403" in message or "missing" in message.lower():
                    continue
                raise

    raise RuntimeError(f"OpenRouter generation failed after trying fallback keys/models: {last_error}")


def _get_openrouter_api_keys() -> List[str]:
    explicit = [
        os.getenv("OPENROUTER_API_KEY2", ""),
        os.getenv("OPENROUTER_API_KEY_2", ""),
        os.getenv("OPENROUTER_API_KEY_ALT", ""),
        os.getenv("OPENROUTER_API_KEY_BACKUP", ""),
        os.getenv("OPENROUTER_API_KEY1", ""),
        os.getenv("OPENROUTER_API_KEY", ""),
    ]

    combined = os.getenv("OPENROUTER_API_KEYS", "")
    if combined:
        explicit.extend(part.strip() for part in combined.split(","))

    keys = []
    for api_key in explicit:
        if api_key and api_key != "your_openrouter_api_key_here" and api_key not in keys:
            keys.append(api_key)
    return keys


def _get_openrouter_models(api_keys: List[str]) -> List[str]:
    model_override = os.getenv("OPENROUTER_MODEL", "").strip()
    if model_override:
        return [model_override]

    discovered_models: List[str] = []
    for api_key in api_keys:
        discovered_models.extend(_fetch_openrouter_free_models(api_key))

    ordered_models: List[str] = []
    for model in discovered_models + [
        "meta-llama/llama-3.1-8b-instruct:free",
        "google/gemma-2-9b-it:free",
        "microsoft/phi-3-mini-128k-instruct:free",
    ]:
        if model and model not in ordered_models:
            ordered_models.append(model)
    return ordered_models


def _fetch_openrouter_free_models(api_key: str) -> List[str]:
    request = Request(
        "https://openrouter.ai/api/v1/models",
        headers={
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": os.getenv("OPENROUTER_REFERER", "https://yggdrasil.local"),
            "X-Title": os.getenv("OPENROUTER_TITLE", "Yggdrasil"),
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=30) as response:
            data = json.loads(response.read().decode("utf-8"))
    except Exception:
        return []

    raw_models = data.get("data") or data.get("models") or []
    free_models: List[str] = []
    for item in raw_models:
        model_id = item.get("id") or item.get("name")
        if not model_id:
            continue
        pricing = item.get("pricing") or {}
        free_pricing = False
        if isinstance(pricing, dict):
            prompt_price = pricing.get("prompt")
            completion_price = pricing.get("completion")
            free_pricing = str(prompt_price) in {"0", "0.0", "0.000000"} and str(completion_price) in {"0", "0.0", "0.000000"}
        if ":free" in model_id or free_pricing:
            free_models.append(model_id)

    return free_models


def _call_openrouter_with_key(prompt: str, api_key: str, model: str, max_tokens: int = 900) -> str:
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "You answer using only the provided curriculum context and retrieved chunks.",
            },
            {"role": "user", "content": prompt},
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
    }

    request = Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": os.getenv("OPENROUTER_REFERER", "https://yggdrasil.local"),
            "X-Title": os.getenv("OPENROUTER_TITLE", "Yggdrasil"),
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        error_body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenRouter HTTP {error.code} for model {model}: {error_body}") from error
    except (URLError, json.JSONDecodeError) as error:
        raise RuntimeError(f"OpenRouter request failed for model {model}: {error}") from error

    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError(f"OpenRouter returned no choices for model {model}: {data}")

    message = choices[0].get("message") or {}
    content = message.get("content")
    if content is None:
        raise RuntimeError(f"OpenRouter returned an empty assistant message for model {model}: {data}")

    raw = str(content).strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1] if raw.count("```") >= 2 else raw
        if raw.startswith("json"):
            raw = raw[4:]
    return raw.strip()


def ask(
    question: str,
    subject_code: str,
    top_k: int = 5,
    history: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Run retrieval, graph lookup, prompt assembly, and OpenRouter generation."""
    chunks = retrieve_context(question, subject_code, top_k=top_k)
    graph_context = get_graph_context(subject_code)
    prompt = assemble_prompt(question, chunks, graph_context, history=history)
    try:
        answer = _call_openrouter(prompt)
    except RuntimeError as error:
        answer = _fallback_answer(question, chunks, graph_context, str(error))
    return {
        "answer": answer,
        "sources": chunks,
        "graph_context_used": graph_context,
    }


def _fallback_answer(question: str, chunks: List[Dict[str, Any]], graph_context: Dict[str, Any], error_message: str) -> str:
    subject = graph_context.get("subject", {})
    subject_name = subject.get("name") or subject.get("code") or "this subject"
    if chunks:
        best_chunk = chunks[0]
        summary = _normalize_whitespace(str(best_chunk.get("text", "")))[:700]
        source_file = best_chunk.get("source_file", "unknown")
        return (
            f"I could not reach a supported OpenRouter free model for response generation. "
            f"Relevant material from {source_file} for {subject_name}: {summary}"
        )

    prereqs = graph_context.get("prerequisites", [])
    prereq_text = ", ".join(f"{item.get('code')} {item.get('name')}" for item in prereqs if item.get("code") or item.get("name"))
    if prereq_text:
        prereq_text = f"Prerequisites available: {prereq_text}. "

    return (
        f"I could not reach a supported OpenRouter free model for response generation. "
        f"{prereq_text}Please try again after selecting a different free OpenRouter model in OPENROUTER_MODEL."
    )
