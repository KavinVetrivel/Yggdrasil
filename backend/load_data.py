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
import time
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
_GROQ_UNAVAILABLE = False
_GROQ_UNAVAILABLE_REASON = ""
_LAST_GROQ_REQUEST_AT = 0.0
GROQ_REQUEST_DELAY_SECONDS = float(os.getenv("GROQ_REQUEST_DELAY_SECONDS", "1.5"))
GROQ_MAX_PAGE_ROWS = int(os.getenv("GROQ_MAX_PAGE_ROWS", "100"))
GROQ_MAX_BLOCK_CHARS = int(os.getenv("GROQ_MAX_BLOCK_CHARS", "8000"))

# Matches common Indian university subject code formats:
#   23N101  (your current format)
#   GE3151  (Anna University style)
#   22MAT11 (Amrita/VTU style)
#   BCS301  (VTU 2021 scheme)
#   CS6301  (older Anna University)
SUBJECT_CODE_RE = re.compile(
    r"(?P<code>"
    r"[A-Z]{1,4}\d{4,6}"          # GE3151, CS6301, BCS301
    r"|[A-Z]{1,3}\d{2}[A-Z]{2,4}\d{2,3}"  # 22MAT11-style (starts alpha)
    r"|\d{2}[A-Z]{1,4}\d{2,4}"    # 23N101, 21CS42
    r"|\d{2}[A-Z]{2,6}\d{1,3}"    # 22MAT11
    r")"
    r"\b",
    re.ASCII
)
SEMESTER_HEADER_PATTERNS = (
    re.compile(r"^\s*(?:semester|sem)\s*[-:–—]?\s*([ivx]+|\d{1,2})\b", re.I),
    re.compile(r"^\s*([ivx]+|\d{1,2})\s*(?:st|nd|rd|th)?\s+semester\b", re.I),
    re.compile(r"^\s*semester\s+([ivx]+|\d{1,2})\b", re.I),
    re.compile(r"^\s*year\s*[-:–]?\s*\d+\s*[-:–]?\s*(?:odd|even)?\s*sem(?:ester)?\s*([ivx]+|\d{1,2})\b", re.I),
    re.compile(r"^\s*\d+\s*[-:–]\s*([ivx]+|\d{1,2})\s+sem(?:ester)?\b", re.I),
)
SEMESTER_TYPE_VALUES = {"theory", "practical", "lab", "laboratory"}
SUBJECT_CATEGORY_VALUES = {"BS", "ES", "HS", "MC", "PC", "EEC", "PE", "OE", "SC"}
ROMAN_SEMESTERS = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}


def get_groq_api_key():
    for env_name in ("GROQ_API_KEY", "GROQ_API", "GROQ_API1", "GROQ_API2"):
        api_key = os.getenv(env_name, "")
        if api_key and api_key != "your_groq_api_key_here":
            return api_key
    return ""


def get_openrouter_api_key():
    for env_name in (
        "OPENROUTER_API_KEY",
        "OPENROUTER_API_KEY1",
        "OPENROUTER_API_KEY2",
        "OPENROUTER_API_KEY_2",
        "OPENROUTER_API_KEY_ALT",
        "OPENROUTER_API_KEY_BACKUP",
    ):
        api_key = os.getenv(env_name, "")
        if api_key and api_key != "your_openrouter_api_key_here":
            return api_key

    combined = os.getenv("OPENROUTER_API_KEYS", "")
    if combined:
        for api_key in (part.strip() for part in combined.split(",")):
            if api_key and api_key != "your_openrouter_api_key_here":
                return api_key
    return ""

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


def normalize_subject_code(value):
    return re.sub(r"\s+", "", (value or "").strip().upper())


def parse_prerequisite_codes(value):
    if not value:
        return []
    if isinstance(value, str):
        raw_values = re.split(r"[;,\n]+", value)
    else:
        raw_values = value

    codes = []
    seen = set()
    for raw_value in raw_values:
        code = normalize_subject_code(raw_value)
        if not code or code in seen:
            continue
        seen.add(code)
        codes.append(code)
    return codes

