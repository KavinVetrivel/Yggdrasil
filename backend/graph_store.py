"""Thin Neo4j client used by the ingestion API and subject queries."""

import os
import sys
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from neo4j import GraphDatabase

if __package__ in {None, ""}:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from config import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD
else:
    from .config import NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD


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


def upsert_student_profile(
    student_id: str,
    email: str,
    college_id: str,
    regulation_id: str,
    program_id: str,
    semester_number: int,
    program_name: str | None = None,
    college_name: str | None = None,
    regulation_name: str | None = None,
    created_at: datetime | None = None,
) -> Dict[str, Any]:
    """Create or update a student node and the academic context around it."""
    driver = _get_driver()
    created_at = created_at or datetime.now(timezone.utc)
    with driver.session() as session:
        session.run(
            """
            MERGE (st:Student {id: $student_id})
            SET st.email = $email,
                st.college_id = $college_id,
                st.regulation_id = $regulation_id,
                st.created_at = coalesce(st.created_at, $created_at)
            MERGE (program:Program {id: $program_id})
            SET program.name = coalesce($program_name, program.name),
                program.updated_at = timestamp()
            MERGE (college:College {id: $college_id})
            SET college.name = coalesce($college_name, college.name),
                college.updated_at = timestamp()
            MERGE (regulation:Regulation {id: $regulation_id})
            SET regulation.name = coalesce($regulation_name, regulation.name),
                regulation.updated_at = timestamp()
            MERGE (semester:Semester {number: $semester_number})
            SET semester.updated_at = timestamp()
            MERGE (st)-[:ENROLLED_IN]->(program)
            MERGE (st)-[:BELONGS_TO]->(college)
            MERGE (st)-[:HAS_REGULATION]->(regulation)
            MERGE (st)-[:IN_SEMESTER]->(semester)
            MERGE (program)-[:IN_COLLEGE]->(college)
            MERGE (program)-[:USES_REGULATION]->(regulation)
            MERGE (regulation)-[:HAS_SEMESTER]->(semester)
            """,
            student_id=student_id,
            email=email,
            college_id=college_id,
            regulation_id=regulation_id,
            program_id=program_id,
            semester_number=int(semester_number),
            program_name=program_name,
            college_name=college_name,
            regulation_name=regulation_name,
            created_at=created_at,
        )

        row = session.run(
            """
            MATCH (st:Student {id: $student_id})
            OPTIONAL MATCH (st)-[:ENROLLED_IN]->(program:Program)
            OPTIONAL MATCH (st)-[:BELONGS_TO]->(college:College)
            OPTIONAL MATCH (st)-[:HAS_REGULATION]->(regulation:Regulation)
            OPTIONAL MATCH (st)-[:IN_SEMESTER]->(semester:Semester)
            RETURN st.id AS id,
                   st.email AS email,
                   st.college_id AS college_id,
                   st.regulation_id AS regulation_id,
                   st.created_at AS created_at,
                   program.id AS program_id,
                   program.name AS program_name,
                   college.id AS college_node_id,
                   college.name AS college_name,
                   regulation.id AS regulation_node_id,
                   regulation.name AS regulation_name,
                   semester.number AS semester_number
            """,
            student_id=student_id,
        ).single()

        return dict(row) if row else {}


def get_student_profile(student_id: str) -> Optional[Dict[str, Any]]:
    driver = _get_driver()
    with driver.session() as session:
        row = session.run(
            """
            MATCH (st:Student {id: $student_id})
            OPTIONAL MATCH (st)-[:ENROLLED_IN]->(program:Program)
            OPTIONAL MATCH (st)-[:BELONGS_TO]->(college:College)
            OPTIONAL MATCH (st)-[:HAS_REGULATION]->(regulation:Regulation)
            OPTIONAL MATCH (st)-[:IN_SEMESTER]->(semester:Semester)
            RETURN st.id AS id,
                   st.email AS email,
                   st.college_id AS college_id,
                   st.regulation_id AS regulation_id,
                   st.created_at AS created_at,
                   program.id AS program_id,
                   program.name AS program_name,
                   college.id AS college_node_id,
                   college.name AS college_name,
                   regulation.id AS regulation_node_id,
                   regulation.name AS regulation_name,
                   semester.number AS semester_number
            """,
            student_id=student_id,
        ).single()
        return dict(row) if row else None


def close():
    global _driver
    if _driver:
        _driver.close()
        _driver = None


def regulation_exists(regulation_id: str) -> bool:
    """Return True if a Regulation node with the given id exists in the graph."""
    if not regulation_id:
        return False
    driver = _get_driver()
    query = """
    MATCH (r:Regulation {id: $regulation_id})
    RETURN r.id IS NOT NULL AS exists
    """
    with driver.session() as session:
        row = session.run(query, regulation_id=regulation_id).single()
        return bool(row and row["exists"])


def get_regulation_semesters(regulation_id: str) -> dict[int, list[dict[str, str]]]:
    """Return a dict mapping semester number -> list of subjects for a regulation.

    Each subject is a dict with keys `code`, `name`, and `credits` where available.
    """
    result: dict[int, list[dict[str, str]]] = {}
    if not regulation_id:
        return result
    driver = _get_driver()
    query = """
    MATCH (r:Regulation {id: $regulation_id})-[:HAS_SEMESTER]->(sem:Semester)<-[:BELONGS_TO]-(s:Subject)
    RETURN sem.number AS semester, s.code AS code, s.name AS name, s.credits AS credits
    ORDER BY sem.number, s.code
    """
    with driver.session() as session:
        rows = session.run(query, regulation_id=regulation_id)
        for row in rows:
            sem = row["semester"]
            if sem is None:
                continue
            entry = {
                "semester": int(sem),
                "code": row.get("code"),
                "name": row.get("name"),
                "credits": row.get("credits"),
            }
            result.setdefault(int(sem), []).append(entry)
    return result


def upsert_regulation_scope(regulation_id: str, regulation_name: str | None = None) -> None:
    """Create the Regulation node and attach every loaded Semester to it."""
    if not regulation_id:
        return
    driver = _get_driver()
    with driver.session() as session:
        session.run(
            """
            MERGE (r:Regulation {id: $regulation_id})
            SET r.name = coalesce($regulation_name, r.name),
                r.updated_at = timestamp()
            WITH r
            MATCH (sem:Semester)
            MERGE (r)-[:HAS_SEMESTER]->(sem)
            """,
            regulation_id=regulation_id,
            regulation_name=regulation_name,
        )