"""Version review caches with immutable Git inputs and analysis implementation."""
from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def analysis_version() -> str:
    """Fingerprint prompts, models, rules and pinned dependencies (never secrets)."""
    root = Path(__file__).resolve().parents[1]
    paths = sorted((root / "agents").glob("*.py")) + [
        root / "tools" / name
        for name in ("ast_parser.py", "semgrep_runner.py", "github_client.py", "review_version.py")
    ] + [root / "requirements.txt"]
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()


def review_cache_key(base_sha: str, head_sha: str, merge_base_sha: str, rules_version: str) -> str:
    """Invalidate when the compared revisions or analysis configuration changes."""
    value = ":".join((base_sha, head_sha, merge_base_sha, analysis_version(), rules_version))
    return hashlib.sha256(value.encode()).hexdigest()
