"""PostgreSQL-backed authentication storage for user credentials."""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

try:
	import psycopg
except ImportError:  # pragma: no cover - runtime dependency issue
	psycopg = None

if __package__ in {None, ""}:
	import sys
	sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
	from config import POSTGRES_DB, POSTGRES_HOST, POSTGRES_PASSWORD, POSTGRES_PORT, POSTGRES_SSLMODE, POSTGRES_USER
else:
	from .config import POSTGRES_DB, POSTGRES_HOST, POSTGRES_PASSWORD, POSTGRES_PORT, POSTGRES_SSLMODE, POSTGRES_USER


PBKDF2_ITERATIONS = 310_000


@dataclass(frozen=True)
class AuthRecord:
	user_id: str
	email: str | None
	created_at: datetime | None


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
					user_id TEXT PRIMARY KEY,
					email TEXT UNIQUE,
					password_hash TEXT NOT NULL,
					password_salt TEXT NOT NULL,
					created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
				)
				"""
			)
		connection.commit()


def _hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
	if not password:
		raise ValueError("Password is required")
	if salt is None:
		salt = secrets.token_bytes(16)
	digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
	return digest.hex(), salt.hex()


def _verify_password(password: str, password_hash: str, password_salt: str) -> bool:
	computed_hash, _ = _hash_password(password, bytes.fromhex(password_salt))
	return hmac.compare_digest(computed_hash, password_hash)


def register_user(user_id: str, password: str, email: str | None = None) -> AuthRecord:
	user_id = (user_id or "").strip()
	email = (email or "").strip() or None
	if not user_id:
		raise ValueError("User ID is required")
	password_hash, password_salt = _hash_password(password)
	initialize_schema()
	with get_connection() as connection:
		with connection.cursor() as cursor:
			cursor.execute("SELECT 1 FROM users WHERE user_id = %s OR (%s IS NOT NULL AND email = %s)", (user_id, email, email))
			if cursor.fetchone():
				raise ValueError("User ID or email already exists")
			cursor.execute(
				"""
				INSERT INTO users (user_id, email, password_hash, password_salt, created_at)
				VALUES (%s, %s, %s, %s, %s)
				""",
				(user_id, email, password_hash, password_salt, datetime.now(timezone.utc)),
			)
		connection.commit()
	return AuthRecord(user_id=user_id, email=email, created_at=datetime.now(timezone.utc))


def authenticate_user(user_id: str, password: str) -> AuthRecord:
	user_id = (user_id or "").strip()
	if not user_id:
		raise ValueError("User ID is required")
	initialize_schema()
	with get_connection() as connection:
		with connection.cursor() as cursor:
			cursor.execute(
				"SELECT user_id, email, password_hash, password_salt, created_at FROM users WHERE user_id = %s",
				(user_id,),
			)
			row = cursor.fetchone()
	if not row:
		raise ValueError("Invalid user ID or password")
	if not _verify_password(password, row[2], row[3]):
		raise ValueError("Invalid user ID or password")
	return AuthRecord(user_id=row[0], email=row[1], created_at=row[4])


def get_user(user_id: str) -> Optional[AuthRecord]:
	user_id = (user_id or "").strip()
	if not user_id:
		return None
	initialize_schema()
	with get_connection() as connection:
		with connection.cursor() as cursor:
			cursor.execute(
				"SELECT user_id, email, created_at FROM users WHERE user_id = %s",
				(user_id,),
			)
			row = cursor.fetchone()
	if not row:
		return None
	return AuthRecord(user_id=row[0], email=row[1], created_at=row[2])