# ─────────────────────────────────────────────
# STEP 1: Read subjects from CSV
# ─────────────────────────────────────────────
def load_subjects_from_csv(path):
    subjects = []
    with open(path, newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row_index, row in enumerate(reader, start=2):
            try:
                prerequisites = parse_prerequisite_codes(row.get('prerequisites', ''))
                subjects.append({
                    'code':         row['code'],
                    'name':         row['name'],
                    'semester':     int(row['semester']),
                    'lecture_hrs':  int(row['lecture_hrs'] or 0),
                    'tutorial_hrs': int(row['tutorial_hrs'] or 0),
                    'practical_hrs':int(row['practical_hrs'] or 0),
                    'credits':      int(row['credits'] or 0),
                    'category':     row['category'],
                    'type':         row['type'],
                    'prerequisites': prerequisites,
                })
            except KeyError as e:
                col = e.args[0] if e.args else '<unknown>'
                raise ValueError(f"Malformed CSV at row {row_index}: missing column {col!r}; row snippet: {row}") from e
            except ValueError as e:
                # Try to identify the offending numeric column for clearer errors
                offending = None
                for col in ('semester', 'lecture_hrs', 'tutorial_hrs', 'practical_hrs', 'credits'):
                    val = row.get(col)
                    try:
                        if col == 'semester':
                            int(val)
                        else:
                            int(val or 0)
                    except Exception:
                        offending = (col, val)
                        break
                if offending:
                    col, val = offending
                    raise ValueError(f"Malformed CSV at row {row_index}: invalid value for column {col!r}: {val!r}; row snippet: {row}") from e
                raise ValueError(f"Malformed CSV at row {row_index}: {e}; row snippet: {row}") from e
    print(f"[CSV] Loaded {len(subjects)} subjects")
    return subjects


def _pair_from_codes(prerequisite, dependent):
    return {"prerequisite": prerequisite, "dependent": dependent}


def collect_manual_prerequisites(subjects):
    pairs = []
    for subject in subjects:
        dependent = subject.get("code", "").strip()
        for prerequisite in subject.get("prerequisites", []):
            pairs.append(_pair_from_codes(prerequisite, dependent))
    return pairs


def merge_valid_prerequisites(subjects, *sources):
    sem_map = {s["code"]: s["semester"] for s in subjects}
    code_set = set(sem_map.keys())
    valid = []
    seen = set()

    for source in sources:
        for item in source:
            pre = str(item.get("prerequisite", "")).strip().upper()
            dep = str(item.get("dependent", "")).strip().upper()
            if pre in code_set and dep in code_set and sem_map[pre] < sem_map[dep] and (pre, dep) not in seen:
                valid.append({"prerequisite": pre, "dependent": dep})
                seen.add((pre, dep))

    return valid


def normalize_whitespace(text):
    return re.sub(r"\s+", " ", text or "").strip()


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower())
    slug = slug.strip("-")
    return slug or "item"


def load_pdf_pages(pdf_path):
    try:
        import pdfplumber
    except ImportError as error:
        raise RuntimeError("Install pdfplumber in this venv before loading syllabus topics.") from error

    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"Syllabus PDF not found: {pdf_path}")

    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            pages.append(page.extract_text() or "")

    return pages


def load_pdf_text(pdf_path):
    return "\n".join(load_pdf_pages(pdf_path))


def compact_prompt_text(text, max_chars):
    normalized = normalize_whitespace(text)
    if not normalized or max_chars <= 0:
        return normalized
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rsplit(" ", 1)[0].strip() or normalized[:max_chars]


def find_syllabus_start_page(pages):
    """
    Dynamically detect which page the actual syllabus content starts on.
    Looks for the first page that has both a subject code and a unit/module heading.
    Falls back to page 0 if nothing matches.
    """
    unit_pattern = re.compile(r"\b(UNIT|MODULE)\b", re.I)
    for i, page_text in enumerate(pages):
        has_code = bool(SUBJECT_CODE_RE.search(page_text))
        has_unit = bool(unit_pattern.search(page_text))
        if has_code and has_unit:
            # Step back one page to avoid cutting off the first subject header
            return max(0, i - 1)

    # Fallback: look for just a subject code (curriculum table pages)
    for i, page_text in enumerate(pages):
        if SUBJECT_CODE_RE.search(page_text):
            return max(0, i - 1)

    return 0


def load_syllabus_text(pdf_path):
    pages = load_pdf_pages(pdf_path)
    start = find_syllabus_start_page(pages)
    return "\n".join(pages[start:])


def parse_semester_value(raw_value):
    value = normalize_whitespace(raw_value).lower()
    if not value:
        return None

    if value.isdigit():
        semester = int(value)
    else:
        semester = ROMAN_SEMESTERS.get(value)
        if semester is None:
            return None

    if 1 <= semester <= 10:
        return semester
    return None


def detect_semester_from_line(line):
    cleaned = normalize_whitespace(line)
    for pattern in SEMESTER_HEADER_PATTERNS:
        match = pattern.search(cleaned)
        if match:
            semester = parse_semester_value(match.group(1))
            if semester is not None:
                return semester
    return None


def normalize_subject_record(item):
    if not isinstance(item, dict):
        return None

    code = normalize_whitespace(str(item.get("code", ""))).upper()
    name = normalize_whitespace(str(item.get("name", "")))
    category = normalize_whitespace(str(item.get("category", ""))).upper()

    try:
        semester = int(item.get("semester"))
        lecture_hrs = int(float(item.get("lecture_hrs", 0)))
        tutorial_hrs = int(float(item.get("tutorial_hrs", 0)))
        practical_hrs = int(float(item.get("practical_hrs", 0)))
        credits = int(float(item.get("credits", 0)))
    except (TypeError, ValueError):
        return None

    if not code or not name or semester < 1 or semester > 10:
        return None

    if not category:
        category = "UNKNOWN"
    if category != "UNKNOWN" and category not in SUBJECT_CATEGORY_VALUES:
        category = "UNKNOWN"

    subject_type = normalize_whitespace(str(item.get("type", "")))
    if subject_type.lower() not in SEMESTER_TYPE_VALUES:
        subject_type = "Practical" if practical_hrs > 0 else "Theory"

    return {
        "code": code,
        "name": name,
        "semester": semester,
        "lecture_hrs": lecture_hrs,
        "tutorial_hrs": tutorial_hrs,
        "practical_hrs": practical_hrs,
        "credits": credits,
        "category": category,
        "type": subject_type,
    }


