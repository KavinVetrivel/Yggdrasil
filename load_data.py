"""
Curriculum Knowledge Graph Loader
- Reads subjects.csv
- Loads predefined prerequisite relationships into Neo4j
- Parses syllabus pages from REGULATIONS.pdf for Unit and Topic nodes
- Loads everything into Neo4j local instance
"""

import csv
import json
import os
import re
from collections import defaultdict, deque
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
SYLLABUS_PDF_PATH = os.getenv("SYLLABUS_PDF_PATH", "REGULATIONS.pdf")
GEMINI_QUOTA_EXHAUSTED = False
_subject_resource_index = None

PREDEFINED_PREREQUISITES = [
    ("23N101", "23N201"),
    ("23N101", "23N301"),
    ("23N101", "23N302"),
    ("23N101", "23N401"),
    ("23N301", "23N401"),
    ("23N201", "23N302"),
    ("23N104", "23N204"),
    ("23N110", "23N204"),
    ("23N204", "23N303"),
    ("23N204", "23N212"),
    ("23N303", "23N403"),
    ("23N303", "23N311"),
    ("23N303", "23N701"),
    ("23N310", "23N511"),
    ("23N402", "23N410"),
    ("23N402", "23N701"),
    ("23N203", "23N404"),
    ("23N203", "23N211"),
    ("23N404", "23N602"),
    ("23N404", "23N503"),
    ("23N504", "23N510"),
    ("23N504", "23N601"),
    ("23N302", "23N405"),
    ("23N301", "23N405"),
    ("23N401", "23N405"),
    ("23N405", "23N411"),
    ("23N405", "23N501"),
    ("23N405", "23N502"),
    ("23N501", "23N603"),
    ("23N501", "23N610"),
    ("23N603", "23N604"),
    ("23N610", "23N604"),
    ("23N302", "23N502"),
    ("23N403", "23N502"),
    ("23N304", "23N720"),
    ("23N720", "23N820"),
]

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


def normalize_whitespace(text):
    return re.sub(r"\s+", " ", text or "").strip()


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower())
    slug = slug.strip("-")
    return slug or "item"


def load_syllabus_text(pdf_path):
    try:
        import pdfplumber
    except ImportError as error:
        raise RuntimeError("Install pdfplumber in this venv before loading syllabus topics.") from error

    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"Syllabus PDF not found: {pdf_path}")

    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages[6:]:
            pages.append(page.extract_text() or "")

    return "\n".join(pages)


def collect_subject_blocks(syllabus_text, subject_codes):
    ordered_codes = sorted(
        {code for code in subject_codes if code and code.strip()},
        key=len,
        reverse=True,
    )

    blocks = []
    current = None

    for raw_line in syllabus_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("====="):
            continue

        matched_code = None
        for code in ordered_codes:
            if line == code or line.startswith(f"{code} "):
                matched_code = code
                break

        if matched_code is not None:
            if current is not None:
                blocks.append(current)
            current = {"code": matched_code, "lines": [line]}
            continue

        if current is not None:
            current["lines"].append(line)

    if current is not None:
        blocks.append(current)

    return blocks


def parse_unit_blocks(subject_lines):
    content_lines = list(subject_lines)
    if len(content_lines) > 1 and re.fullmatch(r"[0-9 ]+", content_lines[1]):
        content_lines = content_lines[2:]
    else:
        content_lines = content_lines[1:]

    stop_markers = ("TEXT BOOK", "TEXTBOOK", "REFERENCES", "TOTAL")
    filtered_lines = []
    for line in content_lines:
        if any(line.upper().startswith(marker) for marker in stop_markers):
            break
        filtered_lines.append(line)

    units = []
    current = None

    for line in filtered_lines:
        heading = re.match(r"^(.{3,140}?):\s*(.*)$", line)
        if heading and not re.match(r"^\d+[\.)]\s*", line):
            if current is not None:
                units.append(current)
            current = {
                "title": normalize_whitespace(heading.group(1)),
                "body_lines": [normalize_whitespace(heading.group(2))] if heading.group(2).strip() else [],
            }
            continue

        if current is None:
            current = {"title": "Overview", "body_lines": []}

        current["body_lines"].append(line)

    if current is not None:
        units.append(current)

    return units


