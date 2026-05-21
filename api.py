"""FastAPI query layer for the curriculum knowledge graph."""

from collections import defaultdict, deque
from functools import lru_cache
import csv
import os
import re
import tempfile
from typing import Dict

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from config import CHUNK_OVERLAP_TOKENS, CHUNK_SIZE_TOKENS
from load_data import load_regulation_graph
from parser import parse_file
from vector_store import upsert_chunks
from rag_pipeline import ask as rag_ask

try:
	from neo4j import GraphDatabase
except ImportError:  # pragma: no cover - handled at runtime
	GraphDatabase = None


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

NEO4J_URI = os.getenv("NEO4J_URI")
NEO4J_USER = os.getenv("NEO4J_USERNAME")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD")
CSV_PATH = os.getenv("CSV_PATH", "subjects.csv")
SYLLABUS_PDF_PATH = os.getenv("SYLLABUS_PDF_PATH", "REGULATIONS.pdf")
ALLOWED_EXTENSIONS = {"pdf", "ppt", "pptx"}

app = FastAPI(title="Yggdrasil Curriculum API", version="1.0.0")
app.add_middleware(
	CORSMiddleware,
	allow_origins=["*"],
	allow_credentials=False,
	allow_methods=["*"],
	allow_headers=["*"],
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
NOTEBOOK_INDEX = os.path.join(BASE_DIR, "curriculum_notebook.html")
UPLOAD_INDEX = os.path.join(BASE_DIR, "upload_resources.html")
REGULATION_UPLOAD_INDEX = os.path.join(BASE_DIR, "regulation_upload.html")
_driver = None
_subject_resource_index = None


class ChatRequest(BaseModel):
	question: str
	subject_code: str


@app.get("/")
def home_page():
	if not graph_is_initialized() and os.path.exists(REGULATION_UPLOAD_INDEX):
		return FileResponse(REGULATION_UPLOAD_INDEX, media_type="text/html")
	if os.path.exists(NOTEBOOK_INDEX):
		return FileResponse(NOTEBOOK_INDEX, media_type="text/html")
	raise HTTPException(status_code=404, detail="Notebook frontend not found")


@app.get("/app")
@app.get("/app/semester/{semester}")
@app.get("/app/semester/{semester}/subject/{code}")
def notebook_page(semester: int | None = None, code: str | None = None):
	if not graph_is_initialized() and os.path.exists(REGULATION_UPLOAD_INDEX):
		return FileResponse(REGULATION_UPLOAD_INDEX, media_type="text/html")
	if os.path.exists(NOTEBOOK_INDEX):
		return FileResponse(NOTEBOOK_INDEX, media_type="text/html")
	raise HTTPException(status_code=404, detail="Notebook frontend not found")


@app.get("/app/semester/{semester}/subject/{code}/upload")
def upload_page(semester: int, code: str):
	if os.path.exists(UPLOAD_INDEX):
		return FileResponse(UPLOAD_INDEX, media_type="text/html")
	raise HTTPException(status_code=404, detail="Upload frontend not found")


@app.get("/app/regulation/upload")
def regulation_upload_page():
	if os.path.exists(REGULATION_UPLOAD_INDEX):
		return FileResponse(REGULATION_UPLOAD_INDEX, media_type="text/html")
	raise HTTPException(status_code=404, detail="Regulation upload frontend not found")


def normalize_whitespace(text):
	return re.sub(r"\s+", " ", text or "").strip()


def parse_semester_label(value):
	match = re.fullmatch(r"(?:sem)?\s*(\d{1,2})", value.strip().lower())
	if not match:
		raise HTTPException(status_code=400, detail=f"Invalid semester label: {value}")

	semester = int(match.group(1))
	if semester < 1 or semester > 8:
		raise HTTPException(status_code=400, detail="Semester must be between 1 and 8")
	return semester


def load_subject_codes(path):
	codes = []
	try:
		with open(path, newline="", encoding="utf-8") as file_handle:
			reader = csv.DictReader(file_handle)
			for line_number, row in enumerate(reader, start=2):
				if not row:
					continue
				try:
					code = (row.get("code") or "").strip()
					name = (row.get("name") or "").strip()
					semester_raw = (row.get("semester") or "").strip()
				except KeyError as error:
					raise ValueError(f"Missing required column in {path}: {error.args[0]}") from error

				if not code or not name or not semester_raw:
					raise ValueError(f"Missing required subject data in {path} at row {line_number}")

				try:
					semester = int(semester_raw)
				except ValueError as error:
					raise ValueError(f"Invalid semester value in {path} at row {line_number}: {semester_raw!r}") from error

				codes.append({"code": code, "name": name, "semester": semester})
	except (FileNotFoundError, PermissionError, csv.Error, KeyError, ValueError) as error:
		raise ValueError(f"Failed to load subject codes from {path}: {error}") from error

	return codes


def graph_is_initialized():
	try:
		driver = get_driver()
	except HTTPException:
		return False

	try:
		with driver.session() as session:
			row = session.run("MATCH (s:Subject) RETURN count(s) AS count").single()
			return bool(row and row["count"])
	except Exception:
		return False


def _ext(filename):
	return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _detect_source_type(filepath, ext):
	"""Classify uploads so Neo4j can distinguish syllabus-style PDFs from resource decks."""
	if ext in ("ppt", "pptx"):
		return "ppt"

	try:
		import pdfplumber
	except ImportError as error:
		raise HTTPException(status_code=500, detail="pdfplumber is required to inspect uploaded PDFs") from error

	import re as _re
	unit_re = _re.compile(r"^\s*(unit\s*[-–]?\s*(i{1,3}|iv|v?i{0,3}|[1-5]))\b", _re.I | _re.M)
	with pdfplumber.open(filepath) as pdf:
		sample = "\n".join((pdf.pages[i].extract_text() or "") for i in range(min(5, len(pdf.pages))))
	return "syllabus" if len(unit_re.findall(sample)) >= 2 else "textbook"


def get_driver():
	global _driver
	if GraphDatabase is None:
		raise HTTPException(status_code=500, detail="neo4j driver is not installed")
	if not NEO4J_URI or not NEO4J_USER or not NEO4J_PASSWORD:
		raise HTTPException(status_code=500, detail="Neo4j connection settings are missing")
	if _driver is None:
		_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
	return _driver


def get_subject(code):
	driver = get_driver()
	with driver.session() as session:
		row = session.run(
			"""
			MATCH (s:Subject {code: $code})
			OPTIONAL MATCH (s)-[:BELONGS_TO]->(sem:Semester)
			OPTIONAL MATCH (s)-[:HAS_RESOURCE]->(r:Resource)
			WITH s, sem, count(DISTINCT r) AS resource_count
			RETURN s.code AS code, s.name AS name, s.category AS category, sem.number AS semester, s.credits AS credits,
			       resource_count
			""",
			code=code,
		).single()

	if not row:
		return None

	return {
		"code": row["code"],
		"name": row["name"],
		"category": row["category"],
		"semester": row["semester"],
		"credits": row["credits"],
		"resource_count": row["resource_count"],
	}


def get_all_subjects():
	driver = get_driver()
	with driver.session() as session:
		rows = session.run(
			"""
			MATCH (s:Subject)
			OPTIONAL MATCH (s)-[:BELONGS_TO]->(sem:Semester)
			OPTIONAL MATCH (s)-[:HAS_RESOURCE]->(r:Resource)
			WITH s, sem, count(DISTINCT r) AS resource_count
			RETURN s.code AS code, s.name AS name, s.category AS category, sem.number AS semester, s.credits AS credits,
			       resource_count
			ORDER BY semester, code
			"""
		).data()

	return rows


def attach_resource_node(subject_code, source_file, source_type, chunk_count):
	driver = get_driver()
	resource_id = f"{subject_code}:{source_file}"
	with driver.session() as session:
		session.run(
			"""
			MATCH (s:Subject {code: $subject_code})
			MERGE (r:Resource {resource_id: $resource_id})
			SET r.subject_code = $subject_code,
				r.source_file = $source_file,
				r.source_type = $source_type,
				r.chunk_count = $chunk_count,
				r.updated_at = datetime()
			MERGE (s)-[:HAS_RESOURCE]->(r)
			""",
			subject_code=subject_code,
			resource_id=resource_id,
			source_file=source_file,
			source_type=source_type,
			chunk_count=chunk_count,
		)


@app.get("/subjects")
def list_subjects():
	"""Return all subjects grouped by semester for upload and query UIs."""
	subjects = get_all_subjects()
	grouped: Dict[int, list] = {}
	for subject in subjects:
		semester = subject.get("semester")
		if semester is None:
			continue
		grouped.setdefault(semester, []).append(subject)
	return {"semesters": grouped}


@app.post("/ingest")
async def ingest(
	file: UploadFile = File(...),
	subject_code: str = Form(...),
):
	"""Chunk an uploaded resource, store vectors in Chroma, and link metadata in Neo4j."""
	ext = _ext(file.filename or "")
	if ext not in ALLOWED_EXTENSIONS:
		raise HTTPException(
			status_code=400,
			detail=f"Unsupported file type '.{ext}'. Allowed: {ALLOWED_EXTENSIONS}",
		)

	subject = get_subject(subject_code.strip().upper())
	if subject is None:
		raise HTTPException(
			status_code=404,
			detail=f"Subject code '{subject_code}' not found in the knowledge graph.",
		)

	suffix = f".{ext}"
	with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
		content = await file.read()
		tmp.write(content)
		tmp_path = tmp.name

	try:
		chunks = parse_file(tmp_path, chunk_size=CHUNK_SIZE_TOKENS, overlap=CHUNK_OVERLAP_TOKENS)
		if not chunks:
			raise HTTPException(status_code=422, detail="No extractable content found in the uploaded file.")

		source_type = _detect_source_type(tmp_path, ext)
		original_filename = file.filename or f"upload.{ext}"
		for chunk in chunks:
			chunk.source_file = original_filename

		summary = upsert_chunks(
			chunks=chunks,
			subject_code=subject["code"],
			subject_name=subject["name"],
			semester=subject["semester"],
			source_type=source_type,
		)

		attach_resource_node(
			subject_code=subject["code"],
			source_file=original_filename,
			source_type=source_type,
			chunk_count=summary["upserted"],
		)
	finally:
		os.unlink(tmp_path)

	return {
		"status": "success",
		"subject": subject,
		"source_type": source_type,
		"file": original_filename,
		"chunks": summary,
	}


@app.post("/ingest/regulation")
async def ingest_regulation(file: UploadFile = File(...)):
	ext = _ext(file.filename or "")
	if ext != "pdf":
		raise HTTPException(
			status_code=400,
			detail="Only PDF regulation uploads are supported.",
		)

	suffix = ".pdf"
	with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
		content = await file.read()
		tmp.write(content)
		tmp_path = tmp.name

	try:
		summary = load_regulation_graph(tmp_path, csv_path=CSV_PATH, verify_graph=False)
	finally:
		os.unlink(tmp_path)

	return {
		"status": "success",
		"file": file.filename or "REGULATIONS.pdf",
		"summary": summary,
		"next": "/app",
	}


@lru_cache(maxsize=1)
def load_syllabus_text(pdf_path):
	try:
		import pdfplumber
	except ImportError as error:  # pragma: no cover - runtime dependency issue
		raise HTTPException(status_code=500, detail="pdfplumber is required to read syllabus resources") from error

	if not os.path.exists(pdf_path):
		raise HTTPException(status_code=500, detail=f"Syllabus PDF not found: {pdf_path}")

	pages = []
	with pdfplumber.open(pdf_path) as pdf:
		for page in pdf.pages[6:]:
			pages.append(page.extract_text() or "")
	return "\n".join(pages)


def collect_subject_blocks(syllabus_text, subject_codes):
	ordered_codes = sorted({code for code in subject_codes if code}, key=len, reverse=True)
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


def extract_resource_sections(subject_lines):
	content_lines = list(subject_lines)
	if len(content_lines) > 1 and re.fullmatch(r"[0-9 ]+", content_lines[1]):
		content_lines = content_lines[2:]
	else:
		content_lines = content_lines[1:]

	sections = {"textbooks": [], "references": []}
	current = None

	for line in content_lines:
		upper_line = line.upper()
		if upper_line.startswith(("TEXT BOOK", "TEXTBOOK")):
			current = "textbooks"
			continue
		if upper_line.startswith("REFERENCES") or upper_line.startswith("REFERENCE"):
			current = "references"
			continue
		if upper_line.startswith(("TOTAL", "SEMESTER", "1.", "2.", "3.")) and current is not None:
			# Keep section parsing tolerant but do not overrun into the next subject block.
			pass

		if current is not None:
			cleaned = normalize_whitespace(line)
			if cleaned:
				sections[current].append(cleaned)

	return sections


def get_subject_resource_index():
	global _subject_resource_index
	if _subject_resource_index is not None:
		return _subject_resource_index

	subject_codes = [entry["code"] for entry in load_subject_codes(CSV_PATH)]
	syllabus_text = load_syllabus_text(SYLLABUS_PDF_PATH)
	subject_blocks = collect_subject_blocks(syllabus_text, subject_codes)

	resource_index = {}
	for block in subject_blocks:
		resource_index[block["code"]] = extract_resource_sections(block["lines"])

	_subject_resource_index = resource_index
	return _subject_resource_index


def fetch_subject_topics(driver, code):
	with driver.session() as session:
		subject_row = session.run(
			"""
			MATCH (s:Subject {code: $code})
			OPTIONAL MATCH (s)-[:BELONGS_TO]->(sem:Semester)
			RETURN s.code AS code, s.name AS name, sem.number AS semester, s.credits AS credits
			""",
			code=code,
		).single()

		if not subject_row:
			raise HTTPException(status_code=404, detail=f"Subject not found: {code}")

		result = session.run(
			"""
			MATCH (s:Subject {code: $code})-[:HAS_UNIT]->(u:Unit)
			OPTIONAL MATCH (u)<-[:PART_OF_UNIT]-(t:Topic)
			RETURN u.key AS unit_key, u.number AS unit_number, u.title AS unit_title,
				   collect(DISTINCT t.name) AS topics
			ORDER BY unit_number
			""",
			code=code,
		)

		units = []
		for row in result:
			units.append(
				{
					"number": row["unit_number"],
					"title": row["unit_title"],
					"topics": sorted(topic for topic in row["topics"] if topic),
				}
			)

	return {
		"subject": {
			"code": subject_row["code"],
			"name": subject_row["name"],
			"semester": subject_row["semester"],
			"credits": subject_row["credits"],
		},
		"units": units,
	}


def fetch_related_topics(driver, name):
	with driver.session() as session:
		rows = session.run(
			"""
			MATCH (t:Topic)
			WHERE toLower(t.name) = toLower($name) OR toLower(t.name) CONTAINS toLower($name)
			RETURN t.key AS key, t.name AS name, t.subject_code AS subject_code, t.unit_key AS unit_key
			ORDER BY t.subject_code, t.name
			LIMIT 25
			""",
			name=name,
		).data()

		if not rows:
			raise HTTPException(status_code=404, detail=f"Topic not found: {name}")

		payload = []
		for row in rows:
			related_rows = session.run(
				"""
				MATCH (source:Topic {key: $key})-[r:RELATED_TO]->(target:Topic)
				RETURN target.key AS key, target.name AS name, target.subject_code AS subject_code,
					   target.unit_key AS unit_key
				UNION
				MATCH (source:Topic)<-[r:RELATED_TO]-(target:Topic)
				WHERE source.key = $key
				RETURN target.key AS key, target.name AS name, target.subject_code AS subject_code,
					   target.unit_key AS unit_key
				ORDER BY subject_code, name
				""",
				key=row["key"],
			).data()

			payload.append(
				{
					"topic": row,
					"related": related_rows,
				}
			)

	return {"matches": payload}


def fetch_subject_resources(code):
	try:
		resource_index = get_subject_resource_index()
		resources = resource_index.get(code)
		if resources is None:
			resources = {"textbooks": [], "references": []}
	except HTTPException:
		resources = {"textbooks": [], "references": []}
	return {"code": code, "resources": resources}


def fetch_subject_prerequisites(driver, code):
	with driver.session() as session:
		subject_row = session.run(
			"""
			MATCH (s:Subject {code: $code})
			OPTIONAL MATCH (s)-[:BELONGS_TO]->(sem:Semester)
			RETURN s.code AS code, s.name AS name, s.category AS category, sem.number AS semester, s.credits AS credits
			""",
			code=code,
		).single()

		if not subject_row:
			raise HTTPException(status_code=404, detail=f"Subject not found: {code}")

		rows = session.run(
			"""
			MATCH (pre:Subject)-[:PREREQUISITE_OF]->(dep:Subject {code: $code})
			OPTIONAL MATCH (pre)-[:BELONGS_TO]->(sem:Semester)
			RETURN pre.code AS code, pre.name AS name, pre.category AS category, sem.number AS semester, pre.credits AS credits
			ORDER BY semester, code
			""",
			code=code,
		).data()

	return {
		"subject": {
			"code": subject_row["code"],
			"name": subject_row["name"],
			"category": subject_row["category"],
			"semester": subject_row["semester"],
			"credits": subject_row["credits"],
		},
		"prerequisites": rows,
	}


def topological_learning_path(driver, start_semester, end_semester):
	with driver.session() as session:
		subject_rows = session.run(
			"""
			MATCH (s:Subject)-[:BELONGS_TO]->(sem:Semester)
			WHERE sem.number >= $start_semester AND sem.number <= $end_semester
			RETURN s.code AS code, s.name AS name, sem.number AS semester
			ORDER BY semester, code
			""",
			start_semester=start_semester,
			end_semester=end_semester,
		).data()

		edge_rows = session.run(
			"""
			MATCH (pre:Subject)-[:PREREQUISITE_OF]->(dep:Subject)
			WHERE pre.semester >= $start_semester AND pre.semester <= $end_semester
			  AND dep.semester >= $start_semester AND dep.semester <= $end_semester
			RETURN pre.code AS from_code, dep.code AS to_code
			""",
			start_semester=start_semester,
			end_semester=end_semester,
		).data()

	subjects = {row["code"]: row for row in subject_rows}
	graph = defaultdict(set)
	indegree = {code: 0 for code in subjects}

	for edge in edge_rows:
		source = edge["from_code"]
		target = edge["to_code"]
		if source in subjects and target in subjects and target not in graph[source]:
			graph[source].add(target)
			indegree[target] += 1

	queue = deque(sorted([code for code, degree in indegree.items() if degree == 0], key=lambda code: (subjects[code]["semester"], code)))
	ordered = []

	while queue:
		code = queue.popleft()
		ordered.append(subjects[code])

		for target in sorted(graph[code], key=lambda item: (subjects[item]["semester"], item)):
			indegree[target] -= 1
			if indegree[target] == 0:
				queue.append(target)

	if len(ordered) != len(subjects):
		fallback_order = sorted(subjects.values(), key=lambda row: (row["semester"], row["code"]))
		ordered = fallback_order

	grouped = defaultdict(list)
	for row in ordered:
		grouped[row["semester"]].append(row)

	return {
		"from": f"sem{start_semester}",
		"to": f"sem{end_semester}",
		"subjects": ordered,
		"by_semester": [
			{"semester": semester, "subjects": grouped[semester]}
			for semester in sorted(grouped)
		],
		"prerequisite_edges": edge_rows,
	}


@app.get("/health")
def health_check():
	return {"status": "ok"}


@app.get("/subject/{code}/topics")
def get_subject_topics(code: str):
	driver = get_driver()
	return fetch_subject_topics(driver, code)


@app.get("/topic/{name}/related")
def get_topic_related(name: str):
	driver = get_driver()
	return fetch_related_topics(driver, name)


@app.get("/subject/{code}/resources")
def get_subject_resources(code: str):
	return fetch_subject_resources(code)


@app.get("/subject/{code}/prerequisites")
def get_subject_prerequisites(code: str):
	driver = get_driver()
	return fetch_subject_prerequisites(driver, code)


@app.post("/chat")
def chat(payload: ChatRequest):
	try:
		result = rag_ask(payload.question, payload.subject_code.strip().upper())
	except ValueError as error:
		raise HTTPException(status_code=404, detail=str(error))

	graph_context = result.get("graph_context_used", {})
	return {
		"answer": result.get("answer", ""),
		"sources": result.get("sources", []),
		"prereqs_used": graph_context.get("prerequisites", []),
	}


@app.get("/path")
def get_learning_path(
	from_semester: str = Query(..., alias="from"),
	to_semester: str = Query(..., alias="to"),
):
	start_semester = parse_semester_label(from_semester)
	end_semester = parse_semester_label(to_semester)

	if start_semester > end_semester:
		raise HTTPException(status_code=400, detail="from semester must be less than or equal to to semester")

	driver = get_driver()
	return topological_learning_path(driver, start_semester, end_semester)


if __name__ == "__main__":
	import uvicorn

	uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
