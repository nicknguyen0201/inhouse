"""EDGAR HTTP client: rate limiting, retries, and the endpoints day 1 needs.

Three things are fetched from EDGAR:
  - the daily form index, which lists every filing for a date
  - each filing document, stored raw and unmodified
  - the submissions JSON per CIK, which is where SIC codes come from
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date

import requests

log = logging.getLogger(__name__)

ARCHIVES = "https://www.sec.gov/Archives"
SUBMISSIONS = "https://data.sec.gov/submissions"

# Transient conditions worth retrying. 403 is included because SEC's rate
# limiter returns it rather than 429 when it decides you are being rude.
RETRY_STATUSES = frozenset({403, 429, 500, 502, 503, 504})
MAX_ATTEMPTS = 4


class EdgarError(Exception):
    """A request to EDGAR failed after exhausting retries."""


def document_url(path: str) -> str:
    """Absolute URL for an archive path as it appears in the daily index."""
    return f"{ARCHIVES}/{path.lstrip('/')}"


class RateLimiter:
    """Thread-safe minimum-interval limiter.

    Requests are spaced by at least 1/rate seconds. This is a pacer, not a token
    bucket -- deliberately, since a bucket permits bursts and bursts are what
    SEC blocks on.
    """

    def __init__(self, rate: float) -> None:
        self._interval = 1.0 / rate if rate > 0 else 0.0
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait = self._next_at - now
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()
            self._next_at = now + self._interval


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes

    @property
    def text(self) -> str:
        # EDGAR's older documents are latin-1; the newer ones are utf-8. Decode
        # permissively -- we store raw bytes anyway, so this is only for parsing.
        return self.body.decode("utf-8", errors="replace")


class EdgarClient:
    def __init__(self, user_agent: str, rate_limit: float = 8.0) -> None:
        self._limiter = RateLimiter(rate_limit)
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Encoding": "gzip, deflate",
            }
        )

    def get(
        self,
        url: str,
        *,
        host: str | None = None,
        retry_statuses: frozenset[int] | set[int] = RETRY_STATUSES,
    ) -> Response:
        last_error: str = "unknown"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self._limiter.acquire()
            try:
                headers = {"Host": host} if host else None
                resp = self._session.get(url, headers=headers, timeout=30)
            except requests.RequestException as exc:
                last_error = str(exc)
            else:
                if resp.status_code == 200:
                    return Response(resp.status_code, resp.content)
                if resp.status_code not in retry_statuses:
                    raise EdgarError(f"{url} -> HTTP {resp.status_code}")
                last_error = f"HTTP {resp.status_code}"

            if attempt < MAX_ATTEMPTS:
                backoff = 2.0 ** (attempt - 1)
                log.warning(
                    "%s: %s (attempt %d/%d), retrying in %.0fs",
                    url, last_error, attempt, MAX_ATTEMPTS, backoff,
                )
                time.sleep(backoff)

        raise EdgarError(f"{url} failed after {MAX_ATTEMPTS} attempts: {last_error}")

    # -- endpoints ---------------------------------------------------------

    def daily_index(self, day: date) -> str:
        """Fetch the form-sorted daily index for a date.

        Raises EdgarError with a readable message for weekends and holidays,
        which have no index file at all.
        """
        quarter = (day.month - 1) // 3 + 1
        url = (
            f"{ARCHIVES}/edgar/daily-index/{day.year}/QTR{quarter}"
            f"/form.{day:%Y%m%d}.idx"
        )
        # A date with no index returns 403, not 404 -- the same status the rate
        # limiter uses. Retrying it four times with backoff would be slow and
        # would end in a misleading "rate limited" message, so check whether the
        # date could have an index before making the request at all.
        if day.weekday() >= 5:
            raise EdgarError(
                f"No daily index for {day}: that is a "
                f"{'Saturday' if day.weekday() == 5 else 'Sunday'}. "
                f"EDGAR publishes indexes on business days only."
            )
        try:
            return self.get(url, retry_statuses=RETRY_STATUSES - {403}).text
        except EdgarError as exc:
            if "403" in str(exc) or "404" in str(exc):
                raise EdgarError(
                    f"No daily index for {day} -- market holiday, or a date "
                    f"EDGAR has not published yet. (EDGAR returns 403 for a "
                    f"missing index.)"
                ) from exc
            raise

    def document(self, path: str) -> bytes:
        """Fetch a filing document by its EDGAR archive path, returned verbatim.

        Index paths are relative to /Archives/, not the site root -- serving them
        from the root 404s on every document.
        """
        return self.get(document_url(path)).body

    def submissions(self, cik: str) -> Response:
        """Fetch the submissions JSON for a CIK, which carries the SIC code."""
        return self.get(f"{SUBMISSIONS}/CIK{cik.zfill(10)}.json")