def extract_subjects_with_groq(pdf_path):
    api_key = get_groq_api_key() or get_openrouter_api_key()
    if not api_key or _GROQ_UNAVAILABLE:
        return []

    subjects = []
    seen = set()

    try:
        import pdfplumber
    except ImportError:
        return []

    with pdfplumber.open(pdf_path) as pdf:
        for page_index, page in enumerate(pdf.pages, start=1):
            page_rows = collect_page_candidate_rows(page)
            page_text = normalize_whitespace(page.extract_text() or "")

            if not page_text and not page_rows:
                continue

            prompt = f"""You extract curriculum subject rows from engineering regulation tables.

Return ONLY a JSON array. Each item must have exactly these fields:
- code
- name
- semester
- lecture_hrs
- tutorial_hrs
- practical_hrs
- credits
- category
- type

Rules:
- Extract only actual subjects, not totals, not marks rules, not notes, not exam criteria.
- Keep the exact subject code and title from the regulation.
- Semester is the semester number for the row.
- If the table shows a row like serial number + subject code + title + hours + credits + category, use it.
- If there is no explicit category, use UNKNOWN.
- type should be Theory or Practical when obvious, otherwise infer from the row.
- Do not invent rows.

Page number: {page_index}

Page text:
{compact_prompt_text(page_text, 5000)}

Candidate rows:
{chr(10).join(page_rows[:GROQ_MAX_PAGE_ROWS])}
"""

            raw = call_groq(prompt, f"extract regulation subjects page {page_index}", max_tokens=2200)
            if not raw:
                raw = call_openrouter(prompt, f"extract regulation subjects page {page_index}", max_tokens=2200)
            if not raw:
                continue

            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue

            if not isinstance(parsed, list):
                continue

            for item in parsed:
                record = normalize_subject_record(item)
                if record is None:
                    continue
                key = (record["code"], record["semester"])
                if key in seen:
                    continue
                seen.add(key)
                subjects.append(record)

    return subjects


def collect_page_candidate_rows(page):
    candidates = []

    page_text = page.extract_text() or ""
    candidates.extend(line for line in (normalize_whitespace(raw_line) for raw_line in page_text.splitlines()) if line)

    try:
        tables = page.extract_tables() or []
    except Exception:
        tables = []

    for table in tables:
        for row in table or []:
            row_text = normalize_whitespace(" ".join(cell for cell in row if cell))
            if row_text:
                candidates.append(row_text)

    try:
        words = page.extract_words(use_text_flow=True, keep_blank_chars=False, extra_attrs=["fontname", "size"])
    except Exception:
        words = []

    if words:
        line_map = defaultdict(list)
        for word in words:
            top = round(float(word.get("top", 0.0)) / 3.0) * 3.0
            line_map[top].append(word)

        for top in sorted(line_map):
            words_on_line = sorted(line_map[top], key=lambda item: float(item.get("x0", 0.0)))
            row_text = normalize_whitespace(" ".join(word.get("text", "") for word in words_on_line))
            if row_text:
                candidates.append(row_text)

    deduped = []
    seen = set()
    for row in candidates:
        key = row.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    return deduped


