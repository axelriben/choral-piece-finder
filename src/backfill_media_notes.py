"""
One-off script to backfill the notes field for CPDL media_files rows.

Extracts the filename from the URL and applies heuristic regex patterns
to detect edition type (transposition, translation, voicing variant, parts).
Updates each row's notes field in place.

Usage:
    python src/backfill_media_notes.py [--db PATH]
"""

import argparse
import re
import sqlite3
from pathlib import Path

DB_DEFAULT = Path(__file__).parent.parent / "data" / "index.db"

# ---------------------------------------------------------------------------
# Pattern definitions
# ---------------------------------------------------------------------------

# Each pattern is (label, compiled_regex). Rules are applied in order;
# all matching labels are joined with '; '.

_PATTERNS: list[tuple[str, re.Pattern]] = [
    # Transpositions — match before voicing variants to avoid false positives
    ("Transposed edition",
     re.compile(
         r"down[_\s-]a[_\s-]|up[_\s-]a[_\s-]"
         r"|transpos(?:ed?|ition)"
         r"|[_\s-]tr\b"          # ' tr' or '_tr' at word boundary
         r"|[_\s-]tr\."          # '_tr.' before extension
         r"|in[_\s][ABCDEFG](?:[_\s](?:flat|sharp|minor|major))?"
         r"|in_g\b|in_a\b|in_b_flat|in_d\b|in_e_flat",
         re.IGNORECASE,
     )),

    # Spanish / other translations
    ("Spanish translation",
     re.compile(r"espa[nñ]ol|spanish", re.IGNORECASE)),
    ("English translation",
     re.compile(r"english|[_\s-]en[_\s-]", re.IGNORECASE)),
    ("German translation",
     re.compile(r"[_\s-]germ|deutsch|[_\s-]de[_\s-]", re.IGNORECASE)),
    ("Italian translation",
     re.compile(r"italian|[_\s-]it[_\s-]", re.IGNORECASE)),
    ("French translation",
     re.compile(r"fran[cç]|french", re.IGNORECASE)),

    # Parts-only editions
    ("Parts only",
     re.compile(r"[_\s-]parts?\b|partbook|partes\b|partbuch", re.IGNORECASE)),

    # Accompaniment / reduction
    ("With accompaniment/reduction",
     re.compile(r"[_\s]acc\b|accompaniment|reduction", re.IGNORECASE)),

    # Voicing variants — uppercase voicing tokens that differ from SATB.
    # Use letter-boundary lookbehind/lookahead because _ is a word char
    # so \b doesn't split '_TTTB' correctly.
    ("TTTB voicing variant",    re.compile(r"(?<![A-Za-z])TTTB(?![A-Za-z])",    re.IGNORECASE)),
    ("STTTB voicing variant",   re.compile(r"(?<![A-Za-z])STTTB(?![A-Za-z])",   re.IGNORECASE)),
    ("TTBB voicing variant",    re.compile(r"(?<![A-Za-z])TTBB(?![A-Za-z])",    re.IGNORECASE)),
    ("SSAA voicing variant",    re.compile(r"(?<![A-Za-z])SSAA(?![A-Za-z])",    re.IGNORECASE)),
    ("ATTB voicing variant",    re.compile(r"(?<![A-Za-z])ATTB(?![A-Za-z])",    re.IGNORECASE)),
    ("SSAATB voicing variant",  re.compile(r"(?<![A-Za-z])SSAATB(?![A-Za-z])",  re.IGNORECASE)),
    ("SSATTB voicing variant",  re.compile(r"(?<![A-Za-z])SSATTB(?![A-Za-z])",  re.IGNORECASE)),
    ("SSAATTB voicing variant", re.compile(r"(?<![A-Za-z])SSAATTB(?![A-Za-z])", re.IGNORECASE)),
]


def _extract_filename(url: str) -> str:
    """Return the filename portion of a CPDL FilePath URL."""
    marker = "FilePath/"
    idx = url.find(marker)
    if idx != -1:
        return url[idx + len(marker):]
    # Fallback: last path segment
    return url.rsplit("/", 1)[-1]


def _build_notes(filename: str) -> str:
    """Apply all heuristic patterns to filename; return joined label string."""
    labels = [label for label, pattern in _PATTERNS if pattern.search(filename)]
    return "; ".join(labels)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill CPDL media_files notes")
    parser.add_argument("--db", default=str(DB_DEFAULT), help="Path to index.db")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT media_id, url FROM media_files"
        " WHERE source = 'cpdl' AND (notes IS NULL OR notes = '')"
    ).fetchall()

    print(f"Rows to process: {len(rows)}")

    updated = 0
    samples: list[tuple[str, str]] = []

    for row in rows:
        filename = _extract_filename(row["url"])
        notes = _build_notes(filename)
        if notes:
            conn.execute(
                "UPDATE media_files SET notes = ? WHERE media_id = ?",
                (notes, row["media_id"]),
            )
            updated += 1
            if len(samples) < 10:
                samples.append((filename, notes))

    conn.commit()
    conn.close()

    print(f"Rows updated with notes: {updated}")
    print(f"Rows left with no notes: {len(rows) - updated}")
    print()
    if samples:
        print("Sample (filename → notes):")
        for fn, note in samples:
            print(f"  {fn}")
            print(f"    → {note}")


if __name__ == "__main__":
    main()
