from __future__ import annotations
import hashlib
from pathlib import Path


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def verify(path: Path, expected_sha256: str) -> bool:
    if not path.exists():
        return False
    return sha256_file(path).lower() == expected_sha256.lower()
