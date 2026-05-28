"""Shared logging helpers for the FastAPI applications."""

from __future__ import annotations

import logging
import os
import time
import uuid
from logging.handlers import RotatingFileHandler

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOG_DIR = os.path.join(PROJECT_ROOT, "logs")


def build_logger(name: str, file_prefix: str) -> logging.Logger:
	os.makedirs(LOG_DIR, exist_ok=True)
	logger = logging.getLogger(name)
	logger.setLevel(logging.INFO)
	logger.propagate = False

	for handler in list(logger.handlers):
		handler.close()
		logger.removeHandler(handler)

	formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
	log_path = os.path.join(LOG_DIR, f"{file_prefix}.log")
	error_log_path = os.path.join(LOG_DIR, f"{file_prefix}.errors.log")

	stream_handler = logging.StreamHandler()
	stream_handler.setFormatter(formatter)

	file_handler = RotatingFileHandler(
		log_path,
		maxBytes=1_048_576,
		backupCount=5,
		encoding="utf-8",
	)
	file_handler.setFormatter(formatter)

	error_handler = RotatingFileHandler(
		error_log_path,
		maxBytes=1_048_576,
		backupCount=5,
		encoding="utf-8",
	)
	error_handler.setLevel(logging.ERROR)
	error_handler.setFormatter(formatter)

	logger.addHandler(stream_handler)
	logger.addHandler(file_handler)
	logger.addHandler(error_handler)
	return logger


def install_request_logging(app, logger: logging.Logger):
	@app.middleware("http")
	async def request_logging_middleware(request: Request, call_next):
		request_id = uuid.uuid4().hex[:8]
		request.state.request_id = request_id
		started_at = time.perf_counter()
		query_string = request.url.query or "-"
		client_host = request.client.host if request.client else "-"
		logger.info(
			"request_started request_id=%s method=%s path=%s query=%s client=%s",
			request_id,
			request.method,
			request.url.path,
			query_string,
			client_host,
		)

		try:
			response = await call_next(request)
		except Exception:
			duration_ms = (time.perf_counter() - started_at) * 1000.0
			logger.exception(
				"request_failed request_id=%s method=%s path=%s duration_ms=%.2f",
				request_id,
				request.method,
				request.url.path,
				duration_ms,
			)
			raise

		duration_ms = (time.perf_counter() - started_at) * 1000.0
		response.headers["X-Request-ID"] = request_id
		logger.info(
			"request_completed request_id=%s method=%s path=%s status=%s duration_ms=%.2f",
			request_id,
			request.method,
			request.url.path,
			response.status_code,
			duration_ms,
		)
		return response

	@app.exception_handler(HTTPException)
	async def http_exception_handler(request: Request, exc: HTTPException):
		request_id = getattr(request.state, "request_id", "-")
		logger.warning(
			"http_exception request_id=%s method=%s path=%s status=%s detail=%s",
			request_id,
			request.method,
			request.url.path,
			exc.status_code,
			exc.detail,
		)
		return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

	@app.exception_handler(RequestValidationError)
	async def validation_exception_handler(request: Request, exc: RequestValidationError):
		request_id = getattr(request.state, "request_id", "-")
		logger.warning(
			"validation_error request_id=%s method=%s path=%s errors=%s",
			request_id,
			request.method,
			request.url.path,
			exc.errors(),
		)
		return JSONResponse(status_code=422, content={"detail": exc.errors()})

	@app.exception_handler(Exception)
	async def unhandled_exception_handler(request: Request, exc: Exception):
		request_id = getattr(request.state, "request_id", "-")
		logger.exception(
			"unhandled_error request_id=%s method=%s path=%s",
			request_id,
			request.method,
			request.url.path,
		)
		return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})

	return logger