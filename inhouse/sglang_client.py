"""SGLang HTTP client.

The only part of extraction that needs a GPU. Everything else in the pipeline
runs against the Client protocol, so this file is the seam where the real
server plugs in.

Launch the server with:

    python -m sglang.launch_server \\
      --model-path Qwen/Qwen2.5-7B-Instruct-AWQ \\
      --host 0.0.0.0 --port 30000 \\
      --mem-fraction-static 0.85
"""

from __future__ import annotations

import json
import logging
import time

import requests

log = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:30000"


class SGLangClient:
    """Posts to SGLang's /generate with a JSON schema constraint.

    Passing `json_schema` is what makes malformed output impossible rather than
    unlikely -- the grammar is applied during decoding, so the model cannot emit
    a token that would break the schema. This is the project's central claim and
    it lives in one parameter.
    """

    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        max_new_tokens: int = 700,
        temperature: float = 0.0,
        timeout: float = 180.0,
        retries: int = 2,
    ) -> None:
        self.url = url.rstrip("/")
        self.max_new_tokens = max_new_tokens
        self.temperature = temperature
        self.timeout = timeout
        self.retries = retries
        self._session = requests.Session()

    def generate(self, prompt: str, schema: dict) -> str:
        payload = {
            "text": prompt,
            "sampling_params": {
                # Greedy: extraction wants reproducibility, not variety. Two
                # runs over the same corpus should differ only where the schema
                # or prompt changed.
                "temperature": self.temperature,
                "max_new_tokens": self.max_new_tokens,
                "json_schema": json.dumps(schema),
            },
        }

        last: Exception | None = None
        for attempt in range(1, self.retries + 2):
            try:
                resp = self._session.post(
                    f"{self.url}/generate", json=payload, timeout=self.timeout
                )
                resp.raise_for_status()
                return resp.json()["text"]
            except (requests.RequestException, KeyError, ValueError) as exc:
                last = exc
                if attempt <= self.retries:
                    backoff = 2.0 ** (attempt - 1)
                    log.warning(
                        "SGLang request failed (%s), attempt %d, retrying in %.0fs",
                        exc, attempt, backoff,
                    )
                    time.sleep(backoff)

        raise RuntimeError(f"SGLang request failed after {self.retries + 1} attempts: {last}")

    # -- diagnostics --------------------------------------------------------

    def health(self) -> bool:
        try:
            return self._session.get(f"{self.url}/health", timeout=5).ok
        except requests.RequestException:
            return False

    def model_path(self) -> str:
        """Model identifier, recorded on every extraction row."""
        try:
            info = self._session.get(f"{self.url}/get_model_info", timeout=5).json()
            return info.get("model_path", "unknown")
        except (requests.RequestException, ValueError):
            return "unknown"

    def cache_hit_rate(self) -> float | None:
        """Prefix cache hit rate from /metrics.

        Read it rather than inferring it -- day 4's whole argument is that the
        shared prefix is cached, and this is the number that shows whether it
        actually is.
        """
        try:
            body = self._session.get(f"{self.url}/metrics", timeout=5).text
        except requests.RequestException:
            return None

        for line in body.splitlines():
            if line.startswith("#"):
                continue
            if "cache_hit_rate" in line:
                try:
                    return float(line.rsplit(maxsplit=1)[-1])
                except ValueError:
                    continue
        return None
