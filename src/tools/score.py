"""
fetch_score — score-retrieval tool for the choral-piece-finder agent.

Returns URLs of available free scores for a work, or publisher guidance
when no downloadable file exists.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import get_connection

TOOL_SPEC = {
    "name": "fetch_score",
    "description": (
        "Returns score URLs for a work if freely available, or publisher "
        "information if not. Use to answer 'how do I get this score' questions."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "work_id": {
                "type": "string",
                "description": "Internal work_id.",
            },
            "preferred_format": {
                "type": "string",
                "enum": ["pdf", "mxl", "musicxml", "midi", "any"],
                "description": (
                    "Preferred format. 'any' returns all formats. "
                    "'mxl' and 'musicxml' are treated as synonymous."
                ),
                "default": "any",
            },
        },
        "required": ["work_id"],
    },
}

# Patterns that suggest a work is commercially published (in copyright or
# requires purchase), used to build guidance when no free score exists.
_PUBLISHER_PATTERNS = [
    r"\bGehrmans?\b",
    r"\bEdition\b",
    r"\bPublisher\b",
    r"\bVerlag\b",
    r"\bSchott\b",
    r"\bBreitkopf\b",
    r"\bPeters\b",
    r"\bRicordi\b",
    r"\bHansen\b",
    r"\bStim\b",
    r"\bed\.\b",
    r"\bpublished by\b",
    r"\bpublisher\b",
    r"\bpublication\b",
]
_PUBLISHER_RE = re.compile("|".join(_PUBLISHER_PATTERNS), re.IGNORECASE)

# Canonical format names; maps user-supplied aliases to DB values
_FORMAT_ALIASES: dict[str, str] = {
    "mxl":      "MusicXML",
    "musicxml": "MusicXML",
    "pdf":      "PDF",
    "midi":     "MIDI",
}


def fetch_score(work_id: str, preferred_format: str = "any") -> dict:
    """Return available score files for *work_id*, or guidance if none exist.

    When *preferred_format* is not 'any', only files matching that format
    are returned, but orphan-probe discoveries are always surfaced in a
    separate note field regardless of format filter.

    Return shape when files are available:
        {"available": True, "files": [...], "note": "..."}
    Return shape when no files:
        {"available": False, "guidance": "..."}
    Returns {"error": ...} if the work_id doesn't exist.
    """
    conn = get_connection()

    work_row = conn.execute(
        "SELECT work_id, title_primary, composer_norm, description, has_free_score"
        " FROM works WHERE work_id = ?",
        (work_id,),
    ).fetchone()

    if work_row is None:
        return {"error": "work_id not found", "work_id": work_id}

    media_rows = conn.execute(
        "SELECT format, url, source, source_media_id, discovered_via, notes"
        " FROM media_files WHERE work_id = ?"
        " ORDER BY discovered_via, format, media_id",
        (work_id,),
    ).fetchall()

    if not media_rows:
        return {
            "available": False,
            "guidance": _build_guidance(work_row),
        }

    # Normalise the preferred_format filter
    fmt_filter: str | None = None
    if preferred_format and preferred_format.lower() != "any":
        fmt_filter = _FORMAT_ALIASES.get(preferred_format.lower(), preferred_format.upper())

    all_files = [dict(r) for r in media_rows]

    if fmt_filter:
        filtered = [f for f in all_files if f["format"].upper() == fmt_filter.upper()]
    else:
        filtered = all_files

    orphan_count = sum(1 for f in all_files if f["discovered_via"] == "orphan_probe")
    work_page_count = sum(1 for f in all_files if f["discovered_via"] == "work_page")

    note_parts: list[str] = []
    if fmt_filter and not filtered:
        note_parts.append(
            f"No {fmt_filter} files found; {len(all_files)} file(s) available in other formats."
        )
        filtered = all_files  # fall back to returning everything
    if orphan_count:
        note_parts.append(
            f"{orphan_count} additional edition(s) discovered beyond the source's "
            "catalog navigation via media-ID probe."
        )
    if work_page_count and orphan_count:
        note_parts.append(
            f"{work_page_count} edition(s) linked directly from the work page."
        )

    return {
        "available": True,
        "files": filtered,
        "note": "  ".join(note_parts) if note_parts else None,
    }


def _build_guidance(work_row) -> str:
    """Build a helpful guidance string for a work with no downloadable files."""
    title = work_row["title_primary"]
    composer = work_row["composer_norm"]
    description = work_row["description"] or ""

    publisher_hints: list[str] = []
    for m in _PUBLISHER_RE.finditer(description):
        # Grab a short window around the match for context
        start = max(0, m.start() - 30)
        end = min(len(description), m.end() + 60)
        snippet = description[start:end].replace("\n", " ").strip()
        publisher_hints.append(snippet)

    source_rows = get_connection().execute(
        "SELECT source_name, source_url FROM sources WHERE work_id = ?",
        (work_row["work_id"],),
    ).fetchall()
    source_urls = [r["source_url"] for r in source_rows if r["source_url"]]

    parts = [f'No free score is currently available for “{title}” by {composer}.']

    if publisher_hints:
        parts.append("Publisher/edition information from the record: " + "; ".join(publisher_hints[:3]) + ".")

    if source_urls:
        parts.append("Source page(s) for further information: " + ", ".join(source_urls) + ".")
    else:
        parts.append("No source URLs are recorded; the piece may require library access.")

    return "  ".join(parts)
