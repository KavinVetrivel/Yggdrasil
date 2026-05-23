import os

from dotenv import load_dotenv


load_dotenv()


def _env(name, default=None):
	return os.getenv(name, default)


def parse_int_env(name, default):
	raw_value = _env(name)
	if raw_value is None:
		return default
	try:
		return int(raw_value)
	except (TypeError, ValueError):
		return default


# Neo4j
NEO4J_URI = _env("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USERNAME = _env("NEO4J_USERNAME", _env("NEO4J_USER", "neo4j"))
NEO4J_USER = NEO4J_USERNAME
NEO4J_PASSWORD = _env("NEO4J_PASSWORD", "password")

# ChromaDB
CHROMA_BACKEND = _env("CHROMA_BACKEND", "cloud").strip().lower()
CHROMA_PERSIST_DIR = _env("CHROMA_PERSIST_DIR", _env("CHROMA_DIR", "./chroma_store"))
CHROMA_DIR = CHROMA_PERSIST_DIR
CHROMA_CLOUD_API_KEY = _env("CHROMA_API_KEY")
CHROMA_CLOUD_TENANT = _env("CHROMA_TENANT")
CHROMA_CLOUD_DATABASE = _env("CHROMA_DATABASE")
CHROMA_CLOUD_HOST = _env("CHROMA_HOST", "api.trychroma.com")
CHROMA_CLOUD_PORT = parse_int_env("CHROMA_PORT", 443)
CHROMA_CLOUD_SSL = _env("CHROMA_SSL", "true").strip().lower() not in {"0", "false", "no", "off"}
CHROMA_COLLECTION_NAME = _env("CHROMA_COLLECTION_NAME", "resources")
CHROMA_EMBEDDING_DIMENSIONS = parse_int_env("CHROMA_EMBEDDING_DIMENSIONS", 384)
SENTENCE_TRANSFORMERS_MODEL = _env("SENTENCE_TRANSFORMERS_MODEL", "sentence-transformers/all-MiniLM-L6-v2")

# Chunking
CHUNK_SIZE_TOKENS = parse_int_env("CHUNK_SIZE_TOKENS", 512)
CHUNK_OVERLAP_TOKENS = parse_int_env("CHUNK_OVERLAP_TOKENS", 100)

# Compatibility aliases kept for older modules.
OPENAI_API_KEY = _env("OPENAI_API_KEY")
EMBEDDING_MODEL = _env("EMBEDDING_MODEL", "text-embedding-3-small")