def extract_topics_from_unit(unit_body_lines):
    text = normalize_whitespace(" ".join(unit_body_lines))
    if not text:
        return []

    text = re.sub(r"\((?:\d+(?:\+\d+)?)\)$", "", text).strip()

    numbered_items = []
    current_item = None
    for line in unit_body_lines:
        stripped = normalize_whitespace(line)
        item_match = re.match(r"^\d+[\.)]\s*(.+)$", stripped)
        if item_match:
            if current_item:
                numbered_items.append(current_item)
            current_item = item_match.group(1)
            continue
        if current_item is not None and stripped:
            current_item = f"{current_item} {stripped}"
    if current_item:
        numbered_items.append(current_item)

    if numbered_items:
        cleaned_items = []
        for item in numbered_items:
            item = normalize_whitespace(item)
            item = re.sub(r"\((?:\d+(?:\+\d+)?)\)$", "", item).strip(" .-–—")
            if item:
                cleaned_items.append(item)
        return cleaned_items

    chunks = re.split(r"\s[–—]\s|\s-\s|\s;\s", text)
    topics = []
    seen = set()
    for chunk in chunks:
        cleaned = normalize_whitespace(chunk).strip(" .;:-–—")
        if not cleaned:
            continue
        if len(cleaned) < 3:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        topics.append(cleaned)
    return topics


def extract_topic_graph(subjects, pdf_path):
    subject_codes = [subject["code"] for subject in subjects]
    syllabus_text = load_syllabus_text(pdf_path)
    subject_blocks = collect_subject_blocks(syllabus_text, subject_codes)

    topic_graph = []
    for block in subject_blocks:
        units = parse_unit_blocks(block["lines"])
        parsed_units = []
        for unit_index, unit in enumerate(units, start=1):
            topics = extract_topics_from_unit(unit["body_lines"])
            parsed_units.append({
                "number": unit_index,
                "title": unit["title"],
                "topics": topics,
            })
        topic_graph.append({"code": block["code"], "units": parsed_units})

    print(f"[Syllabus] Parsed topic structure for {len(topic_graph)} subjects")
    return topic_graph


def get_gemini_api_keys():
    api_keys = []
    for env_name in (
        "GEMINI_API_KEY",
        "GEMINI_API_KEY1",
        "GEMINI_API_KEY2",
        "GEMINI_API_KEY3",
        "GEMINI_API_KEY4",
        "GEMINI_API_KEY5",
        "GEMINI_API_KEY6",
    ):
        api_key = os.getenv(env_name)
        if api_key and api_key != "your_gemini_api_key_here" and api_key not in api_keys:
            api_keys.append(api_key)
    return api_keys


def call_gemini(prompt, label, max_output_tokens=1000):
    global GEMINI_QUOTA_EXHAUSTED

    model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max_output_tokens,
            "temperature": 0,
        },
    }

    api_keys = get_gemini_api_keys()
    if not api_keys:
        print(f"[API] Set GEMINI_API_KEY or GEMINI_API_KEY1 through GEMINI_API_KEY6 before {label}.")
        return None

    def is_usage_limit_error(error_body):
        lowered = error_body.lower()
        return (
            "quota" in lowered
            or "usage limit" in lowered
            or "rate limit" in lowered
            or "resource_exhausted" in lowered
        )

    raw_response = None
    raw = None

    for index, api_key in enumerate(api_keys, start=1):
        print(f"[API] Calling Gemini with key {index} of {len(api_keys)} to {label}...")

        endpoint = (
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            f"?key={api_key}"
        )

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
            if error.code in {429, 403} and is_usage_limit_error(error_body) and index < len(api_keys):
                print(f"[API] Key {index} hit usage limit; trying next API key.")
                continue
            print(f"[API] Error: {error.code} — {error_body}")
            if error.code in {429, 403} and is_usage_limit_error(error_body):
                GEMINI_QUOTA_EXHAUSTED = True
            return None
        except URLError as error:
            print(f"[API] Error: {error.reason}")
            return None

        try:
            response_data = json.loads(raw_response)
            raw = response_data["candidates"][0]["content"]["parts"][0]["text"].strip()
            break
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            print(f"[API] Unexpected Gemini response: {error}")
            print(f"[API] Raw response:\n{raw_response}")
            return None

    if raw is None:
        if not api_keys:
            print("[API] No Gemini API keys configured; skipping Gemini call.")
            return None
        GEMINI_QUOTA_EXHAUSTED = True
        return None

    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    return raw.strip()


