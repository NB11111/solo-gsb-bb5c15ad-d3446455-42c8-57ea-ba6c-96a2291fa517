"""Application configuration, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_MAX_REQUEST_SIZE_BYTES = 10 * 1024 * 1024  # 10 MiB


def _parse_size(value: str) -> int:
    """Parse a byte size such as ``10485760``, ``10MB`` or ``1m``.

    Supported suffixes: B (default), K, M, G (binary, 1024-based).
    ``0`` means "no limit".
    """
    text = value.strip()
    if not text:
        raise ValueError("request size limit must not be empty")
    multiplier = 1
    suffix = text[-1].upper()
    suffixes = {"K": 1024, "M": 1024**2, "G": 1024**3}
    if suffix in suffixes:
        multiplier = suffixes[suffix]
        text = text[:-1]
    elif suffix == "B":
        text = text[:-1]
    number = int(text)  # raises ValueError with a useful message
    if number < 0:
        raise ValueError("request size limit must be >= 0")
    return number * multiplier


@dataclass(frozen=True)
class Settings:
    max_request_size_bytes: int = DEFAULT_MAX_REQUEST_SIZE_BYTES
    host: str = "0.0.0.0"
    port: int = 8000

    @classmethod
    def from_env(cls) -> "Settings":
        raw = os.environ.get(
            "MAX_REQUEST_SIZE_BYTES", str(DEFAULT_MAX_REQUEST_SIZE_BYTES)
        )
        try:
            max_size = _parse_size(raw)
        except ValueError as exc:
            raise RuntimeError(
                f"invalid MAX_REQUEST_SIZE_BYTES={raw!r}: {exc}"
            ) from exc
        port = int(os.environ.get("PORT", "8000"))
        return cls(
            max_request_size_bytes=max_size,
            host=os.environ.get("HOST", "0.0.0.0"),
            port=port,
        )


settings = Settings.from_env()