def parse_subject_row(line, semester):
    """
    Parse a subject row from any Indian university regulation format.

    Handles:
    - 4-column numeric:  L  T  P  C  (most common)
    - 3-column numeric:  L  P  C     (no tutorial column)
    - 3-column numeric:  L  T  C     (no practical column, treated as T=val, P=0)
    - Optional trailing category token (2-5 uppercase letters)
    - Any subject code format matched by SUBJECT_CODE_RE
    """
    cleaned = normalize_whitespace(line)
    if not cleaned or semester is None:
        return None

    tokens = cleaned.split()
    if len(tokens) < 4:
        return None

    # Find subject code position
    code_index = None
    for index, token in enumerate(tokens):
        if SUBJECT_CODE_RE.match(token):
            code_index = index
            break

    if code_index is None:
        return None

    # Optional trailing category — accept any 2-5 uppercase alpha token
    # Don't reject rows that lack it; just mark as UNKNOWN
    last_token = tokens[-1].upper()
    if re.fullmatch(r"[A-Z]{2,5}", last_token):
        category = last_token
        search_end = len(tokens) - 1   # numeric window must end before category
    else:
        category = "UNKNOWN"
        search_end = len(tokens)

    # Find numeric window: try 4-col then 3-col
    numeric_start = None
    window_size = None
    for ws in (4, 3):
        for idx in range(code_index + 1, search_end - ws + 1):
            window = tokens[idx: idx + ws]
            if all(re.fullmatch(r"\d+(?:\.\d+)?", t) for t in window):
                numeric_start = idx
                window_size = ws
                break
        if numeric_start is not None:
            break

    if numeric_start is None:
        return None

    num = tokens[numeric_start: numeric_start + window_size]
    if window_size == 4:
        lecture_hrs   = int(float(num[0]))
        tutorial_hrs  = int(float(num[1]))
        practical_hrs = int(float(num[2]))
        credits       = int(float(num[3]))
    else:
        # 3-column: assume L  P  C  (tutorial = 0)
        lecture_hrs   = int(float(num[0]))
        tutorial_hrs  = 0
        practical_hrs = int(float(num[1]))
        credits       = int(float(num[2]))

    name_tokens = tokens[code_index + 1: numeric_start]
    name = normalize_whitespace(" ".join(name_tokens)).strip(" -–—")
    if not name:
        return None

    subject_type = "Practical" if practical_hrs > 0 else "Theory"

    return {
        "code":          tokens[code_index],
        "name":          name,
        "semester":      semester,
        "lecture_hrs":   lecture_hrs,
        "tutorial_hrs":  tutorial_hrs,
        "practical_hrs": practical_hrs,
        "credits":       credits,
        "category":      category,
        "type":          subject_type,
    }


