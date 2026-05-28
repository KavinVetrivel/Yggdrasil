"""FastAPI query layer for the curriculum knowledge graph."""

from collections import defaultdict, deque
from functools import lru_cache
import csv
import os
import re
import sys
import tempfile
from typing import Dict, List

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

if __package__ in {None, ""}:
	sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
	from config import CHUNK_OVERLAP_TOKENS, CHUNK_SIZE_TOKENS
	from auth_store import authenticate_user, get_user, register_user
	from load_data import load_regulation_graph
	from logging_utils import build_logger, install_request_logging
	from parser import parse_file
	from graph_store import get_student_profile, upsert_student_profile
	from vector_store import upsert_chunks
	from rag_pipeline import ask as rag_ask
else:
	from .config import CHUNK_OVERLAP_TOKENS, CHUNK_SIZE_TOKENS
	from .auth_store import authenticate_user, get_user, register_user
	from .load_data import load_regulation_graph
	from .logging_utils import build_logger, install_request_logging
	from .parser import parse_file
	from .graph_store import get_student_profile, upsert_student_profile
	from .vector_store import upsert_chunks
	from .rag_pipeline import ask as rag_ask

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
SUBJECT_CSV_FIELDNAMES = [
	"code",
	"name",
	"semester",
	"lecture_hrs",
	"tutorial_hrs",
	"practical_hrs",
	"credits",
	"category",
	"type",
	"prerequisites",
]
DEFAULT_MANUAL_CATEGORY = "MC"
DEFAULT_MANUAL_TYPE = "Theory"

app = FastAPI(title="Yggdrasil Curriculum API", version="1.0.0")
app.add_middleware(
	CORSMiddleware,
	allow_origins=["*"],
	allow_credentials=False,
	allow_methods=["*"],
	allow_headers=["*"],
)
logger = build_logger("yggdrasil.api", "api")
install_request_logging(app, logger)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(PROJECT_ROOT, "frontend")
NOTEBOOK_INDEX = os.path.join(FRONTEND_DIR, "curriculum_notebook.html")
UPLOAD_INDEX = os.path.join(FRONTEND_DIR, "upload_resources.html")
REGULATION_UPLOAD_INDEX = os.path.join(FRONTEND_DIR, "regulation_upload.html")
_driver = None
_subject_resource_index = None


class ChatRequest(BaseModel):
	query: str | None = None
	question: str | None = None
	subject_id: str | None = None
	subject_code: str | None = None
	history: List[dict] = Field(default_factory=list)


class CreateSubjectRequest(BaseModel):
	code: str
	name: str
	semester: int
	credits: int
	prerequisites: List[str] = []


class RegisterUserRequest(BaseModel):
	user_id: str
	password: str
	email: str | None = None


class LoginUserRequest(BaseModel):
	user_id: str
	password: str


class StudentProfileRequest(BaseModel):
	user_id: str
	college_id: str
	regulation_id: str
	program_id: str
	semester_number: int
	email: str | None = None
	college_name: str | None = None
	regulation_name: str | None = None
	program_name: str | None = None


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


def derive_chunk_profile(file_size_bytes, ext):
	file_size_mb = max(float(file_size_bytes) / (1024.0 * 1024.0), 0.1)
	size_scale = max(0.65, min(2.75, file_size_mb ** 0.5))
	chunk_size = int(CHUNK_SIZE_TOKENS / size_scale)
	chunk_size = max(120, min(700, chunk_size))
	overlap = max(40, min(120, int(chunk_size * 0.2)))
	if ext in {"ppt", "pptx"}:
		chunk_size = max(100, int(chunk_size * 0.85))
		chunk_size = min(chunk_size, 600)
		if file_size_mb > 3:
			chunk_size = max(90, int(chunk_size * 0.9))
		overlap = max(30, min(100, int(chunk_size * 0.15)))
	return chunk_size, overlap


def normalize_subject_code(value):
	return normalize_whitespace(value).upper().replace(" ", "")


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


def read_subject_csv_rows(path):
	if not os.path.exists(path):
		return []

	with open(path, newline="", encoding="utf-8") as file_handle:
		reader = csv.DictReader(file_handle)
		return list(reader)


