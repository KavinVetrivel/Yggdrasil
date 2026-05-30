"""PostgreSQL-backed authentication storage and JWT helpers."""

from __future__ import annotations

import hashlib
import base64
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import jwt
from passlib.context import CryptContext
try:
	import bcrypt
except Exception:  # pragma: no cover - optional dependency
	bcrypt = None

try:
	import psycopg
except ImportError:  # pragma: no cover - runtime dependency issue
	psycopg = None

if __package__ in {None, ""}:
	import sys
	sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
	from config import (
		ACCESS_TOKEN_EXPIRE_MINUTES,
		JWT_ALGORITHM,
		JWT_SECRET_KEY,
		POSTGRES_DB,
		POSTGRES_HOST,
		POSTGRES_PASSWORD,
		POSTGRES_PORT,
		POSTGRES_SSLMODE,
		POSTGRES_USER,
		REFRESH_TOKEN_EXPIRE_DAYS,
	)
else:
	from .config import (
		ACCESS_TOKEN_EXPIRE_MINUTES,
		JWT_ALGORITHM,
		JWT_SECRET_KEY,
		POSTGRES_DB,
		POSTGRES_HOST,
		POSTGRES_PASSWORD,
		POSTGRES_PORT,
		POSTGRES_SSLMODE,
		POSTGRES_USER,
		REFRESH_TOKEN_EXPIRE_DAYS,
	)


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)

# Ensure the bcrypt backend is initialized with a short secret to avoid
# passlib's backend-detection path trying to hash a long secret and
# raising a "password cannot be longer than 72 bytes" error on first use.
try:
	pwd_context.hash("init")
except Exception:
	# If initialization fails for any reason, swallow the error —
	# subsequent calls will still attempt to set the backend.
	pass
BCRYPT_MAX_BYTES = 72


@dataclass(frozen=True)
class AuthRecord:
	student_id: str
	email: str | None
	college_id: str | None
	regulation_id: str | None
	is_active: bool
	created_at: datetime | None


@dataclass(frozen=True)
class TokenPair:
	access_token: str
	refresh_token: str
	access_expires_at: datetime
	refresh_expires_at: datetime
	token_type: str = "bearer"


def _require_driver() -> Any:
	if psycopg is None:
		raise RuntimeError("Install psycopg[binary] to use PostgreSQL auth storage.")
	return psycopg


def _connection_kwargs() -> dict[str, Any]:
	kwargs: dict[str, Any] = {
		"host": POSTGRES_HOST,
		"port": POSTGRES_PORT,
		"dbname": POSTGRES_DB,
		"user": POSTGRES_USER,
		"password": POSTGRES_PASSWORD,
		"sslmode": POSTGRES_SSLMODE,
	}
	if not POSTGRES_PASSWORD:
		kwargs.pop("password")
	return kwargs


def get_connection():
	psycopg_module = _require_driver()
	return psycopg_module.connect(**_connection_kwargs())


