"""Per-module coverage floor gate (Phase 5 §Q). Stdlib only.

Reads ``coverage.json`` (``coverage json`` / ``pytest --cov-report=json``) and
``coverage_floors.json``: a list of ``{"glob": "<path glob>", "floor": <0-100>}``
rules. Matching is ``fnmatch`` over the POSIX form of every coverage path, so
``**`` works exactly like coverage's own ``--include`` patterns.

A file may match several rules; the **highest** floor wins. Every glob must
match at least one file (a typo would otherwise silently pass), and any file
under the enforced source roots that matches no rule still has to clear the
``default_floor``. Exits non-zero and prints one line per violation.

Usage:
    python scripts/check_coverage_floors.py coverage.json coverage_floors.json
"""

from __future__ import annotations

import fnmatch
import json
import posixpath
import sys
from pathlib import Path
from typing import Any

DEFAULT_FLOOR_KEY = "default_floor"
TOTAL_FLOOR_KEY = "total_floor"


def _load(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise SystemExit(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path}: invalid JSON: {exc}") from exc


def _norm(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/")).lstrip("./")


def _pct(entry: dict[str, Any]) -> float:
    summary = entry.get("summary") or {}
    value = summary.get("percent_covered")
    if value is None:
        # Fake/foreign coverage JSON may only carry counts.
        total = int(summary.get("num_statements") or 0)
        covered = int(summary.get("covered_lines") or 0)
        return 100.0 if total == 0 else 100.0 * covered / total
    return float(value)


def check(coverage: dict[str, Any], floors: dict[str, Any]) -> list[str]:
    rules = list(floors.get("floors") or [])
    default_floor = float(floors.get(DEFAULT_FLOOR_KEY, 0.0))
    total_floor = floors.get(TOTAL_FLOOR_KEY)
    files: dict[str, dict[str, Any]] = coverage.get("files") or {}
    problems: list[str] = []
    matched: dict[str, int] = {str(rule["glob"]): 0 for rule in rules}

    for raw_path, entry in sorted(files.items()):
        path = _norm(raw_path)
        percent = _pct(entry)
        applicable = [
            float(rule["floor"])
            for rule in rules
            if fnmatch.fnmatch(path, _norm(str(rule["glob"])))
        ]
        for rule in rules:
            if fnmatch.fnmatch(path, _norm(str(rule["glob"]))):
                matched[str(rule["glob"])] += 1
        floor = max(applicable) if applicable else default_floor
        if percent + 1e-9 < floor:
            problems.append(
                f"{path}: {percent:.2f}% < floor {floor:.2f}%"
            )

    for glob, count in matched.items():
        if count == 0:
            problems.append(f"floor rule {glob!r} matched no file (typo or stale rule)")

    if total_floor is not None:
        totals = coverage.get("totals") or {}
        total = totals.get("percent_covered")
        if total is None:
            total = _pct({"summary": totals})
        if float(total) + 1e-9 < float(total_floor):
            problems.append(
                f"TOTAL: {float(total):.2f}% < floor {float(total_floor):.2f}%"
            )
    return problems


def main(argv: list[str]) -> int:
    cov_path = Path(argv[1] if len(argv) > 1 else "coverage.json")
    floors_path = Path(argv[2] if len(argv) > 2 else "coverage_floors.json")
    problems = check(_load(cov_path), _load(floors_path))
    if problems:
        print("coverage floors: FAIL")
        for line in problems:
            print(f"  - {line}")
        return 1
    print("coverage floors: PASS")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main(sys.argv))
