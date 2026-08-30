"""Storage backends: S3 for production, local filesystem for development.

Both expose the same three operations, so the ingest command never branches on
which one it has. `STORAGE_URI=s3://bucket` picks S3; anything else is a path.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Protocol


class Storage(Protocol):
    def put(self, key: str, body: bytes) -> None: ...
    def exists(self, key: str) -> bool: ...
    def uri(self, key: str) -> str: ...
    def head_bytes(self, key: str, size: int) -> bytes: ...
    def size(self, key: str) -> int: ...


class LocalStorage:
    """Writes under a directory. Same key layout as S3, so the two are swappable."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()

    def put(self, key: str, body: bytes) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temp file and rename, so an interrupted run never leaves a
        # half-written document that `exists()` would later skip.
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(body)
        tmp.replace(path)

    def exists(self, key: str) -> bool:
        return (self.root / key).is_file()

    def head_bytes(self, key: str, size: int) -> bytes:
        """First `size` bytes of a stored object, for re-reading SGML headers."""
        try:
            with open(self.root / key, "rb") as fh:
                return fh.read(size)
        except OSError:
            return b""

    def size(self, key: str) -> int:
        try:
            return (self.root / key).stat().st_size
        except OSError:
            return -1

    def uri(self, key: str) -> str:
        return str(self.root / key)


class S3Storage:
    def __init__(self, bucket: str, prefix: str = "") -> None:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "boto3 is required for S3 storage. Install it with:\n"
                "  pip install -e .\n"
                "Or set STORAGE_URI to a local path to run without AWS."
            ) from exc

        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._local = threading.local()
        self._session = boto3.session.Session()

    @property
    def _client(self):
        # botocore clients are not thread-safe; give each worker thread its own.
        client = getattr(self._local, "client", None)
        if client is None:
            client = self._session.client("s3")
            self._local.client = client
        return client

    def _full(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def put(self, key: str, body: bytes) -> None:
        self._client.put_object(Bucket=self.bucket, Key=self._full(key), Body=body)

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self.bucket, Key=self._full(key))
        except ClientError as exc:
            if exc.response["Error"]["Code"] in ("404", "NoSuchKey", "403"):
                return False
            raise
        return True

    def head_bytes(self, key: str, size: int) -> bytes:
        """First `size` bytes, fetched with a Range request rather than a full GET."""
        from botocore.exceptions import ClientError

        try:
            resp = self._client.get_object(
                Bucket=self.bucket, Key=self._full(key), Range=f"bytes=0-{size - 1}"
            )
            return resp["Body"].read()
        except ClientError:
            return b""

    def size(self, key: str) -> int:
        from botocore.exceptions import ClientError

        try:
            resp = self._client.head_object(Bucket=self.bucket, Key=self._full(key))
        except ClientError:
            return -1
        return int(resp["ContentLength"])

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{self._full(key)}"


def open_storage(uri: str) -> Storage:
    """Build a backend from a URI. `s3://bucket/optional/prefix` or a local path."""
    if uri.startswith("s3://"):
        rest = uri[len("s3://"):].strip("/")
        bucket, _, prefix = rest.partition("/")
        if not bucket:
            raise ValueError(f"malformed S3 URI: {uri!r}")
        return S3Storage(bucket, prefix)
    return LocalStorage(uri)