def extract_subjects_from_regulation(pdf_path):
    groq_subjects = extract_subjects_with_groq(pdf_path)
    if groq_subjects:
        print(f"[Groq] Extracted {len(groq_subjects)} subjects from regulation PDF")
        return groq_subjects

    subjects = []
    current_semester = None
    pending_row = None
    seen = set()

    try:
        import pdfplumber
    except ImportError as error:
        raise RuntimeError("Install pdfplumber in this venv before loading syllabus topics.") from error

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_rows = collect_page_candidate_rows(page)

            for line in page_rows:
                if line.startswith("====="):
                    continue

                detected_semester = detect_semester_from_line(line)
                if detected_semester is not None:
                    current_semester = detected_semester
                    pending_row = None
                    continue

                if pending_row is not None:
                    if SUBJECT_CODE_RE.search(line):
                        parsed_pending = parse_subject_row(pending_row, current_semester)
                        if parsed_pending is not None:
                            key = (parsed_pending["code"], parsed_pending["semester"])
                            if key not in seen:
                                seen.add(key)
                                subjects.append(parsed_pending)
                        pending_row = None
                    else:
                        combined_row = normalize_whitespace(f"{pending_row} {line}")
                        parsed = parse_subject_row(combined_row, current_semester)
                        if parsed is not None:
                            key = (parsed["code"], parsed["semester"])
                            if key not in seen:
                                seen.add(key)
                                subjects.append(parsed)
                            pending_row = None
                            continue
                        pending_row = combined_row
                        continue

                parsed = parse_subject_row(line, current_semester)
                if parsed is None:
                    if SUBJECT_CODE_RE.search(line):
                        pending_row = line
                    continue

                key = (parsed["code"], parsed["semester"])
                if key in seen:
                    continue
                seen.add(key)
                subjects.append(parsed)

    return subjects


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
    """
    Extract unit blocks from a subject's syllabus lines.

    Detects headings in these formats:
      UNIT I / UNIT - 1 / UNIT–II        (most Anna University, VTU)
      MODULE 1 / MODULE - II              (Amrita, some VTU)
      UNIT I: Title text                  (combined heading + title)
      Title Text:                         (colon-terminated heading, old format)
    """
    UNIT_HEADING_RE = re.compile(
        r"^(?:"
        r"(?:UNIT|MODULE)\s*[-–—]?\s*([IVXivx]+|\d+)"  # UNIT I, MODULE 2
        r")"
        r"(?:\s*[-–:]\s*(.+))?$",                        # optional ": Title" suffix
        re.I,
    )
    COLON_HEADING_RE = re.compile(r"^(.{3,140}?):\s*(.*)$")

    content_lines = list(subject_lines)
    # Drop the subject header line and optional credit-hour summary line
    if len(content_lines) > 1 and re.fullmatch(r"[0-9 ]+", content_lines[1]):
        content_lines = content_lines[2:]
    else:
        content_lines = content_lines[1:]

    stop_markers = ("TEXT BOOK", "TEXTBOOK", "REFERENCES", "TOTAL", "REFERENCE BOOK", "SUGGESTED READING")
    filtered_lines = []
    for line in content_lines:
        if any(line.upper().startswith(marker) for marker in stop_markers):
            break
        filtered_lines.append(line)

    units = []
    current = None

    for line in filtered_lines:
        unit_match = UNIT_HEADING_RE.match(line.strip())
        if unit_match:
            if current is not None:
                units.append(current)
            # Use the suffix after the unit number as the title, or blank if absent
            title_suffix = normalize_whitespace(unit_match.group(2) or "")
            unit_label = normalize_whitespace(unit_match.group(0).split(":")[0])
            title = title_suffix if title_suffix else unit_label
            current = {"title": title, "body_lines": []}
            continue

        colon_match = COLON_HEADING_RE.match(line)
        if colon_match and not re.match(r"^\d+[\.)]\s*", line):
            if current is not None:
                units.append(current)
            body_start = normalize_whitespace(colon_match.group(2))
            current = {
                "title": normalize_whitespace(colon_match.group(1)),
                "body_lines": [body_start] if body_start else [],
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

    if len(text) > 600:
        summarized_items = summarize_unit_text(text, max_sentences=4)
        if summarized_items:
            return summarized_items

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


def normalize_topic_graph_item(item):
    if not isinstance(item, dict):
        return None

    try:
        number = int(item.get("number"))
    except (TypeError, ValueError):
        return None

    title = normalize_whitespace(str(item.get("title", "")))
    if not title:
        return None

    topics_raw = item.get("topics", [])
    if not isinstance(topics_raw, list):
        topics_raw = []

    topics = []
    seen = set()
    for topic in topics_raw:
        cleaned = normalize_whitespace(str(topic))
        if not cleaned:
            continue
        key = cleaned.lower()
        if key in seen:
            continue
        seen.add(key)
        topics.append(cleaned)

    return {"number": number, "title": title, "topics": topics}


def extract_topic_graph_with_groq(subjects, pdf_path):
    api_key = get_groq_api_key() or get_openrouter_api_key()
    if not api_key or _GROQ_UNAVAILABLE:
        return []

    try:
        syllabus_text = load_syllabus_text(pdf_path)
    except Exception:
        return []

    subject_codes = [subject["code"] for subject in subjects]
    subject_blocks = collect_subject_blocks(syllabus_text, subject_codes)
    topic_graph = []

    for block in subject_blocks:
        block_text = compact_prompt_text("\n".join(block["lines"]), GROQ_MAX_BLOCK_CHARS)
        if not block_text:
            continue

        prompt = f"""You extract unit and topic nodes from a syllabus section.

Return ONLY a JSON array. Each item must have exactly:
- number
- title
- topics

Rules:
- Extract only true units/modules from the syllabus.
- Each topic must be a short topical node, not a sentence paragraph.
- Ignore references, textbooks, assessment rules, mark schemes, and notes.
- If a unit has no clear title, use "Overview".
- Keep topics deduplicated and concise.

Subject code: {block['code']}

Syllabus text:
{block_text}
"""

        raw = call_groq(prompt, f"extract topics for {block['code']}", max_tokens=2500)
        if not raw:
            raw = call_openrouter(prompt, f"extract topics for {block['code']}", max_tokens=2500)
        if not raw:
            continue

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue

        if not isinstance(parsed, list):
            continue

        units = []
        seen_numbers = set()
        for item in parsed:
            unit = normalize_topic_graph_item(item)
            if unit is None or unit["number"] in seen_numbers:
                continue
            seen_numbers.add(unit["number"])
            units.append(unit)

        if units:
            topic_graph.append({"code": block["code"], "units": sorted(units, key=lambda entry: entry["number"])})

    return topic_graph


def summarize_unit_text(text, max_sentences=4):
    try:
        from nltk.corpus import stopwords
        from nltk.tokenize import sent_tokenize
    except ImportError:
        return []

    try:
        sentences = [normalize_whitespace(sentence) for sentence in sent_tokenize(text)]
    except LookupError:
        sentences = [normalize_whitespace(sentence) for sentence in re.split(r"(?<=[.!?])\s+", text)]

    sentences = [sentence for sentence in sentences if sentence]
    if not sentences:
        return []
    if len(sentences) <= max_sentences:
        return sentences

    try:
        stop_words = set(stopwords.words("english"))
    except LookupError:
        stop_words = set()

    frequency = defaultdict(int)
    for sentence in sentences:
        for word in re.findall(r"[A-Za-z][A-Za-z'-]+", sentence.lower()):
            if word not in stop_words:
                frequency[word] += 1

    if not frequency:
        return sentences[:max_sentences]

    ranked = []
    for index, sentence in enumerate(sentences):
        words = re.findall(r"[A-Za-z][A-Za-z'-]+", sentence.lower())
        score = sum(frequency.get(word, 0) for word in words)
        ranked.append((score, index, sentence))

    selected = sorted(sorted(ranked, key=lambda item: (-item[0], item[1]))[:max_sentences], key=lambda item: item[1])
    return [sentence for _, _, sentence in selected]


def extract_topic_graph(subjects, pdf_path):
    groq_topic_graph = extract_topic_graph_with_groq(subjects, pdf_path)
    if groq_topic_graph:
        print(f"[Groq] Parsed topic structure for {len(groq_topic_graph)} subjects")
        return groq_topic_graph

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
    """
    Infer RELATED_TO edges between topics in different subjects.
    Priority: Groq → OpenRouter → Gemini → local heuristic fallback.
    """
    use_groq = bool(get_groq_api_key()) and not _GROQ_UNAVAILABLE
    use_openrouter = bool(get_openrouter_api_key())

    if GEMINI_QUOTA_EXHAUSTED and not use_groq and not use_openrouter:
        print("[API] All LLM quotas exhausted; using local heuristic topic matching.")
        return infer_related_topics_fallback(subjects, prerequisites, topic_graph)

    subject_lookup = {subject["code"]: subject for subject in subjects}
    topic_index = build_topic_index(topic_graph)
    target_map = build_related_subject_targets(prerequisites, topic_index.keys(), max_depth=2)

    related_pairs = []
    seen_pairs = set()
    provider_counts = defaultdict(int)

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

        raw = None
        provider_used = None
        if use_groq:
            raw = call_groq(prompt, f"infer topic links for {source_code}", max_tokens=1400)
            if raw is not None:
                provider_used = "Groq"

        if raw is None and use_openrouter:
            raw = call_openrouter(prompt, f"infer topic links for {source_code}", max_tokens=1400)
            if raw is not None:
                provider_used = "OpenRouter"

        if raw is None and not GEMINI_QUOTA_EXHAUSTED:
            raw = call_gemini(prompt, f"infer topic links for {source_code}", max_output_tokens=1400)
            if raw is not None:
                provider_used = "Gemini"

        if raw is None:
            if GEMINI_QUOTA_EXHAUSTED and not use_groq and not use_openrouter:
                return infer_related_topics_fallback(subjects, prerequisites, topic_graph)
            continue

        if provider_used:
            provider_counts[provider_used] += 1

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as error:
            print(f"[API] JSON parse failed for topic links from {source_code}: {error}")
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

    if provider_counts:
        breakdown = ", ".join(f"{provider}: {count}" for provider, count in sorted(provider_counts.items()))
        print(f"[LLM] Inferred {len(related_pairs)} cross-subject topic relationships ({breakdown})")
    else:
        print(f"[LLM] Inferred {len(related_pairs)} cross-subject topic relationships (no LLM provider succeeded; fallback may have been used)")
    return related_pairs


# ─────────────────────────────────────────────
# GROQ — free LLM for prerequisite inference
# ─────────────────────────────────────────────
def call_groq(prompt, label, max_tokens=2000):
    """
    Call Groq free-tier API (llama-3.3-70b-versatile).
    Returns raw text or None on failure.
    Set GROQ_API_KEY in .env — free at console.groq.com.
    """
    global _GROQ_UNAVAILABLE, _GROQ_UNAVAILABLE_REASON

    if _GROQ_UNAVAILABLE:
        return None

    api_key = get_groq_api_key()
    if not api_key:
        return None

    global _LAST_GROQ_REQUEST_AT
    if GROQ_REQUEST_DELAY_SECONDS > 0:
        elapsed = time.monotonic() - _LAST_GROQ_REQUEST_AT
        if elapsed < GROQ_REQUEST_DELAY_SECONDS:
            time.sleep(GROQ_REQUEST_DELAY_SECONDS - elapsed)

    payload = {
        "model": os.getenv("GROQ_MODEL", "llama3-8b-8192"),
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }

    request = Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    _LAST_GROQ_REQUEST_AT = time.monotonic()

    try:
        with urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
            raw = data["choices"][0]["message"]["content"].strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return raw.strip()
    except HTTPError as error:
        error_body = error.read().decode('utf-8', errors='replace')
        print(f"[Groq] HTTP {error.code} for {label}: {error_body}")
        if error.code == 403 and "1010" in error_body:
            _GROQ_UNAVAILABLE = True
            _GROQ_UNAVAILABLE_REASON = (
                "Groq is rejecting this request at the edge (Cloudflare 1010 / 403), "
                "so the API call is not reaching Groq's backend. This usually points to "
                "a blocked IP/network, a restricted key, or a Groq-side access rule."
            )
            print(f"[Groq] {_GROQ_UNAVAILABLE_REASON} Disabling Groq for the rest of this run.")
    except (TimeoutError, URLError, KeyError, IndexError, json.JSONDecodeError) as error:
        print(f"[Groq] Error for {label}: {error}")

    return None


def call_openrouter(prompt, label, max_tokens=2000):
    """
    Call OpenRouter API (supports Claude, Llama, GPT-4, etc.).
    Returns raw text or None on failure.
    Set OPENROUTER_API_KEY in .env — signup at openrouter.ai.
    """
    api_key = get_openrouter_api_key()
    if not api_key:
        return None

    model = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
    }

    request = Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "HTTP-Referer": "https://yggdrasil.local",
            "X-Title": "Yggdrasil",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
            raw = data["choices"][0]["message"]["content"].strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
            return raw.strip()
    except HTTPError as error:
        error_body = error.read().decode('utf-8', errors='replace')
        print(f"[OpenRouter] HTTP {error.code} for {label}: {error_body}")
    except (TimeoutError, URLError, KeyError, IndexError, json.JSONDecodeError) as error:
        print(f"[OpenRouter] Error for {label}: {error}")

    return None


