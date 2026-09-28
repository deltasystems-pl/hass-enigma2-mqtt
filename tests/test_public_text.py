"""What this public repository says must be readable by the public.

Design decisions that bind this code are recorded here, as ADRs in `docs/adr/`. A comment or a
docstring that cites a document a reader of this repository cannot open - by its section numbers
or by the names of its open decisions - explains nothing to that reader, so the reference goes to
the ADR, or the reasoning is written out in plain words.
"""

from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("custom_components", "tests", "tools", "docs", ".github")
SUFFIXES = {".py", ".md", ".json", ".yml", ".yaml", ".sh"}
PRIVATE_CITATION = re.compile(
    r"[Ss]pec(?:ification)? (?:section )?§?ae\b"
    r"|§ ?ae\.?[0-9]"
    r"|\bae\.[0-9]+\b"
    r"|\bdesign section\b"
    r"|\bOD [A-Z0-9]+\b"
    r"|\bthe design's OD\b"
)


def _files() -> list[Path]:
    found = [path for path in ROOT.glob("*.md")]
    for top in SCANNED:
        found.extend(
            path
            for path in (ROOT / top).rglob("*")
            if path.is_file()
            and path.suffix in SUFFIXES
            and "bundled" not in path.parts
            and "__pycache__" not in path.parts
            and path != Path(__file__).resolve()
        )
    return found


def test_nothing_cites_a_design_document_the_reader_cannot_open() -> None:
    cited = [
        f"{path.relative_to(ROOT)}:{number}: {line.strip()}"
        for path in _files()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if PRIVATE_CITATION.search(line)
    ]

    assert cited == []