def build_topic_index(topic_graph):
    return {entry["code"]: entry["units"] for entry in topic_graph}


def format_subject_topic_summary(subject, units):
    lines = [f"{subject['code']} | {subject['name']} | Semester {subject['semester']}"]
    for unit in units:
        topic_names = unit["topics"]
        if not topic_names:
            continue
        topics_text = ", ".join(topic_names)
        lines.append(f"Unit {unit['number']} - {unit['title']}: {topics_text}")
    return "\n".join(lines)


def build_related_subject_targets(prerequisites, subject_codes, max_depth=2):
    adjacency = defaultdict(set)
    for pair in prerequisites:
        source = pair.get("prerequisite", "").strip()
        target = pair.get("dependent", "").strip()
        if source and target:
            adjacency[source].add(target)

    subject_set = set(subject_codes)
    targets_by_source = defaultdict(set)

    for source in subject_set:
        queue = deque([(source, 0)])
        visited = {source}
        while queue:
            current, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for next_code in adjacency.get(current, set()):
                if next_code in visited:
                    continue
                visited.add(next_code)
                next_depth = depth + 1
                if next_code in subject_set and next_code != source:
                    targets_by_source[source].add(next_code)
                queue.append((next_code, next_depth))

    return {source: sorted(targets) for source, targets in targets_by_source.items()}


def canonical_topic_token(token):
    token = token.lower()
    token = re.sub(r"[^a-z0-9]", "", token)
    if not token:
        return ""

    prefix_map = {
        "bayes": "bayes",
        "markov": "markov",
        "hidden": "hidden",
        "network": "network",
        "database": "database",
        "transform": "transform",
        "convol": "convolution",
        "recur": "recurrent",
        "neural": "neural",
        "probab": "probability",
        "classif": "classification",
        "optim": "optimization",
        "graph": "graph",
        "cluster": "cluster",
        "security": "security",
        "privacy": "privacy",
        "parallel": "parallel",
        "distributed": "distributed",
        "search": "search",
        "learning": "learning",
        "regress": "regression",
        "deep": "deep",
        "model": "model",
        "algorithm": "algorithm",
    }

    for prefix, canonical in prefix_map.items():
        if token.startswith(prefix):
            return canonical

    if token.endswith("ies") and len(token) > 3:
        return token[:-3] + "y"
    if token.endswith("es") and len(token) > 3:
        return token[:-2]
    if token.endswith("s") and len(token) > 3:
        return token[:-1]
    return token


def topic_keywords(topic_name):
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9+-]*", topic_name)
    return {canonical_topic_token(token) for token in tokens if canonical_topic_token(token)}


def score_topic_relation(source_topic, target_topic):
    source_lower = source_topic.lower()
    target_lower = target_topic.lower()

    if source_lower == target_lower:
        return 0

    if source_lower in target_lower or target_lower in source_lower:
        return 4

    source_keywords = topic_keywords(source_topic)
    target_keywords = topic_keywords(target_topic)
    overlap = source_keywords & target_keywords

    if overlap:
        return 3 + min(len(overlap), 2)

    pair_rules = [
        ("markov", {"hmm", "hidden", "bayes", "bayesian"}),
        ("bayes", {"bayes", "bayesian", "probability", "network"}),
        ("neural", {"deep", "learning", "cnn", "rnn", "transform"}),
        ("convolution", {"cnn", "deep", "learning", "network"}),
        ("database", {"sql", "mongodb", "neo4j", "big", "data"}),
        ("graph", {"graph", "network", "search"}),
        ("optimization", {"gradient", "descent", "learning", "model"}),
        ("parallel", {"distributed", "gpu", "mpi", "openmp"}),
        ("security", {"privacy", "cryptography", "authentication", "network"}),
        ("search", {"dfs", "bfs", "heuristic", "algorithm"}),
        ("tree", {"decision", "classification", "regression"}),
    ]

    for anchor, companions in pair_rules:
        if anchor in source_keywords and target_keywords & companions:
            return 2
        if anchor in target_keywords and source_keywords & companions:
            return 2

    return 0