def infer_prerequisites_heuristic(subjects):
    """
    Local fallback: infer prerequisites purely from subject names and semester ordering.
    Looks for explicit name-based signals like "Advanced X" following "X", lab versions
    of theory subjects, and common CS/ML curriculum patterns.
    """
    NAME_SIGNALS = [
        (re.compile(r"\badvanced\b", re.I), re.compile(r"(?!advanced)\b", re.I)),
        (re.compile(r"\bii\b", re.I), None),
        (re.compile(r"\b2\b"), None),
        (re.compile(r"\blab\b|\blaboratory\b|\bpractical\b", re.I), None),
    ]

    by_sem = defaultdict(list)
    for s in subjects:
        by_sem[s["semester"]].append(s)

    name_to_subjects = defaultdict(list)
    for s in subjects:
        key = re.sub(r"\b(advanced|ii|2|lab|laboratory|practical|and|the|of|in|to)\b", "", s["name"], flags=re.I)
        key = re.sub(r"\s+", " ", key).strip().lower()
        name_to_subjects[key].append(s)

    prereqs = []
    seen = set()

    for s in subjects:
        # "Advanced X" → find plain "X" in earlier semester
        if re.search(r"\badvanced\b", s["name"], re.I):
            base_name = re.sub(r"\badvanced\b\s*", "", s["name"], flags=re.I).strip().lower()
            for candidate in subjects:
                if candidate["semester"] < s["semester"] and base_name in candidate["name"].lower():
                    pair = (candidate["code"], s["code"])
                    if pair not in seen:
                        seen.add(pair)
                        prereqs.append({"prerequisite": candidate["code"], "dependent": s["code"]})

        # "X Lab" / "X Laboratory" → find "X" theory in same or earlier semester
        if re.search(r"\blab\b|\blaboratory\b", s["name"], re.I):
            base_name = re.sub(r"\s*(lab|laboratory|practical)\s*", " ", s["name"], flags=re.I).strip().lower()
            for candidate in subjects:
                if candidate["semester"] <= s["semester"] and candidate["code"] != s["code"]:
                    if base_name in candidate["name"].lower() or candidate["name"].lower() in base_name:
                        pair = (candidate["code"], s["code"])
                        if pair not in seen:
                            seen.add(pair)
                            prereqs.append({"prerequisite": candidate["code"], "dependent": s["code"]})

    print(f"[Heuristic] Inferred {len(prereqs)} prerequisite relationships")
    return prereqs