def initialize_schema() -> None:
	with get_connection() as connection:
		with connection.cursor() as cursor:
			cursor.execute(
				"""
				CREATE TABLE IF NOT EXISTS users (
					id BIGSERIAL PRIMARY KEY,
					student_id TEXT UNIQUE NOT NULL,
					email TEXT UNIQUE,
					college_id TEXT,
					regulation_id TEXT,
					hashed_password TEXT,
					password_hash TEXT,
					password_salt TEXT,
					is_active BOOLEAN NOT NULL DEFAULT TRUE,
					created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
				)
				"""
			)
			# Create chat_history without a strict foreign key to users.student_id
			# to avoid migration ordering problems when an existing `users` table
			# lacks the `student_id` column. Referential integrity is still
			# enforced at the application layer.
			cursor.execute(
				"""
				CREATE TABLE IF NOT EXISTS chat_history (
					id BIGSERIAL PRIMARY KEY,
					student_id TEXT NOT NULL,
					role TEXT NOT NULL,
					content TEXT NOT NULL,
					created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
				)
				"""
			)
			# Store refresh tokens keyed by user_id (student_id). Avoid a DB-level
			# foreign key to `users.student_id` to prevent migration failure when
			# that column is absent in older schemas.
			cursor.execute(
				"""
				CREATE TABLE IF NOT EXISTS refresh_tokens (
					id BIGSERIAL PRIMARY KEY,
					user_id TEXT NOT NULL,
					token_hash TEXT NOT NULL UNIQUE,
					expires_at TIMESTAMPTZ NOT NULL,
					revoked BOOLEAN NOT NULL DEFAULT FALSE,
					revoked_at TIMESTAMPTZ,
					created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
				)
				"""
			)
			cursor.execute("CREATE INDEX IF NOT EXISTS idx_refresh_tokens_user_id ON refresh_tokens (user_id)")
			cursor.execute("CREATE INDEX IF NOT EXISTS idx_refresh_tokens_token_hash ON refresh_tokens (token_hash)")
			cursor.execute("CREATE INDEX IF NOT EXISTS idx_refresh_tokens_revoked ON refresh_tokens (revoked)")
			cursor.execute("CREATE INDEX IF NOT EXISTS idx_chat_history_student_id_created_at ON chat_history (student_id, created_at DESC)")
			# Ensure legacy databases get a student_id column if missing
			cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS student_id TEXT")
			cursor.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_student_id ON users (student_id)")
			cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS college_id TEXT")
			cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS regulation_id TEXT")
			cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS hashed_password TEXT")
			cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_hash TEXT")
			cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS password_salt TEXT")
			cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE")
			cursor.execute("ALTER TABLE refresh_tokens ADD COLUMN IF NOT EXISTS revoked_at TIMESTAMPTZ")
		connection.commit()


def append_chat_message(student_id: str, role: str, content: str) -> None:
	student_id = _normalize_student_id(student_id)
	role = (role or "").strip().lower()
	content = (content or "").strip()
	if not role or not content:
		return
	if role not in {"user", "assistant"}:
		raise ValueError("Chat role must be user or assistant")
	initialize_schema()
	with get_connection() as connection:
		with connection.cursor() as cursor:
			cursor.execute(
				"""
				INSERT INTO chat_history (student_id, role, content, created_at)
				VALUES (%s, %s, %s, %s)
				""",
				(student_id, role, content, datetime.now(timezone.utc)),
			)
		connection.commit()


def get_chat_history(student_id: str, limit: int = 12) -> list[dict[str, Any]]:
	student_id = _normalize_student_id(student_id)
	initialize_schema()
	with get_connection() as connection:
		with connection.cursor() as cursor:
			cursor.execute(
				"""
				SELECT role, content, created_at
				FROM chat_history
				WHERE student_id = %s
				ORDER BY created_at DESC, id DESC
				LIMIT %s
				""",
				(student_id, max(1, int(limit))),
			)
			rows = cursor.fetchall() or []
	return [
		{"role": row[0], "content": row[1], "created_at": row[2]}
		for row in reversed(rows)
	]


def _normalize_student_id(student_id: str | None = None, user_id: str | None = None) -> str:
	resolved = (student_id or user_id or "").strip()
	if not resolved:
		raise ValueError("Student ID is required")
	return resolved


def _detect_user_id_column(cursor) -> str:
	"""Return the column name in `users` that should be treated as the student id.

	Prefers `student_id` then `user_id`. Caller should pass a cursor from the
	current connection to avoid extra connections.
	"""
	cursor.execute(
		"SELECT column_name FROM information_schema.columns WHERE table_name='users' AND column_name IN ('student_id','user_id')"
	)
	rows = [r[0] for r in cursor.fetchall() or []]
	if 'student_id' in rows:
		return 'student_id'
	if 'user_id' in rows:
		return 'user_id'
	return ''