def infer_related_topics_fallback(subjects, prerequisites, topic_graph):
    subject_lookup = {subject["code"]: subject for subject in subjects}
    topic_index = build_topic_index(topic_graph)
    target_map = build_related_subject_targets(prerequisites, topic_index.keys(), max_depth=2)

    related_pairs = []
    seen_pairs = set()

    for source_code in sorted(target_map):
        source_subject = subject_lookup.get(source_code)
        source_units = topic_index.get(source_code, [])
        if not source_subject or not source_units:
            continue

        target_codes = [code for code in target_map[source_code] if code in topic_index]
        if not target_codes:
            continue

        for target_code in target_codes:
            if target_code == source_code:
                continue

            best_pairs = []
            target_units = topic_index.get(target_code, [])
            for source_unit in source_units:
                for source_topic in source_unit["topics"]:
                    for target_unit in target_units:
                        for target_topic in target_unit["topics"]:
                            score = score_topic_relation(source_topic, target_topic)
                            if score > 0:
                                best_pairs.append((score, source_topic, target_topic))

            best_pairs.sort(key=lambda item: (-item[0], item[1].lower(), item[2].lower()))
            for score, source_topic, target_topic in best_pairs[:3]:
                pair_key = (source_code, source_topic.lower(), target_code, target_topic.lower())
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                related_pairs.append({
                    "source_subject": source_code,
                    "source_topic": source_topic,
                    "target_subject": target_code,
                    "target_topic": target_topic,
                })

    print(f"[Fallback] Inferred {len(related_pairs)} cross-subject topic relationships locally")
    return related_pairs


def infer_related_topics(subjects, prerequisites, topic_graph):
    if GEMINI_QUOTA_EXHAUSTED:
        print("[API] Gemini quota exhausted; using local heuristic topic matching.")
        return infer_related_topics_fallback(subjects, prerequisites, topic_graph)

    subject_lookup = {subject["code"]: subject for subject in subjects}
    topic_index = build_topic_index(topic_graph)
    target_map = build_related_subject_targets(prerequisites, topic_index.keys(), max_depth=2)

    related_pairs = []
    seen_pairs = set()

    for source_code in sorted(target_map):
        source_subject = subject_lookup.get(source_code)
        source_units = topic_index.get(source_code, [])
        if not source_subject or not source_units:
            continue

        target_codes = [code for code in target_map[source_code] if code in topic_index]
        if not target_codes:
            continue

        source_summary = format_subject_topic_summary(source_subject, source_units)
        target_summaries = []
        for target_code in target_codes:
            target_subject = subject_lookup.get(target_code)
            if not target_subject:
                continue
            target_summaries.append(format_subject_topic_summary(target_subject, topic_index[target_code]))

        if not target_summaries:
            continue

        prompt = f"""You are building RELATED_TO edges between Topic nodes in a curriculum knowledge graph.

Infer only strong conceptual relationships between topics in DIFFERENT subjects.
Use only the exact topic names shown below. Do not invent topics, do not paraphrase, and do not return within-subject pairs.

Return ONLY a JSON array, no explanation, no markdown, no extra text.
Each object must have exactly: "source_subject", "source_topic", "target_subject", "target_topic".

Prefer specific conceptual links such as one topic feeding into, extending, or directly applying another topic.
Limit to at most 5 relationships total for this source subject.

Source subject:
{source_summary}

Candidate target subjects:
{chr(10).join(target_summaries)}
"""

        raw = call_gemini(prompt, f"infer topic links for {source_code}", max_output_tokens=1400)
        if raw is None:
            if GEMINI_QUOTA_EXHAUSTED:
                return infer_related_topics_fallback(subjects, prerequisites, topic_graph)
            continue

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            print(f"[API] JSON parse failed for topic links from {source_code}: {error}")
            print(f"[API] Raw response:\n{raw}")
            continue

        if not isinstance(parsed, list):
            continue

        for item in parsed:
            if not isinstance(item, dict):
                continue

            source_subject_code = normalize_whitespace(str(item.get("source_subject", "")))
            source_topic_name = normalize_whitespace(str(item.get("source_topic", "")))
            target_subject_code = normalize_whitespace(str(item.get("target_subject", "")))
            target_topic_name = normalize_whitespace(str(item.get("target_topic", "")))

            if not source_subject_code or not source_topic_name or not target_subject_code or not target_topic_name:
                continue
            if source_subject_code == target_subject_code:
                continue
            if source_subject_code not in topic_index or target_subject_code not in topic_index:
                continue

            pair_key = (
                source_subject_code,
                source_topic_name.lower(),
                target_subject_code,
                target_topic_name.lower(),
            )
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)
            related_pairs.append({
                "source_subject": source_subject_code,
                "source_topic": source_topic_name,
                "target_subject": target_subject_code,
                "target_topic": target_topic_name,
            })

    print(f"[LLM] Inferred {len(related_pairs)} cross-subject topic relationships")
    return related_pairs


