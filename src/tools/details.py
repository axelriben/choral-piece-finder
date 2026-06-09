"""
get_work_details — full-record retrieval tool for the choral-piece-finder agent.

Returns every field for a single work, plus its related voicings, media files,
and source records.  Used after search_local_index narrows to a specific work.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import get_connection

TOOL_SPEC = {
    "name": "get_work_details",
    "description": (
        "Returns the full record for a single work given its work_id. "
        "Use after search_local_index narrows to a specific work the user "
        "wants to know more about."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "work_id": {
                "type": "string",
                "description": "Internal work_id from a previous search result.",
            }
        },
        "required": ["work_id"],
    },
}


def get_work_details(work_id: str) -> dict:
    """Return the complete record for *work_id* including all related rows.

    Returns a dict with all works columns plus:
      sources     — list of {source_name, source_url, source_work_id}
      voicings    — list of {voicing_string, num_voices, is_primary, notes}
      media_files — list of {format, url, source, source_media_id,
                              discovered_via, notes}

    Returns {"error": "work_id not found", "work_id": ...} if the id is absent.
    """
    conn = get_connection()

    work_row = conn.execute(
        "SELECT * FROM works WHERE work_id = ?", (work_id,)
    ).fetchone()

    if work_row is None:
        return {
            "error": "work_id_not_found",
            "work_id_attempted": work_id,
            "message": (
                "The work_id provided does not exist in the index. "
                "Call search_local_index with the user's query to obtain a valid work_id, "
                "then retry this tool with that work_id."
            ),
        }

    work = dict(work_row)

    # Decode title_alternates_json into a real list
    if work.get("title_alternates_json"):
        try:
            work["title_alternates"] = json.loads(work["title_alternates_json"])
        except (ValueError, TypeError):
            work["title_alternates"] = []
    else:
        work["title_alternates"] = []
    del work["title_alternates_json"]

    # Omit the raw blob from the response — it's large and redundant
    work.pop("raw_record_blob", None)

    work["has_free_score"] = bool(work.get("has_free_score"))

    source_rows = conn.execute(
        "SELECT source_name, source_url, source_work_id"
        " FROM sources WHERE work_id = ? ORDER BY source_id",
        (work_id,),
    ).fetchall()
    work["sources"] = [dict(r) for r in source_rows]

    voicing_rows = conn.execute(
        "SELECT voicing_string, num_voices, is_primary, notes"
        " FROM voicings WHERE work_id = ? ORDER BY is_primary DESC, voicing_id",
        (work_id,),
    ).fetchall()
    work["voicings"] = [
        {
            "voicing_string": r["voicing_string"],
            "num_voices":     r["num_voices"],
            "is_primary":     bool(r["is_primary"]),
            "notes":          r["notes"],
        }
        for r in voicing_rows
    ]

    media_rows = conn.execute(
        "SELECT format, url, source, source_media_id, discovered_via, notes"
        " FROM media_files WHERE work_id = ?"
        " ORDER BY discovered_via, format, media_id",
        (work_id,),
    ).fetchall()
    work["media_files"] = [dict(r) for r in media_rows]

    return work