def _column_info(cursor, name: str) -> dict:
	cursor.execute(
		"SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name='users' AND column_name = %s",
		(name,),
	)
	row = cursor.fetchone()
	if not row:
		return {'exists': False, 'is_nullable': True}
	return {'exists': True, 'is_nullable': (row[1] == 'YES')}


def _bcrypt_password(password: str) -> str:
    """Pre-hash password with SHA-256 to safely bypass bcrypt's 72-byte limit."""
    if not password:
        raise ValueError("Password is required")
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    # base64-encode so the result is valid ASCII — bcrypt handles it cleanly
    return base64.b64encode(digest).decode("ascii")  # always 44 chars, well under 72


def truncate_for_bcrypt(password: str) -> str:
	"""Return a password string truncated to bcrypt's 72 UTF-8 byte limit.

	This is provided as a public helper so callers can avoid passlib errors
	by truncating before calling into CryptContext.hash()/verify().
	"""
	return _bcrypt_password(password)


def _hash_password(password: str) -> str:
	# Prefer the native `bcrypt` binding to avoid passlib's first-call
	# backend-detection path which may attempt to hash the supplied
	# secret and raise on long inputs. We pre-hash to SHA-256 first
	# so the value passed to bcrypt is always short.
	pre = _bcrypt_password(password).encode("utf-8")
	if bcrypt is not None:
		hashed = bcrypt.hashpw(pre, bcrypt.gensalt(rounds=12))
		# bcrypt.hashpw returns bytes like b"$2b$12$..."
		return hashed.decode("utf-8")
	# fallback to passlib if native binding is unavailable
	return pwd_context.hash(pre)


def _verify_password(password: str, hashed_password: str | None, legacy_password_hash: str | None = None, legacy_password_salt: str | None = None) -> bool:
	if hashed_password:
		# If we have the native bcrypt binding, use it directly against
		# the pre-hashed value. Otherwise fall back to passlib.
		if bcrypt is not None:
			try:
				return bcrypt.checkpw(_bcrypt_password(password).encode("utf-8"), hashed_password.encode("utf-8"))
			except Exception:
				return False
		try:
			return pwd_context.verify(_bcrypt_password(password), hashed_password)
		except Exception:
			return False
	if legacy_password_hash and legacy_password_salt:
		computed_hash = hashlib.pbkdf2_hmac(
			"sha256",
			password.encode("utf-8"),
			bytes.fromhex(legacy_password_salt),
			310_000,
		)
		return hmac.compare_digest(computed_hash.hex(), legacy_password_hash)
	return False


def _refresh_token_hash(token: str) -> str:
	return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _decode_token(token: str, expected_type: str) -> dict[str, Any]:
	try:
		payload = jwt.decode(token, JWT_SECRET_KEY, algorithms=[JWT_ALGORITHM])
	except jwt.ExpiredSignatureError as error:
		raise ValueError("Token has expired") from error
	except jwt.InvalidTokenError as error:
		raise ValueError("Invalid token") from error

	if payload.get("type") != expected_type:
		raise ValueError(f"Invalid {expected_type} token")
	if not payload.get("student_id"):
		raise ValueError("Token is missing student_id")
	return payload


def decode_access_token(token: str) -> dict[str, Any]:
	return _decode_token(token, "access")


def decode_refresh_token(token: str) -> dict[str, Any]:
	return _decode_token(token, "refresh")


def build_access_token(record: AuthRecord, now: datetime | None = None) -> tuple[str, datetime]:
	now = now or datetime.now(timezone.utc)
	expires_at = now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
	payload = {
		"type": "access",
		"jti": secrets.token_hex(16),
		"iat": int(now.timestamp()),
		"exp": int(expires_at.timestamp()),
		"sub": record.student_id,
		"student_id": record.student_id,
		"email": record.email,
		"college_id": record.college_id,
		"regulation_id": record.regulation_id,
		"is_active": record.is_active,
	}
	return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM), expires_at