def find_subject_csv_row(code):
	normalized_code = normalize_subject_code(code)
	for index, row in enumerate(read_subject_csv_rows(CSV_PATH)):
		if normalize_subject_code(row.get("code")) == normalized_code:
			return index, row
	return None, None


def subject_code_exists_in_csv(code):
	_, row = find_subject_csv_row(code)
	return row is not None


def upsert_subject_csv_record(record):
	rows = read_subject_csv_rows(CSV_PATH)
	fieldnames = list(rows[0].keys()) if rows else list(SUBJECT_CSV_FIELDNAMES)
	if "prerequisites" not in fieldnames:
		fieldnames.append("prerequisites")

	updated_rows = []
	found = False
	for row in rows:
		row_code = normalize_subject_code(row.get("code"))
		if row_code == record["code"]:
			updated_rows.append(record)
			found = True
		else:
			row.setdefault("prerequisites", "")
			updated_rows.append(row)

	if not found:
		updated_rows.append(record)

	with tempfile.NamedTemporaryFile("w", delete=False, newline="", encoding="utf-8") as tmp_file:
		writer = csv.DictWriter(tmp_file, fieldnames=fieldnames)
		writer.writeheader()
		for row in updated_rows:
			writer.writerow({field: row.get(field, "") for field in fieldnames})
		tmp_path = tmp_file.name

	os.replace(tmp_path, CSV_PATH)


def remove_subject_from_csv(code):
	rows = read_subject_csv_rows(CSV_PATH)
	if not rows:
		return False

	fieldnames = list(rows[0].keys())
	if "prerequisites" not in fieldnames:
		fieldnames.append("prerequisites")

	normalized_code = normalize_subject_code(code)
	updated_rows = []
	removed = False
	for row in rows:
		row_code = normalize_subject_code(row.get("code"))
		if row_code == normalized_code:
			removed = True
			continue
		prerequisites = parse_prerequisite_codes(row.get("prerequisites", ""))
		prerequisites = [prereq for prereq in prerequisites if prereq != normalized_code]
		row["prerequisites"] = ",".join(prerequisites)
		updated_rows.append(row)

	if removed or len(updated_rows) != len(rows):
		with tempfile.NamedTemporaryFile("w", delete=False, newline="", encoding="utf-8") as tmp_file:
			writer = csv.DictWriter(tmp_file, fieldnames=fieldnames)
			writer.writeheader()
			for row in updated_rows:
				writer.writerow({field: row.get(field, "") for field in fieldnames})
			tmp_path = tmp_file.name

		os.replace(tmp_path, CSV_PATH)

	return removed


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


def cleanup_orphan_subjects():
	driver = get_driver()
	with driver.session() as session:
		session.run(
			"""
			MATCH (s:Subject)
			WHERE NOT (s)-[:BELONGS_TO]->(:Semester)
			DETACH DELETE s
			"""
		)


@app.on_event("startup")
def startup_cleanup():
	try:
		cleanup_orphan_subjects()
	except HTTPException:
		return


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


def resolve_prerequisite_subjects(prerequisite_codes, semester):
	driver = get_driver()
	resolved = []
	seen = set()
	with driver.session() as session:
		for prerequisite_code in prerequisite_codes:
			row = session.run(
				"""
				MATCH (s:Subject {code: $code})
				OPTIONAL MATCH (s)-[:BELONGS_TO]->(sem:Semester)
				RETURN s.code AS code, s.name AS name, sem.number AS semester
				""",
				code=prerequisite_code,
			).single()
			if not row:
				raise HTTPException(status_code=400, detail=f"Prerequisite subject not found: {prerequisite_code}")
			if row["semester"] is None or int(row["semester"]) >= int(semester):
				raise HTTPException(status_code=400, detail=f"Prerequisite {prerequisite_code} must be in an earlier semester")
			normalized_code = normalize_subject_code(row["code"])
			if normalized_code in seen:
				continue
			seen.add(normalized_code)
			resolved.append({"code": row["code"], "name": row["name"], "semester": row["semester"]})
	return resolved


