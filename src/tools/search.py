"""
search_local_index — primary retrieval tool for the choral-piece-finder agent.

Translates structured filters into a SQL query against data/index.db and
returns summary records suitable for presenting to the user or passing to
get_work_details for follow-up.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import get_connection

TOOL_SPEC = {
    "name": "search_local_index",
    "description": (
        "Searches the unified index of choral works. Use this as the primary retrieval tool. "
        "Returns up to 25 matching works with summary information. "
        "Multiple filters are combined with AND. "
        "Free-text query searches title, incipit, and description."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "Free-text query searched across title, incipit, and description. Optional."
                ),
            },
            "composer": {
                "type": "string",
                "description": (
                    "Composer surname or full normalized name. Substring match, "
                    "case-insensitive. Optional."
                ),
            },
            "voicing": {
                "type": "string",
                "description": (
                    "Voicing string like 'SATB', 'SSAATTBB', 'SATTB'. "
                    "Substring match against any of a work's voicings. Optional."
                ),
            },
            "num_voices": {
                "type": "integer",
                "description": (
                    "Number of distinct voice parts. Matches works whose primary "
                    "voicing has this voice count. Optional."
                ),
            },
            "language": {
                "type": "string",
                "description": "Text language ISO code or name. Substring match. Optional.",
            },
            "period": {
                "type": "string",
                "enum": ["Renaissance", "Late Romantic"],
                "description": "Compositional period. Optional.",
            },
            "genre_main": {
                "type": "string",
                "enum": ["Sacred", "Secular"],
                "description": "Main genre. Optional.",
            },
            "duration_max_sec": {
                "type": "integer",
                "description": (
                    "Upper bound on duration in seconds; matches works whose "
                    "duration_min_sec is at most this value. Optional."
                ),
            },
            "duration_min_sec": {
                "type": "integer",
                "description": "Lower bound on duration in seconds. Optional.",
            },
            "has_free_score": {
                "type": "boolean",
                "description": (
                    "Restrict to works with at least one downloadable score. Optional."
                ),
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results. Default 25, max 100.",
                "default": 25,
            },
        },
    },
}


def search_local_index(
    query: str | None = None,
    composer: str | None = None,
    voicing: str | None = None,
    num_voices: int | None = None,
    language: str | None = None,
    period: str | None = None,
    genre_main: str | None = None,
    duration_max_sec: int | None = None,
    duration_min_sec: int | None = None,
    has_free_score: bool | None = None,
    limit: int = 25,
) -> list[dict]:
    """Search the unified index with optional structured filters.

    All filters are optional and combined with AND.  Returns a list of summary
    dicts enriched with voicings and sources lists.
    """
    limit = min(int(limit), 100)

    clauses: list[str] = []
    params: list = []

    if query:
        words = query.split()
        for word in words:
            pattern = f"%{word}%"
            clauses.append(
                "(LOWER(w.title_primary) LIKE LOWER(?) "
                "OR LOWER(COALESCE(w.incipit,'')) LIKE LOWER(?) "
                "OR LOWER(COALESCE(w.description,'')) LIKE LOWER(?))"
            )
            params.extend([pattern, pattern, pattern])

    if composer:
        clauses.append("LOWER(w.composer_norm) LIKE LOWER(?)")
        params.append(f"%{composer}%")

    if voicing:
        clauses.append(
            "EXISTS ("
            "  SELECT 1 FROM voicings v"
            "  WHERE v.work_id = w.work_id"
            "  AND LOWER(v.voicing_string) LIKE LOWER(?)"
            ")"
        )
        params.append(f"%{voicing}%")

    if num_voices is not None:
        clauses.append(
            "EXISTS ("
            "  SELECT 1 FROM voicings v"
            "  WHERE v.work_id = w.work_id"
            "  AND v.is_primary = 1"
            "  AND v.num_voices = ?"
            ")"
        )
        params.append(num_voices)

    if language:
        clauses.append("LOWER(COALESCE(w.text_language,'')) LIKE LOWER(?)")
        params.append(f"%{language}%")

    if period:
        clauses.append("w.period = ?")
        params.append(period)

    if genre_main:
        clauses.append("w.genre_main = ?")
        params.append(genre_main)

    if duration_max_sec is not None:
        # Works whose minimum duration is within the budget
        clauses.append("w.duration_min_sec IS NOT NULL AND w.duration_min_sec <= ?")
        params.append(duration_max_sec)

    if duration_min_sec is not None:
        clauses.append("w.duration_max_sec IS NOT NULL AND w.duration_max_sec >= ?")
        params.append(duration_min_sec)

    if has_free_score is not None:
        clauses.append("w.has_free_score = ?")
        params.append(1 if has_free_score else 0)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    sql = f"""
        SELECT
            w.work_id, w.title_primary, w.composer_norm,
            w.period, w.genre_main, w.genre_sub,
            w.text_language, w.year_composition,
            w.duration_min_sec, w.duration_max_sec,
            w.has_free_score
        FROM works w
        {where}
        ORDER BY w.has_free_score DESC, w.composer_norm, w.title_primary
        LIMIT ?
    """
    params.append(limit)

    conn = get_connection()
    work_rows = conn.execute(sql, params).fetchall()

    if not work_rows:
        return []

    work_ids = [r["work_id"] for r in work_rows]
    placeholders = ",".join("?" * len(work_ids))

    voicing_rows = conn.execute(
        f"SELECT work_id, voicing_string FROM voicings"
        f" WHERE work_id IN ({placeholders}) ORDER BY is_primary DESC, voicing_id",
        work_ids,
    ).fetchall()
    voicings_by_work: dict[str, list[str]] = {}
    for vr in voicing_rows:
        voicings_by_work.setdefault(vr["work_id"], []).append(vr["voicing_string"])

    source_rows = conn.execute(
        f"SELECT work_id, source_name, source_url"
        f" FROM sources WHERE work_id IN ({placeholders})",
        work_ids,
    ).fetchall()
    sources_by_work: dict[str, list[dict]] = {}
    for sr in source_rows:
        sources_by_work.setdefault(sr["work_id"], []).append(
            {"source_name": sr["source_name"], "source_url": sr["source_url"]}
        )

    results = []
    for row in work_rows:
        wid = row["work_id"]
        results.append({
            "work_id":          wid,
            "title_primary":    row["title_primary"],
            "composer_norm":    row["composer_norm"],
            "period":           row["period"],
            "genre_main":       row["genre_main"],
            "genre_sub":        row["genre_sub"],
            "text_language":    row["text_language"],
            "year_composition": row["year_composition"],
            "duration_min_sec": row["duration_min_sec"],
            "duration_max_sec": row["duration_max_sec"],
            "has_free_score":   bool(row["has_free_score"]),
            "voicings":         voicings_by_work.get(wid, []),
            "sources":          sources_by_work.get(wid, []),
        })

    return results
