"""CIK -> SIC lookup, backed by a persistent on-disk cache.

The sector filter the whole dashboard hangs off is the SIC code, and it lives on
a separate endpoint (data.sec.gov/submissions) from the filings themselves. It
is fetched now, while we are already making requests, rather than later.

Two levels of deduplication matter here, because every request costs rate limit:
  - within a day, many filings share a CIK (one company, several Form 4s)
  - across days, the same companies file repeatedly

So lookups are deduplicated per run and cached to disk between runs. A company's
SIC code effectively never changes, which is what makes the disk cache sound.
"""

from __future__ import annotations

import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable

from .edgar import EdgarClient, EdgarError

log = logging.getLogger(__name__)

DEFAULT_CACHE = Path.home() / ".cache" / "inhouse" / "sic.json"


class SicLookup:
    """Resolves CIKs to (sic, sic_description, company_name)."""

    def __init__(
        self,
        client: EdgarClient,
        cache_path: Path | None = None,
        max_workers: int = 6,
    ) -> None:
        self._client = client
        self._cache_path = Path(cache_path) if cache_path else DEFAULT_CACHE
        self._max_workers = max_workers
        self._lock = threading.Lock()
        self._cache: dict[str, dict] = self._load()
        self._dirty = False

    def _load(self) -> dict[str, dict]:
        try:
            data = json.loads(self._cache_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self) -> None:
        if not self._dirty:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cache_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._cache, indent=0, sort_keys=True))
            tmp.replace(self._cache_path)
            self._dirty = False
        except OSError as exc:
            # A cache we cannot persist is a slower next run, not a failed one.
            log.warning("could not write SIC cache to %s: %s", self._cache_path, exc)

    def warm(self, ciks: Iterable[str]) -> None:
        """Resolve every CIK not already cached, in parallel within the rate limit."""
        missing = sorted({c.zfill(10) for c in ciks} - self._cache.keys())
        if not missing:
            return

        log.info("resolving SIC for %d new CIK(s) (%d cached)", len(missing), len(self._cache))
        with ThreadPoolExecutor(max_workers=self._max_workers) as pool:
            list(pool.map(self._fetch, missing))
        self.save()

    def _fetch(self, cik: str) -> None:
        try:
            payload = json.loads(self._client.submissions(cik).text)
        except (EdgarError, json.JSONDecodeError) as exc:
            # A missing SIC is a null column, not a failed ingest. Individual
            # filers -- the people behind Form 4s -- have no SIC at all, which is
            # expected rather than exceptional.
            log.debug("no submissions data for CIK %s: %s", cik, exc)
            entry = {"sic": None, "sic_description": None, "company": None}
        else:
            sic = (payload.get("sic") or "").strip() or None
            entry = {
                "sic": sic,
                "sic_description": (payload.get("sicDescription") or "").strip() or None,
                "company": (payload.get("name") or "").strip() or None,
            }

        with self._lock:
            self._cache[cik] = entry
            self._dirty = True

    def get(self, cik: str) -> dict:
        return self._cache.get(cik.zfill(10), {"sic": None, "sic_description": None, "company": None})

    def sic(self, cik: str) -> str | None:
        return self.get(cik)["sic"]

    def __len__(self) -> int:
        return len(self._cache)