def create_subject_in_graph(subject, prerequisite_codes):
	driver = get_driver()
	with driver.session() as session:
		session.run(
			"""
			MERGE (s:Subject {code: $code})
			SET s.name = $name,
				s.semester = $semester,
				s.lecture_hrs = $lecture_hrs,
				s.tutorial_hrs = $tutorial_hrs,
				s.practical_hrs = $practical_hrs,
				s.credits = $credits,
				s.category = $category,
				s.type = $type,
				s.updated_at = datetime()
			WITH s
				MERGE (sem:Semester {number: $semester})
				SET sem.number = $semester,
					sem.updated_at = datetime()
			MERGE (s)-[:BELONGS_TO]->(sem)
			WITH s
			UNWIND $prerequisites AS prerequisite_code
			MATCH (pre:Subject {code: prerequisite_code})
			MERGE (pre)-[:PREREQUISITE_OF]->(s)
			""",
			code=subject["code"],
			name=subject["name"],
			semester=subject["semester"],
			lecture_hrs=subject["lecture_hrs"],
			tutorial_hrs=subject["tutorial_hrs"],
			practical_hrs=subject["practical_hrs"],
			credits=subject["credits"],
			category=subject["category"],
			type=subject["type"],
			prerequisites=prerequisite_codes,
		)


def delete_subject_in_graph(code):
	driver = get_driver()
	with driver.session() as session:
		subject_row = session.run(
			"MATCH (s:Subject {code: $code}) RETURN s.code AS code",
			code=code,
		).single()
		if not subject_row:
			return False

		session.run(
			"""
			MATCH (s:Subject {code: $code})-[:HAS_RESOURCE]->(r:Resource)
			DETACH DELETE r
			""",
			code=code,
		)
		session.run(
			"""
			MATCH (s:Subject {code: $code})-[:HAS_TOPIC]->(t:Topic)
			DETACH DELETE t
			""",
			code=code,
		)
		session.run(
			"""
			MATCH (s:Subject {code: $code})-[:HAS_UNIT]->(u:Unit)
			DETACH DELETE u
			""",
			code=code,
		)
		session.run(
			"""
			MATCH (s:Subject {code: $code})
			DETACH DELETE s
			""",
			code=code,
		)
		return True


@app.post("/subjects")
def create_subject(payload: CreateSubjectRequest):
	code = normalize_subject_code(payload.code)
	name = normalize_whitespace(payload.name)
	if not code:
		raise HTTPException(status_code=400, detail="Subject code is required")
	if not name:
		raise HTTPException(status_code=400, detail="Subject name is required")
	if payload.semester < 1 or payload.semester > 8:
		raise HTTPException(status_code=400, detail="Semester must be between 1 and 8")
	if payload.credits < 0:
		raise HTTPException(status_code=400, detail="Credits must be zero or greater")
	_, csv_row = find_subject_csv_row(code)
	if csv_row is not None:
		csv_semester = csv_row.get("semester", "")
		csv_name = csv_row.get("name", "")
		print(
			f"[Subject Create] Recreating code {code} from CSV row: semester={csv_semester}, name={csv_name!r}. "
			"The code is not present in the active graph, so this will overwrite the CSV row."
		)

	prerequisite_codes = parse_prerequisite_codes(payload.prerequisites)
	resolved_prerequisites = resolve_prerequisite_subjects(prerequisite_codes, payload.semester)
	subject_record = {
		"code": code,
		"name": name,
		"semester": int(payload.semester),
		"lecture_hrs": int(payload.credits),
		"tutorial_hrs": 0,
		"practical_hrs": 0,
		"credits": int(payload.credits),
		"category": DEFAULT_MANUAL_CATEGORY,
		"type": DEFAULT_MANUAL_TYPE,
		"prerequisites": ",".join(prerequisite_codes),
	}

	create_subject_in_graph(subject_record, [item["code"] for item in resolved_prerequisites])
	upsert_subject_csv_record(subject_record)

	created_subject = get_subject(code)
	if created_subject is None:
		raise HTTPException(status_code=500, detail="Subject was created in the graph but could not be reloaded")

	return {
		"status": "success",
		"subject": created_subject,
		"prerequisites": resolved_prerequisites,
	}