# ─────────────────────────────────────────────
# STEP 2: Infer prerequisites
# ─────────────────────────────────────────────
def infer_prerequisites(subjects):
    """
    Infer prerequisite relationships between subjects.
    Priority: Groq (free LLM) → OpenRouter (paid, more reliable) → local heuristic fallback.
    """
    if not subjects:
        return []

    manual_prerequisites = collect_manual_prerequisites(subjects)
    predefined_prerequisites = [
        {"prerequisite": prerequisite, "dependent": dependent}
        for prerequisite, dependent in PREDEFINED_PREREQUISITES
    ]

    if not get_groq_api_key() and not get_openrouter_api_key():
        print("[Prerequisites] No Groq or OpenRouter key set; using local heuristic inference.")
        inferred = infer_prerequisites_heuristic(subjects)
        return merge_valid_prerequisites(subjects, predefined_prerequisites, manual_prerequisites, inferred)

    # Format subjects as a compact list for the prompt
    lines = []
    for s in subjects:
        lines.append(f"  {s['code']} | Sem {s['semester']} | {s['name']} | {s['category']} | {s['type']}")
    subject_list = "\n".join(lines)

    prompt = f"""You are building a curriculum knowledge graph for an engineering college.
Given this list of subjects, infer PREREQUISITE_OF relationships: which subject must be completed before another.

Rules:
- Only create prerequisites where there is a clear conceptual dependency.
- A subject can only prerequisite subjects in LATER semesters.
- "Advanced X" always requires plain "X".
- Labs require their corresponding theory subject.
- Do not invent relationships; if unclear, skip.
- Return ONLY a JSON array. Each object: {{"prerequisite": "CODE", "dependent": "CODE"}}
- No explanation, no markdown.

Subjects:
{subject_list}
"""

    raw = None
    if get_groq_api_key():
        print(f"[Groq] Inferring prerequisites for {len(subjects)} subjects...")
        raw = call_groq(prompt, "infer prerequisites", max_tokens=2000)

    if raw is None and get_openrouter_api_key():
        print(f"[OpenRouter] Inferring prerequisites for {len(subjects)} subjects...")
        raw = call_openrouter(prompt, "infer prerequisites", max_tokens=2000)

    if raw is None:
        print("[Prerequisites] All LLM providers unavailable; using local heuristic inference.")
        inferred = infer_prerequisites_heuristic(subjects)
        return merge_valid_prerequisites(subjects, predefined_prerequisites, manual_prerequisites, inferred)

    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            raise ValueError("Expected a JSON array")
    except (json.JSONDecodeError, ValueError) as error:
        print(f"[Prerequisites] JSON parse failed: {error}; using local heuristic.")
        inferred = infer_prerequisites_heuristic(subjects)
        return merge_valid_prerequisites(subjects, predefined_prerequisites, manual_prerequisites, inferred)

    inferred = []
    sem_map = {s["code"]: s["semester"] for s in subjects}
    code_set = set(sem_map.keys())
    for item in parsed:
        pre = str(item.get("prerequisite", "")).strip().upper()
        dep = str(item.get("dependent", "")).strip().upper()
        if pre in code_set and dep in code_set and sem_map[pre] < sem_map[dep]:
            inferred.append({"prerequisite": pre, "dependent": dep})

    merged = merge_valid_prerequisites(subjects, predefined_prerequisites, manual_prerequisites, inferred)
    print(f"[Groq] Inferred {len(merged)} valid prerequisite relationships")
    return merged