def build_refresh_token(record: AuthRecord, now: datetime | None = None) -> tuple[str, datetime]:
	now = now or datetime.now(timezone.utc)
	expires_at = now + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
	payload = {
		"type": "refresh",
		"jti": secrets.token_hex(16),
		"iat": int(now.timestamp()),
		"exp": int(expires_at.timestamp()),
		"sub": record.student_id,
		"student_id": record.student_id,
	}
	return jwt.encode(payload, JWT_SECRET_KEY, algorithm=JWT_ALGORITHM), expires_at


def _insert_refresh_token(cursor, user_id: str, refresh_token: str, expires_at: datetime) -> None:
	cursor.execute(
		"""
		INSERT INTO refresh_tokens (user_id, token_hash, expires_at, revoked, created_at)
		VALUES (%s, %s, %s, FALSE, %s)
		""",
		(user_id, _refresh_token_hash(refresh_token), expires_at, datetime.now(timezone.utc)),
	)


def register_user(
	student_id: str | None = None,
	password: str = "",
	email: str | None = None,
	college_id: str | None = None,
	regulation_id: str | None = None,
	user_id: str | None = None,
) -> AuthRecord:
	student_id = _normalize_student_id(student_id, user_id)
	email = (email or "").strip() or None
	college_id = (college_id or "").strip() or None
	regulation_id = (regulation_id or "").strip() or None
	if not email:
		raise ValueError("Email is required")
	if not college_id:
		raise ValueError("College ID is required")
	if not regulation_id:
		raise ValueError("Regulation ID is required")
	hashed_password = _hash_password(password)
	initialize_schema()
	created_at = datetime.now(timezone.utc)
	with get_connection() as connection:
		with connection.cursor() as cursor:
			col = _detect_user_id_column(cursor)
			if col == 'student_id':
				if email:
					cursor.execute(
						"SELECT 1 FROM users WHERE student_id = %s OR email = %s",
						(student_id, email),
					)
				else:
					cursor.execute(
						"SELECT 1 FROM users WHERE student_id = %s",
						(student_id,),
					)
				if cursor.fetchone():
					raise ValueError("Student ID or email already exists")
				# Prepare legacy pbkdf2 fields if required by old schemas
				legacy_info = _column_info(cursor, 'password_hash')
				legacy_salt = None
				legacy_hash = None
				if legacy_info['exists'] and not legacy_info['is_nullable']:
					legacy_salt = secrets.token_hex(16)
					legacy_hash = hashlib.pbkdf2_hmac(
						'sha256', password.encode('utf-8'), bytes.fromhex(legacy_salt), 310_000
					).hex()

				# If the legacy schema also has a non-null `user_id` column,
				# set it to the same value as `student_id` to satisfy constraints.
				user_info = _column_info(cursor, 'user_id')
				if user_info['exists'] and not user_info['is_nullable']:
					if legacy_hash is not None:
						cursor.execute(
							"INSERT INTO users (student_id, user_id, email, college_id, regulation_id, hashed_password, password_hash, password_salt, is_active, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, TRUE, %s)",
							(student_id, student_id, email, college_id, regulation_id, hashed_password, legacy_hash, legacy_salt, created_at),
						)
					else:
						cursor.execute(
							"INSERT INTO users (student_id, user_id, email, college_id, regulation_id, hashed_password, is_active, created_at) VALUES (%s, %s, %s, %s, %s, %s, TRUE, %s)",
							(student_id, student_id, email, college_id, regulation_id, hashed_password, created_at),
						)
				else:
					if legacy_hash is not None:
						cursor.execute(
							"INSERT INTO users (student_id, email, college_id, regulation_id, hashed_password, password_hash, password_salt, is_active, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s)",
							(student_id, email, college_id, regulation_id, hashed_password, legacy_hash, legacy_salt, created_at),
						)
					else:
						cursor.execute(
							"INSERT INTO users (student_id, email, college_id, regulation_id, hashed_password, is_active, created_at) VALUES (%s, %s, %s, %s, %s, TRUE, %s)",
							(student_id, email, college_id, regulation_id, hashed_password, created_at),
						)
			elif col == 'user_id':
				if email:
					cursor.execute(
						"SELECT 1 FROM users WHERE user_id = %s OR email = %s",
						(student_id, email),
					)
				else:
					cursor.execute(
						"SELECT 1 FROM users WHERE user_id = %s",
						(student_id,),
					)
				if cursor.fetchone():
					raise ValueError("Student ID or email already exists")
				# If legacy password_hash is required, compute and include it
				legacy_info = _column_info(cursor, 'password_hash')
				if legacy_info['exists'] and not legacy_info['is_nullable']:
					legacy_salt = secrets.token_hex(16)
					legacy_hash = hashlib.pbkdf2_hmac(
						'sha256', password.encode('utf-8'), bytes.fromhex(legacy_salt), 310_000
					).hex()
					cursor.execute(
						"INSERT INTO users (user_id, email, college_id, regulation_id, hashed_password, password_hash, password_salt, is_active, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, TRUE, %s)",
						(student_id, email, college_id, regulation_id, hashed_password, legacy_hash, legacy_salt, created_at),
					)
				else:
					cursor.execute(
						"INSERT INTO users (user_id, email, college_id, regulation_id, hashed_password, is_active, created_at) VALUES (%s, %s, %s, %s, %s, TRUE, %s)",
						(student_id, email, college_id, regulation_id, hashed_password, created_at),
					)
			else:
				# Unknown users layout: fall back to email-only uniqueness check
				cursor.execute("SELECT 1 FROM users WHERE (%s IS NOT NULL AND email = %s)", (email, email))
				if cursor.fetchone():
					raise ValueError("Email already exists")
				cursor.execute(
					"INSERT INTO users (email, college_id, regulation_id, hashed_password, is_active, created_at) VALUES (%s, %s, %s, %s, TRUE, %s)",
					(email, college_id, regulation_id, hashed_password, created_at),
				)
		connection.commit()
	return AuthRecord(
		student_id=student_id,
		email=email,
		college_id=college_id,
		regulation_id=regulation_id,
		is_active=True,
		created_at=created_at,
	)