# ─────────────────────────────────────────────
# STEP 2: Infer prerequisites via Gemini API
# ─────────────────────────────────────────────
def infer_prerequisites(subjects):
    if PREDEFINED_PREREQUISITES:
        print(f"[Prerequisites] Using {len(PREDEFINED_PREREQUISITES)} predefined relationships")
        return [
            {"prerequisite": prerequisite, "dependent": dependent}
            for prerequisite, dependent in PREDEFINED_PREREQUISITES
        ]

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

    raw = call_gemini(prompt, "infer prerequisites", max_output_tokens=1000)
    if raw is None:
        return []

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
def load_into_neo4j(subjects, prerequisites, topic_graph):
    if GraphDatabase is None:
        raise RuntimeError("Install the neo4j driver in this venv before loading data: c:/vscode/Yggdrasil/.venv/Scripts/python.exe -m pip install neo4j")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    related_topics = infer_related_topics(subjects, prerequisites, topic_graph)

    with driver.session() as session:

        # Constraints
        print("[Neo4j] Creating constraints...")
        session.run("CREATE CONSTRAINT subject_code IF NOT EXISTS FOR (s:Subject) REQUIRE s.code IS UNIQUE")
        session.run("CREATE CONSTRAINT semester_num IF NOT EXISTS FOR (sem:Semester) REQUIRE sem.number IS UNIQUE")
        session.run("CREATE CONSTRAINT unit_key IF NOT EXISTS FOR (u:Unit) REQUIRE u.key IS UNIQUE")
        session.run("CREATE CONSTRAINT topic_key IF NOT EXISTS FOR (t:Topic) REQUIRE t.key IS UNIQUE")
        session.run("CREATE INDEX topic_lookup IF NOT EXISTS FOR (t:Topic) ON (t.subject_code, t.name_normalized)")

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

        # Units and topics
        print(f"[Neo4j] Loading topic graph for {len(topic_graph)} subjects...")
        unit_count = 0
        topic_count = 0
        for subject_topics in topic_graph:
            subject_code = subject_topics["code"]
            for unit in subject_topics["units"]:
                unit_key = f"{subject_code}:{unit['number']}"
                unit_title = normalize_whitespace(unit["title"])

                session.run(
                    """
                    MATCH (sub:Subject {code: $subject_code})
                    MERGE (u:Unit {key: $unit_key})
                    SET u.subject_code = $subject_code,
                        u.number = $unit_number,
                        u.title = $unit_title
                    MERGE (sub)-[:HAS_UNIT]->(u)
                    """,
                    subject_code=subject_code,
                    unit_key=unit_key,
                    unit_number=unit["number"],
                    unit_title=unit_title,
                )
                unit_count += 1

                for topic_name in unit["topics"]:
                    topic_key = f"{unit_key}:{slugify(topic_name)}"
                    topic_name_normalized = normalize_whitespace(topic_name).lower()
                    session.run(
                        """
                        MATCH (sub:Subject {code: $subject_code})
                        MATCH (u:Unit {key: $unit_key})
                        MERGE (t:Topic {key: $topic_key})
                        SET t.subject_code = $subject_code,
                            t.unit_key = $unit_key,
                            t.name = $topic_name,
                            t.name_normalized = $topic_name_normalized
                        MERGE (sub)-[:HAS_TOPIC]->(t)
                        MERGE (t)-[:PART_OF_UNIT]->(u)
                        """,
                        subject_code=subject_code,
                        unit_key=unit_key,
                        topic_key=topic_key,
                        topic_name=topic_name,
                        topic_name_normalized=topic_name_normalized,
                    )
                    topic_count += 1

        print(f"  Loaded: {unit_count} units | {topic_count} topics")

        # Cross-subject topic relationships
        print(f"[Neo4j] Loading {len(related_topics)} RELATED_TO relationships...")
        related_loaded = 0
        related_skipped = 0
        for relation in related_topics:
            source_subject = relation.get("source_subject", "").strip()
            source_topic = relation.get("source_topic", "").strip()
            target_subject = relation.get("target_subject", "").strip()
            target_topic = relation.get("target_topic", "").strip()
            source_topic_normalized = normalize_whitespace(source_topic).lower()
            target_topic_normalized = normalize_whitespace(target_topic).lower()

            if not source_subject or not source_topic or not target_subject or not target_topic:
                related_skipped += 1
                continue

            result = session.run(
                """
                MATCH (source:Topic {subject_code: $source_subject, name_normalized: $source_topic_normalized})
                MATCH (target:Topic {subject_code: $target_subject, name_normalized: $target_topic_normalized})
                MERGE (source)-[r:RELATED_TO]->(target)
                RETURN r
                """,
                source_subject=source_subject,
                source_topic_normalized=source_topic_normalized,
                target_subject=target_subject,
                target_topic_normalized=target_topic_normalized,
            )

            if result.single():
                related_loaded += 1
            else:
                related_skipped += 1

        print(f"  Loaded: {related_loaded} | Skipped: {related_skipped}")

    driver.close()
    print("[Neo4j] Done.")


