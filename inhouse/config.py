"""Configuration, read from the environment.

Everything the ingest command needs is here. See .env.example.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# SEC asks for a descriptive User-Agent with real contact details, and blocks
# traffic without one. See https://www.sec.gov/os/accessing-edgar-data
DEFAULT_USER_AGENT = ""

# SEC's published ceiling is 10 requests/second. We stay under it rather than
# ride it -- a burst that trips their limiter costs a ten-minute block, which is
# far more expensive than the second we save.
DEFAULT_RATE_LIMIT = 8.0

FORM_TYPES = ("8-K", "4")


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so `make ingest` works without extra dependencies."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        # Real environment variables win over the file.
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Config:
    user_agent: str
    storage_uri: str
    rate_limit: float
    max_concurrency: int
    form_types: tuple[str, ...]

    @classmethod
    def from_env(cls, dotenv: Path | None = None) -> "Config":
        _load_dotenv(dotenv or Path(".env"))

        user_agent = os.environ.get("SEC_USER_AGENT", DEFAULT_USER_AGENT).strip()
        if not user_agent:
            raise ConfigError(
                "SEC_USER_AGENT is not set.\n"
                "The SEC requires a User-Agent with real contact details and will\n"
                "block requests without one. Set it in .env, for example:\n"
                '  SEC_USER_AGENT="Jane Doe jane@example.com"'
            )
        if "@" not in user_agent:
            raise ConfigError(
                f"SEC_USER_AGENT={user_agent!r} has no contact email. "
                "The SEC expects something like 'Jane Doe jane@example.com'."
            )

        bucket = os.environ.get("S3_BUCKET", "").strip()
        storage_uri = os.environ.get("STORAGE_URI", "").strip()
        if not storage_uri:
            storage_uri = f"s3://{bucket}" if bucket else "./data"

        return cls(
            user_agent=user_agent,
            storage_uri=storage_uri,
            rate_limit=float(os.environ.get("SEC_RATE_LIMIT", DEFAULT_RATE_LIMIT)),
            max_concurrency=int(os.environ.get("SEC_MAX_CONCURRENCY", "6")),
            form_types=FORM_TYPES,
        )


class ConfigError(Exception):
    """Raised when required configuration is missing or malformed."""