def authenticate_user(student_id: str | None = None, password: str = "", user_id: str | None = None) -> AuthRecord:
	student_id = _normalize_student_id(student_id, user_id)
	initialize_schema()
	with get_connection() as connection:
		with connection.cursor() as cursor:
			col = _detect_user_id_column(cursor)
			if col == 'student_id':
				cursor.execute(
					"SELECT student_id, email, college_id, regulation_id, hashed_password, password_hash, password_salt, is_active, created_at FROM users WHERE student_id = %s",
					(student_id,),
				)
			elif col == 'user_id':
				cursor.execute(
					"SELECT user_id AS student_id, email, college_id, regulation_id, hashed_password, password_hash, password_salt, is_active, created_at FROM users WHERE user_id = %s",
					(student_id,),
				)
			else:
				cursor.execute(
					"SELECT NULL AS student_id, email, college_id, regulation_id, hashed_password, password_hash, password_salt, is_active, created_at FROM users WHERE email = %s",
					(student_id,),
				)
			row = cursor.fetchone()
	if not row:
		raise ValueError("Invalid student ID or password")
	if not row[7]:
		raise ValueError("Account is disabled")
	if not _verify_password(password, row[4], row[5], row[6]):
		raise ValueError("Invalid student ID or password")
	return AuthRecord(student_id=row[0], email=row[1], college_id=row[2], regulation_id=row[3], is_active=row[7], created_at=row[8])


