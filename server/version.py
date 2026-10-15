"""Which code and settings a running service has.

The service reads its Python code and .env once, when it starts. ``fingerprint`` hashes
the Python files of the project as they are on disk; the service records it at start and
reports it at /api/health, so `tradingagents server selfcheck` can tell a service still
running yesterday's code (or yesterday's .env) from one that has been restarted. The web
page files are left out: they are read from disk on every request.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
PACKAGES = ("cli", "server", "tradingagents")

# Settings worth comparing between the service and a fresh read of .env (no secrets).
SETTINGS = ("llm_provider", "quick_think_llm", "deep_think_llm", "output_language", "vote_runs",
            "max_debate_rounds", "max_risk_discuss_rounds", "analyst_report_reuse_hours")


def fingerprint(root: Path = PROJECT_DIR) -> str:
    digest = hashlib.sha256()
    for package in PACKAGES:
        for path in sorted((root / package).rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            # Line endings do not change the code: a CRLF and an LF copy hash alike.
            digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
    return digest.hexdigest()[:16]


def settings(config: dict) -> dict:
    return {key: config.get(key) for key in SETTINGS}
