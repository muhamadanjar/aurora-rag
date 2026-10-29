import os

from dotenv import dotenv_values
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


def configure_cors(app: FastAPI, raw_origins: str | None = None) -> None:
    if raw_origins is None:
        raw_origins = os.environ.get("AURORA_CORS_ORIGINS")
        if raw_origins is None:
            raw_origins = dotenv_values(".env").get("AURORA_CORS_ORIGINS") or ""

    origins = [origin.strip() for origin in raw_origins.split(",") if origin.strip()]
    if not origins:
        return
    if "*" in origins:
        raise ValueError("AURORA_CORS_ORIGINS must contain explicit origins, not '*'")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "If-Match", "X-Requested-With", "Idempotency-Key"],
        expose_headers=["ETag", "Example-ETag", "Content-Disposition", "Retry-After"],
    )
