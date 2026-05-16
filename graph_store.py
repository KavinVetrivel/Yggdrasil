"""Thin Neo4j client used by the ingestion API and subject queries."""

from typing import Optional, Dict, Any, List
from neo4j import GraphDatabase
from config import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD


_driver = None


def _get_driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    return _driver


# ── Subject validation & lookup ───────────────────────────────────────────────

def get_subject(subject_code: str) -> Optional[Dict[str, Any]]:
    """
    Returns {code, name, semester} for a subject code, or None if not found.
    """
    driver = _get_driver()
    query = """
    MATCH (s:Subject {code: $code})
    OPTIONAL MATCH (s)-[:BELONGS_TO]->(sem:Semester)
    RETURN s.code AS code, s.name AS name, sem.number AS semester, s.credits AS credits
    """
    with driver.session() as session:
        result = session.run(query, code=subject_code)
        record = result.single()
        if record is None:
            return None
        return {
            "code": record["code"],
            "name": record["name"],
            "semester": record["semester"],
            "credits": record["credits"],
        }


def get_all_subjects() -> List[Dict[str, Any]]:
    """
    Returns all subjects with semester — used to populate the frontend dropdown.
    """
    driver = _get_driver()
    query = """
    MATCH (s:Subject)
    OPTIONAL MATCH (s)-[:BELONGS_TO]->(sem:Semester)
    RETURN s.code AS code, s.name AS name, sem.number AS semester, s.credits AS credits
    ORDER BY semester, code
    """
    with driver.session() as session:
        results = session.run(query)
        return [
            {"code": r["code"], "name": r["name"], "semester": r["semester"], "credits": r["credits"]}
            for r in results
        ]


# ── Resource node creation (attach after ingestion) ───────────────────────────

def attach_resource_node(
    subject_code: str,
    source_file: str,
    source_type: str,
    chunk_count: int,
) -> None:
    """
    Creates or updates a Resource node and links it to the Subject.
    (:Subject)-[:HAS_RESOURCE]->(:Resource)
    """
    driver = _get_driver()
    resource_id = f"{subject_code}:{source_file}"
    with driver.session() as session:
        subject = session.run(
            """
            MATCH (s:Subject {code: $code})
            RETURN s.code AS code
            """,
            code=subject_code,
        ).single()
        if subject is None:
            raise ValueError(f"Subject not found for resource attachment: {subject_code}")

        query = """
        MATCH (s:Subject {code: $code})
        MERGE (r:Resource {resource_id: $resource_id})
        SET r.subject_code = $code,
            r.source_file = $source_file,
            r.source_type = $source_type,
            r.chunk_count = $chunk_count,
            r.updated_at = timestamp()
        MERGE (s)-[:HAS_RESOURCE]->(r)
        """
        session.run(
            query,
            code=subject_code,
            resource_id=resource_id,
            source_file=source_file,
            source_type=source_type,
            chunk_count=chunk_count,
        )


def close():
    global _driver
    if _driver:
        _driver.close()
        _driver = None