# ─────────────────────────────────────────────
# STEP 4: Verify
# ─────────────────────────────────────────────
def verify(subjects, prerequisites, topic_graph):
    if GraphDatabase is None:
        raise RuntimeError("Install the neo4j driver in this venv before verifying data: c:/vscode/Yggdrasil/.venv/Scripts/python.exe -m pip install neo4j")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session() as session:
        total = session.run("MATCH (s:Subject) RETURN count(s) AS n").single()['n']
        rels  = session.run("MATCH ()-[r:PREREQUISITE_OF]->() RETURN count(r) AS n").single()['n']
        units = session.run("MATCH (u:Unit) RETURN count(u) AS n").single()['n']
        topics = session.run("MATCH (t:Topic) RETURN count(t) AS n").single()['n']
        related = session.run("MATCH ()-[r:RELATED_TO]->() RETURN count(r) AS n").single()['n']
        print(f"\n[Verify] Subjects in DB : {total}")
        print(f"[Verify] Prerequisites  : {rels}")
        print(f"[Verify] Units          : {units}")
        print(f"[Verify] Topics         : {topics}")
        print(f"[Verify] Related topics : {related}")

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

        print("\n[Verify] Topic relationships:")
        result = session.run("""
            MATCH (source:Topic)-[:RELATED_TO]->(target:Topic)
            RETURN source.subject_code AS from_subject, source.name AS from_topic,
                   target.subject_code AS to_subject, target.name AS to_topic
            ORDER BY from_subject, from_topic
            LIMIT 20
        """)
        for row in result:
            print(f"  {row['from_subject']} :: {row['from_topic']} -> {row['to_subject']} :: {row['to_topic']}")

    driver.close()


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
if __name__ == '__main__':
    subjects      = load_subjects_from_csv(CSV_PATH)
    prerequisites = infer_prerequisites(subjects)
    topic_graph   = extract_topic_graph(subjects, SYLLABUS_PDF_PATH)
    load_into_neo4j(subjects, prerequisites, topic_graph)
    verify(subjects, prerequisites, topic_graph)