# ─────────────────────────────────────────────
# STEP 3: Load into Neo4j
# ─────────────────────────────────────────────
def load_into_neo4j(subjects, prerequisites, topic_graph):
    if GraphDatabase is None:
        raise RuntimeError("Install the neo4j driver in this venv before loading data: c:/vscode/Yggdrasil/.venv/Scripts/python.exe -m pip install neo4j")

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    try:
        related_topics = infer_related_topics(subjects, prerequisites, topic_graph)
        summary = {
            "subjects_loaded": len(subjects),
            "prerequisites_loaded": 0,
            "prerequisites_skipped": 0,
            "units_loaded": 0,
            "topics_loaded": 0,
            "related_loaded": 0,
            "related_skipped": 0,
        }

        with driver.session() as session:

            # Constraints
            print("[Neo4j] Creating constraints...")
            session.run("CREATE CONSTRAINT subject_code IF NOT EXISTS FOR (s:Subject) REQUIRE s.code IS UNIQUE")
            session.run("CREATE CONSTRAINT semester_num IF NOT EXISTS FOR (sem:Semester) REQUIRE sem.number IS UNIQUE")
            session.run("CREATE CONSTRAINT unit_key IF NOT EXISTS FOR (u:Unit) REQUIRE u.key IS UNIQUE")
            session.run("CREATE CONSTRAINT topic_key IF NOT EXISTS FOR (t:Topic) REQUIRE t.key IS UNIQUE")
            session.run("CREATE INDEX topic_lookup IF NOT EXISTS FOR (t:Topic) ON (t.subject_code, t.name_normalized)")

            # Semesters — infer range from actual subjects (supports 4-yr, 5-yr programs)
            print("[Neo4j] Creating Semester nodes...")
            max_sem = max((s["semester"] for s in subjects), default=8)
            for n in range(1, max_sem + 1):
                session.run("""
                    MERGE (sem:Semester {number: $n})
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
            summary["prerequisites_loaded"] = loaded
            summary["prerequisites_skipped"] = skipped

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
            summary["units_loaded"] = unit_count
            summary["topics_loaded"] = topic_count

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
            summary["related_loaded"] = related_loaded
            summary["related_skipped"] = related_skipped

    finally:
        try:
            driver.close()
        except Exception:
            pass
    print("[Neo4j] Done.")
    return summary


def reset_curriculum_graph(driver):
    with driver.session() as session:
        session.run("MATCH (n:Topic) DETACH DELETE n")
        session.run("MATCH (n:Unit) DETACH DELETE n")
        session.run("MATCH (n:Subject) DETACH DELETE n")
        session.run("MATCH (n:Semester) DETACH DELETE n")
        session.run("MATCH (n:Program) DETACH DELETE n")


def load_regulation_graph(regulation_pdf_path, csv_path=CSV_PATH, verify_graph=False):
    if GraphDatabase is None:
        raise RuntimeError("Install the neo4j driver in this venv before loading data: c:/vscode/Yggdrasil/.venv/Scripts/python.exe -m pip install neo4j")

    subjects = extract_subjects_from_regulation(regulation_pdf_path)
    if not subjects:
        raise RuntimeError(
            "No subjects were extracted from the uploaded regulation PDF. Check that the file has semester headings and subject rows in a readable table format."
        )

    prerequisites = infer_prerequisites(subjects)
    topic_graph = extract_topic_graph(subjects, regulation_pdf_path)

    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    reset_curriculum_graph(driver)
    driver.close()

    summary = load_into_neo4j(subjects, prerequisites, topic_graph)

    result = {
        "regulation_pdf": regulation_pdf_path,
        "csv_path": csv_path,
        "subjects": len(subjects),
        "prerequisites": len(prerequisites),
        "topic_subjects": len(topic_graph),
    }
    result.update(summary)

    if verify_graph:
        verify(subjects, prerequisites, topic_graph)

    return result


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