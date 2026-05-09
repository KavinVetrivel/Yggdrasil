"""
Curriculum Knowledge Graph Loader
- Reads subjects.csv
- Uses Gemini API to infer prerequisite relationships
- Loads everything into Neo4j local instance
"""

import csv
import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from neo4j import GraphDatabase
except ImportError:
    GraphDatabase = None

# ─────────────────────────────────────────────
# CONFIG — update these
# ─────────────────────────────────────────────
def load_env_file(path=".env"):
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

NEO4J_URI      = os.getenv("NEO4J_URI")
NEO4J_USER     = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
CSV_PATH       = os.getenv("CSV_PATH", "subjects.csv")         # path to your subjects.csv

# ─────────────────────────────────────────────
# STEP 1: Read subjects from CSV
# ─────────────────────────────────────────────
def load_subjects_from_csv(path):
    subjects = []
    with open(path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            subjects.append({
                'code':         row['code'],
                'name':         row['name'],
                'semester':     int(row['semester']),
                'lecture_hrs':  int(row['lecture_hrs']),
                'tutorial_hrs': int(row['tutorial_hrs']),
                'practical_hrs':int(row['practical_hrs']),
                'credits':      int(row['credits']),
                'category':     row['category'],
                'type':         row['type'],
            })
    print(f"[CSV] Loaded {len(subjects)} subjects")
    return subjects


# ─────────────────────────────────────────────
# STEP 2: Infer prerequisites via Gemini API
# ─────────────────────────────────────────────
def infer_prerequisites(subjects):
    # Build a compact subject list for the prompt
    subject_list = "\n".join(
        f"Sem{s['semester']} | {s['code']} | {s['name']} | {s['category']}"
        for s in subjects
        if s['category'] not in ('MC',)   # skip mandatory non-academic courses
    )

    prompt = f"""You are an expert in computer science curriculum design.

Below is the complete subject list for a BE CSE (AI & ML) 8-semester program.
Format: Semester | Code | Name | Category

{subject_list}

Task: Identify prerequisite relationships between these subjects.
A prerequisite means: a student should ideally complete subject A before taking subject B,
because B builds directly on A's concepts.

Rules:
- Only use subject codes from the list above.
- A prerequisite must be in an EARLIER semester than the dependent subject.
- Be strict — only add a relationship if there is a clear conceptual dependency.
- Do NOT add relationships for general foundational subjects like calculus or English.
- Focus on core CS/AI/ML subject chains.

Return ONLY a JSON array, no explanation, no markdown, no extra text.
Each object must have exactly: "prerequisite" and "dependent" as subject codes.

Example format:
[
  {{"prerequisite": "23N303", "dependent": "23N403"}},
  {{"prerequisite": "23N405", "dependent": "23N501"}}
]"""

    api_key = os.getenv("GEMINI_API_KEY", "your_gemini_api_key_here")
    model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

    if not api_key or api_key == "your_gemini_api_key_here":
        print("[API] Set GEMINI_API_KEY before running the loader.")
        return []

    print("[API] Calling Gemini to infer prerequisites...")

    endpoint = (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        f"?key={api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 1000,
            "temperature": 0,
        },
    }

    request = Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=60) as response:
            raw_response = response.read().decode("utf-8")
    except HTTPError as error:
        error_body = error.read().decode("utf-8", errors="replace")
        print(f"[API] Error: {error.code} — {error_body}")
        return []
    except URLError as error:
        print(f"[API] Error: {error.reason}")
        return []

    try:
        response_data = json.loads(raw_response)
        raw = response_data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        print(f"[API] Unexpected Gemini response: {error}")
        print(f"[API] Raw response:\n{raw_response}")
        return []

    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    try:
        pairs = json.loads(raw)
        print(f"[API] Got {len(pairs)} prerequisite relationships")
        return pairs
    except json.JSONDecodeError as e:
        print(f"[API] JSON parse failed: {e}")
        print(f"[API] Raw response:\n{raw}")
        return []


