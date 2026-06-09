"""
disambiguate_homonyms — work-disambiguation tool for the choral-piece-finder agent.

When a composer + title query matches multiple works, returns each candidate
with incipit, text author, voicing, and parent-work information so the user
(or agent) can distinguish between them.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import get_connection
from utils import normalize_title

TOOL_SPEC = {
    "name": "disambiguate_homonyms",
    "description": (
        "When a composer + title query has multiple plausible matches, returns "
        "each candidate with disambiguating information (incipit, text author, "
        "key, parent work). Use whenever a search yields multiple works with "
        "similar titles by the same composer. "
        "By default, only choral works are returned; set include_non_choral=true "
        "to also surface solo songs and instrumental pieces."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "composer": {
                "type": "string",
                "description": "Composer surname or full normalized name.",
            },
            "title": {
                "type": "string",
                "description": "Title or title fragment. Substring match.",
            },
            "include_non_choral": {
                "type": "boolean",
                "description": (
                    "Set to true ONLY when the user explicitly asks about non-choral works "
                    "(solo songs, instrumental, orchestral, etc.). Omit this parameter by "
                    "default — disambiguation returns choral works only by default."
                ),
            },
        },
        "required": ["composer", "title"],
    },
}


def disambiguate_homonyms(
    composer: str, title: str, include_non_choral: bool = False
) -> dict:
    """Return all works matching *composer* and *title* with disambiguating fields.

    Each candidate includes: work_id, title_primary, title_alternates, incipit
    (up to 80 chars), text_author, key_text, year_composition, primary voicing,
    and parent work title (if the work is part of a cycle).  By default only
    choral works are returned; set include_non_choral=True to include solo songs
    and instrumental pieces.

    Returns {"candidates": [...], "count": N, "note": "..."}.
    """
    conn = get_connection()

    choral_clause = "" if include_non_choral else "AND is_choral = 1"

    rows = conn.execute(
        f"""
        SELECT work_id, title_primary, title_alternates_json, incipit,
               text_author, key_text, year_composition, parent_work_id
        FROM works
        WHERE LOWER(composer_norm) LIKE LOWER(?)
          AND title_normalized LIKE ?
          {choral_clause}
        ORDER BY title_primary, work_id
        """,
        (f"%{composer}%", f"%{normalize_title(title)}%"),
    ).fetchall()

    if not rows:
        return {
            "candidates": [],
            "count": 0,
            "note": f"No works found matching composer='{composer}' and title='{title}'.",
        }

    work_ids = [r["work_id"] for r in rows]
    placeholders = ",".join("?" * len(work_ids))

    # Primary voicing for each candidate
    voicing_rows = conn.execute(
        f"SELECT work_id, voicing_string FROM voicings"
        f" WHERE work_id IN ({placeholders}) AND is_primary = 1"
        f" ORDER BY voicing_id",
        work_ids,
    ).fetchall()
    primary_voicing: dict[str, str] = {r["work_id"]: r["voicing_string"] for r in voicing_rows}

    # Parent work titles (for works that are movements/songs within a cycle)
    parent_ids = [r["parent_work_id"] for r in rows if r["parent_work_id"]]
    parent_titles: dict[str, str] = {}
    if parent_ids:
        parent_placeholders = ",".join("?" * len(parent_ids))
        parent_rows = conn.execute(
            f"SELECT work_id, title_primary FROM works"
            f" WHERE work_id IN ({parent_placeholders})",
            parent_ids,
        ).fetchall()
        parent_titles = {r["work_id"]: r["title_primary"] for r in parent_rows}

    candidates: list[dict] = []
    for row in rows:
        alternates: list[str] = []
        if row["title_alternates_json"]:
            try:
                alternates = json.loads(row["title_alternates_json"])
            except (ValueError, TypeError):
                pass

        incipit_raw = row["incipit"] or ""
        incipit_short = incipit_raw[:80].rstrip() + ("…" if len(incipit_raw) > 80 else "")

        parent_work_id = row["parent_work_id"]
        parent_work_title = parent_titles.get(parent_work_id) if parent_work_id else None

        candidates.append({
            "work_id":          row["work_id"],
            "title_primary":    row["title_primary"],
            "title_alternates": alternates,
            "incipit":          incipit_short or None,
            "text_author":      row["text_author"],
            "key_text":         row["key_text"],
            "year_composition": row["year_composition"],
            "primary_voicing":  primary_voicing.get(row["work_id"]),
            "parent_work_id":   parent_work_id,
            "parent_work_title": parent_work_title,
        })

    n = len(candidates)
    if n == 1:
        note = "Only one match found; no disambiguation needed."
    else:
        note = (
            f"{n} works match this query. Ask the user to clarify which one they mean, "
            "using the incipit, text author, or voicing to distinguish them."
        )

    return {"candidates": candidates, "count": n, "note": note}
