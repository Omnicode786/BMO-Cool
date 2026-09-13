#!/usr/bin/env python3
"""Offline repository lint used when the build host cannot install Ruff."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SEARCH_DIRS = (ROOT / "src", ROOT / "tests", ROOT / "scripts")
FORBIDDEN_MARKERS = ("TO" + "DO: implement", "FIX" + "ME: implement")
STANDALONE_PASS = re.compile(r"^\s*pass\s*(?:#.*)?$")


def main() -> int:
    errors: list[str] = []
    checked = 0
    for base in SEARCH_DIRS:
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            checked += 1
            text = path.read_text(encoding="utf-8")
            rel = path.relative_to(ROOT)
            try:
                tree = ast.parse(text, filename=str(rel))
            except SyntaxError as exc:
                errors.append(f"{rel}:{exc.lineno}: syntax error: {exc.msg}")
                continue
            if path.parts[path.parts.index(base.name) + 1 :] and ast.get_docstring(tree) is None:
                errors.append(f"{rel}: missing module docstring")
            for number, line in enumerate(text.splitlines(), start=1):
                if line.rstrip() != line:
                    errors.append(f"{rel}:{number}: trailing whitespace")
                if "\t" in line:
                    errors.append(f"{rel}:{number}: tab character")
                if STANDALONE_PASS.match(line):
                    errors.append(f"{rel}:{number}: standalone pass is not allowed")
                for marker in FORBIDDEN_MARKERS:
                    if marker in line:
                        errors.append(f"{rel}:{number}: forbidden placeholder marker {marker!r}")
    if errors:
        print("Offline lint FAILED")
        for error in errors:
            print(f"  {error}")
        return 1
    print(f"Offline lint passed: {checked} Python files parsed; no placeholders/trailing whitespace/tabs found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