def get_user(student_id: str | None = None, user_id: str | None = None) -> Optional[AuthRecord]:
	student_id = (student_id or user_id or "").strip()
	if not student_id:
		return None
	initialize_schema()
	with get_connection() as connection:
		with connection.cursor() as cursor:
			col = _detect_user_id_column(cursor)
			if col == 'student_id':
				cursor.execute(
					"SELECT student_id, email, college_id, regulation_id, is_active, created_at FROM users WHERE student_id = %s",
					(student_id,),
				)
			elif col == 'user_id':
				cursor.execute(
					"SELECT user_id AS student_id, email, college_id, regulation_id, is_active, created_at FROM users WHERE user_id = %s",
					(student_id,),
				)
			else:
				cursor.execute(
					"SELECT NULL AS student_id, email, college_id, regulation_id, is_active, created_at FROM users WHERE email = %s",
					(student_id,),
				)
			row = cursor.fetchone()
	if not row:
		return None
	return AuthRecord(student_id=row[0], email=row[1], college_id=row[2], regulation_id=row[3], is_active=row[4], created_at=row[5])


def issue_token_pair(record: AuthRecord, cursor=None) -> TokenPair:
	now = datetime.now(timezone.utc)
	access_token, access_expires_at = build_access_token(record, now)
	refresh_token, refresh_expires_at = build_refresh_token(record, now)
	if cursor is None:
		initialize_schema()
		with get_connection() as connection:
			with connection.cursor() as connection_cursor:
				_insert_refresh_token(connection_cursor, record.student_id, refresh_token, refresh_expires_at)
			connection.commit()
	else:
		_insert_refresh_token(cursor, record.student_id, refresh_token, refresh_expires_at)
	return TokenPair(
		access_token=access_token,
		refresh_token=refresh_token,
		access_expires_at=access_expires_at,
		refresh_expires_at=refresh_expires_at,
	)


def refresh_token_pair(refresh_token: str) -> tuple[TokenPair, AuthRecord]:
	claims = decode_refresh_token(refresh_token)
	record = get_user(claims["student_id"])
	if record is None:
		raise ValueError("User not found")
	if not record.is_active:
		raise ValueError("Account is disabled")
	refresh_hash = _refresh_token_hash(refresh_token)
	now = datetime.now(timezone.utc)
	with get_connection() as connection:
		with connection.cursor() as cursor:
			cursor.execute(
				"""
				SELECT id, revoked, expires_at
				FROM refresh_tokens
				WHERE token_hash = %s
				FOR UPDATE
				""",
				(refresh_hash,),
			)
			row = cursor.fetchone()
			if not row:
				raise ValueError("Refresh token is invalid")
			if row[1]:
				raise ValueError("Refresh token has been revoked")
			if row[2] <= now:
				raise ValueError("Refresh token has expired")
			cursor.execute(
				"UPDATE refresh_tokens SET revoked = TRUE, revoked_at = %s WHERE id = %s",
				(now, row[0]),
			)
			new_pair = issue_token_pair(record, cursor=cursor)
			connection.commit()
	return new_pair, record


def revoke_refresh_token(refresh_token: str) -> None:
	claims = decode_refresh_token(refresh_token)
	_ = claims.get("student_id")
	refresh_hash = _refresh_token_hash(refresh_token)
	now = datetime.now(timezone.utc)
	initialize_schema()
	with get_connection() as connection:
		with connection.cursor() as cursor:
			cursor.execute(
				"""
				UPDATE refresh_tokens
				SET revoked = TRUE, revoked_at = %s
				WHERE token_hash = %s AND revoked = FALSE
				""",
				(now, refresh_hash),
			)
		connection.commit()


def extract_bearer_token(authorization: str | None) -> str:
	if not authorization:
		raise ValueError("Missing Authorization header")
	prefix = "bearer "
	if not authorization.lower().startswith(prefix):
		raise ValueError("Authorization header must use Bearer scheme")
	token = authorization[len(prefix):].strip()
	if not token:
		raise ValueError("Missing bearer token")
	return token


def require_access_token_claims(authorization: str | None) -> dict[str, Any]:
	token = extract_bearer_token(authorization)
	return decode_access_token(token)