# ─────────────────────────────────────────────
# STEP 3: Load into Neo4j
# ─────────────────────────────────────────────
def load_into_neo4j(subjects, prerequisites):
    if GraphDatabase is None:
        raise RuntimeError("Install the neo4j driver in this venv before loading data: c:/vscode/Yggdrasil/.venv/Scripts/python.exe -m pip install neo4j")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    with driver.session() as session:

        # Constraints
        print("[Neo4j] Creating constraints...")
        session.run("CREATE CONSTRAINT subject_code IF NOT EXISTS FOR (s:Subject) REQUIRE s.code IS UNIQUE")
        session.run("CREATE CONSTRAINT semester_num IF NOT EXISTS FOR (sem:Semester) REQUIRE sem.number IS UNIQUE")

        # Program
        print("[Neo4j] Creating Program node...")
        session.run("""
            MERGE (p:Program {code: 'CSE_AIML'})
            SET p.name       = 'BE Computer Science and Engineering (AI&ML)',
                p.regulation = '2023',
                p.total_credits = 166
        """)

        # Semesters
        print("[Neo4j] Creating Semester nodes...")
        for n in range(1, 9):
            session.run("""
                MERGE (sem:Semester {number: $n})
                WITH sem
                MATCH (p:Program {code: 'CSE_AIML'})
                MERGE (sem)-[:PART_OF]->(p)
            """, n=n)

        # Subjects
        print(f"[Neo4j] Loading {len(subjects)} subjects...")
        for s in subjects:
            session.run("""
                MERGE (sub:Subject {code: $code})
                SET sub.name          = $name,
                    sub.semester      = $semester,
                    sub.lecture_hrs   = $lecture_hrs,
                    sub.tutorial_hrs  = $tutorial_hrs,
                    sub.practical_hrs = $practical_hrs,
                    sub.credits       = $credits,
                    sub.category      = $category,
                    sub.type          = $type
                WITH sub
                MATCH (sem:Semester {number: $semester})
                MERGE (sub)-[:BELONGS_TO]->(sem)
            """, **s)

        # Prerequisites
        print(f"[Neo4j] Loading {len(prerequisites)} prerequisite relationships...")
        loaded = 0
        skipped = 0
        for pair in prerequisites:
            pre_code = pair.get('prerequisite', '').strip()
            dep_code = pair.get('dependent', '').strip()

            if not pre_code or not dep_code:
                skipped += 1
                continue

            result = session.run("""
                MATCH (pre:Subject {code: $pre_code})
                MATCH (dep:Subject {code: $dep_code})
                MERGE (pre)-[r:PREREQUISITE_OF]->(dep)
                RETURN r
            """, pre_code=pre_code, dep_code=dep_code)

            if result.single():
                loaded += 1
            else:
                print(f"  [WARN] Skipped ({pre_code} -> {dep_code}): one or both codes not found")
                skipped += 1

        print(f"  Loaded: {loaded} | Skipped: {skipped}")

    driver.close()
    print("[Neo4j] Done.")


# ─────────────────────────────────────────────
# STEP 4: Verify
# ─────────────────────────────────────────────
def verify(subjects, prerequisites):
    if GraphDatabase is None:
        raise RuntimeError("Install the neo4j driver in this venv before verifying data: c:/vscode/Yggdrasil/.venv/Scripts/python.exe -m pip install neo4j")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session() as session:
        total = session.run("MATCH (s:Subject) RETURN count(s) AS n").single()['n']
        rels  = session.run("MATCH ()-[r:PREREQUISITE_OF]->() RETURN count(r) AS n").single()['n']
        print(f"\n[Verify] Subjects in DB : {total}")
        print(f"[Verify] Prerequisites  : {rels}")

        print("\n[Verify] Subjects per semester:")
        result = session.run("""
            MATCH (s:Subject)-[:BELONGS_TO]->(sem:Semester)
            RETURN sem.number AS semester, count(s) AS count
            ORDER BY semester
        """)
        for row in result:
            print(f"  Semester {row['semester']}: {row['count']} subjects")

        print("\n[Verify] Prerequisite chains:")
        result = session.run("""
            MATCH (pre:Subject)-[:PREREQUISITE_OF]->(dep:Subject)
            RETURN pre.code AS from_code, pre.name AS from_name,
                   dep.code AS to_code, dep.name AS to_name
            ORDER BY pre.semester
        """)
        for row in result:
            print(f"  {row['from_code']} ({row['from_name'][:30]}) -> {row['to_code']} ({row['to_name'][:30]})")

    driver.close()


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
if __name__ == '__main__':
    subjects      = load_subjects_from_csv(CSV_PATH)
    prerequisites = infer_prerequisites(subjects)
    load_into_neo4j(subjects, prerequisites)
    verify(subjects, prerequisites)