@app.delete("/subject/{code}")
def delete_subject(code: str):
	normalized_code = normalize_subject_code(code)
	graph_subject = get_subject(normalized_code)
	deleted_from_graph = False
	if graph_subject is not None:
		deleted_from_graph = delete_subject_in_graph(normalized_code)

	deleted_from_csv = remove_subject_from_csv(normalized_code)

	if not deleted_from_graph and not deleted_from_csv:
		raise HTTPException(status_code=404, detail=f"Subject code '{normalized_code}' was not found in the graph or CSV.")

	return {
		"status": "success",
		"code": normalized_code,
		"deleted_from_graph": deleted_from_graph,
		"deleted_from_csv": deleted_from_csv,
	}


@app.post("/auth/register")
def register_auth_user(payload: RegisterUserRequest):
	try:
		record = register_user(payload.user_id, payload.password, payload.email)
	except ValueError as error:
		message = str(error)
		status_code = 409 if "exists" in message.lower() else 400
		raise HTTPException(status_code=status_code, detail=message)

	return {
		"status": "success",
		"user": {
			"user_id": record.user_id,
			"email": record.email,
			"created_at": record.created_at,
		},
	}


@app.post("/auth/login")
def login_auth_user(payload: LoginUserRequest):
	try:
		record = authenticate_user(payload.user_id, payload.password)
	except ValueError as error:
		raise HTTPException(status_code=401, detail=str(error))

	return {
		"status": "success",
		"user": {
			"user_id": record.user_id,
			"email": record.email,
			"created_at": record.created_at,
		},
	}


@app.post("/students")
def upsert_student(payload: StudentProfileRequest):
	user = get_user(payload.user_id)
	if user is None:
		raise HTTPException(status_code=404, detail=f"User '{payload.user_id}' was not found in PostgreSQL")

	email = payload.email or user.email or ""
	profile = upsert_student_profile(
		student_id=payload.user_id,
		email=email,
		college_id=payload.college_id,
		regulation_id=payload.regulation_id,
		program_id=payload.program_id,
		semester_number=payload.semester_number,
		program_name=payload.program_name,
		college_name=payload.college_name,
		regulation_name=payload.regulation_name,
	)

	return {
		"status": "success",
		"student": profile,
	}


@app.get("/students/{student_id}")
def get_student(student_id: str):
	profile = get_student_profile(student_id)
	if profile is None:
		raise HTTPException(status_code=404, detail=f"Student '{student_id}' was not found")
	return {"status": "success", "student": profile}


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
		file_size_bytes = os.path.getsize(tmp_path)
		chunk_size, overlap = derive_chunk_profile(file_size_bytes, ext)
		chunks = parse_file(tmp_path, chunk_size=chunk_size, overlap=overlap)
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
	driver = get_driver()
	with driver.session() as session:
		subject_row = session.run(
			"""
			MATCH (s:Subject {code: $code})
			RETURN s.code AS code, s.name AS name
			""",
			code=code,
		).single()
		if not subject_row:
			raise HTTPException(status_code=404, detail=f"Subject not found: {code}")

		resources = session.run(
			"""
			MATCH (s:Subject {code: $code})-[:HAS_RESOURCE]->(r:Resource)
			RETURN r.source_file AS source_file,
			       r.source_type AS source_type,
			       r.chunk_count AS chunk_count,
			       r.updated_at AS updated_at
			ORDER BY r.updated_at DESC, r.source_file ASC
			""",
			code=code,
		).data()

	return {
		"code": code,
		"subject": {"code": subject_row["code"], "name": subject_row["name"]},
		"resources": resources,
	}


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
	question = normalize_whitespace(payload.query or payload.question)
	subject_code = normalize_whitespace(payload.subject_id or payload.subject_code).upper()
	if not question:
		raise HTTPException(status_code=400, detail="Missing chat query.")
	if not subject_code:
		raise HTTPException(status_code=400, detail="Missing subject identifier.")

	try:
		result = rag_ask(question, subject_code, history=payload.history)
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

	module_name = "api" if __package__ in {None, ""} else f"{__package__}.api"
	uvicorn.run(f"{module_name}:app", host="0.0.0.0", port=8000, reload=True)
