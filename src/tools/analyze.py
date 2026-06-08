"""
analyze_score_features — music21-based MusicXML analysis tool.

Downloads and caches a MusicXML file for a given work_id, then uses
music21 to compute per-voice ranges and basic structural features.
music21 is imported lazily so importing this module doesn't force the
heavy dependency on callers that don't need it.
"""

import hashlib
import re
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from db import get_connection

TOOL_SPEC = {
    "name": "analyze_score_features",
    "description": (
        "Downloads a MusicXML score and computes structural features: per-voice "
        "ranges, key signature, time signature, total measure count, estimated "
        "duration. Use to fill in metadata that the catalogs don't provide, "
        "especially per-voice vocal ranges."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "work_id": {
                "type": "string",
                "description": (
                    "Work_id from the index. The tool will find a MusicXML media "
                    "file for this work, or report if none is available."
                ),
            }
        },
        "required": ["work_id"],
    },
}

_USER_AGENT = (
    "ChoralPieceFinder/0.1 (university IR lab; axel.riben2@gmail.com; "
    "https://github.com/axelriben/choral-piece-finder) python-requests/2.x"
)
_CACHE_DIR = Path(__file__).parent.parent.parent / "data" / "score_cache"
_MUSICXML_FORMATS = ("MusicXML", "mxl", "xml")

# Assumed default tempo (quarter-note BPM) when no MetronomeMark is present.
_DEFAULT_TEMPO = 80


def _cache_path(work_id: str, source_media_id: str | None, url: str) -> Path:
    """Return the local cache path for a score file."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if source_media_id:
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", source_media_id)
    else:
        safe_id = hashlib.sha1(url.encode()).hexdigest()[:12]
    return _CACHE_DIR / f"{work_id}__{safe_id}.mxl"


def _download(url: str, dest: Path) -> None:
    """Download *url* to *dest*, streaming to keep memory low."""
    resp = requests.get(
        url,
        headers={"User-Agent": _USER_AGENT},
        timeout=30,
        stream=True,
    )
    resp.raise_for_status()
    dest.write_bytes(resp.content)


def _pitch_to_scientific(pitch) -> str:
    """Return scientific notation string for a music21 Pitch object."""
    return pitch.nameWithOctave


def analyze_score_features(work_id: str) -> dict:
    """Analyse the MusicXML score for *work_id* and return computed features.

    Downloads the first MusicXML media file found for the work (caching it
    at data/score_cache/) and uses music21 to compute:
      - per-part pitch ranges
      - estimated key
      - time signatures present
      - measure count
      - rough duration estimate

    Returns {"available": False, "reason": "..."} when no MusicXML is
    available or parsing fails.
    """
    conn = get_connection()

    if not conn.execute("SELECT 1 FROM works WHERE work_id = ?", (work_id,)).fetchone():
        return {"available": False, "reason": f"work_id not found: {work_id}"}

    fmt_placeholders = ",".join("?" * len(_MUSICXML_FORMATS))
    media_row = conn.execute(
        f"SELECT format, url, source_media_id FROM media_files"
        f" WHERE work_id = ? AND format IN ({fmt_placeholders})"
        f" ORDER BY media_id ASC LIMIT 1",
        (work_id, *_MUSICXML_FORMATS),
    ).fetchone()

    if media_row is None:
        return {"available": False, "reason": "no MusicXML source available for this work"}

    url = media_row["url"]
    source_media_id = media_row["source_media_id"]
    cache_file = _cache_path(work_id, source_media_id, url)

    if not cache_file.exists():
        try:
            _download(url, cache_file)
        except Exception as exc:
            return {"available": False, "reason": f"download failed: {exc}"}

    try:
        from music21 import converter
        score = converter.parse(str(cache_file))
    except Exception as exc:
        cache_file.unlink(missing_ok=True)  # don't keep a broken cache entry
        return {"available": False, "reason": f"parse failed: {exc}"}

    # --- Per-part pitch ranges ---
    parts_info: list[dict] = []
    score_parts = score.parts if hasattr(score, "parts") else []
    for part in score_parts:
        part_name = part.partName or part.id or "Unknown"
        lowest = None
        highest = None

        for elem in part.recurse().notes:
            pitches = list(elem.pitches)  # notes have one; chords have several
            for p in pitches:
                if lowest is None or p.midi < lowest.midi:
                    lowest = p
                if highest is None or p.midi > highest.midi:
                    highest = p

        if lowest is None:
            parts_info.append({
                "part_name": part_name,
                "voice_label": part_name,
                "lowest_pitch": None,
                "highest_pitch": None,
                "range_semitones": None,
                "lowest_pitch_midi": None,
                "highest_pitch_midi": None,
            })
        else:
            parts_info.append({
                "part_name": part_name,
                "voice_label": part_name,
                "lowest_pitch": _pitch_to_scientific(lowest),
                "highest_pitch": _pitch_to_scientific(highest),
                "range_semitones": highest.midi - lowest.midi,
                "lowest_pitch_midi": lowest.midi,
                "highest_pitch_midi": highest.midi,
            })

    # --- Key estimate ---
    try:
        key_obj = score.analyze("key")
        key_estimated = f"{key_obj.tonic.name} {key_obj.mode}"
    except Exception:
        key_estimated = None

    # --- Time signatures ---
    from music21 import meter
    ts_set: list[str] = []
    seen_ts: set[str] = set()
    for ts in score.recurse().getElementsByClass(meter.TimeSignature):
        label = ts.ratioString
        if label not in seen_ts:
            seen_ts.add(label)
            ts_set.append(label)

    # --- Measure count (from first part) ---
    from music21 import stream
    num_measures = 0
    if score_parts:
        measures = score_parts[0].getElementsByClass(stream.Measure)
        num_measures = len(measures)

    # --- Duration estimate ---
    # Use music21's computed total duration in quarter-lengths, then convert
    # to seconds at the default or detected tempo.
    try:
        from music21 import tempo as m21_tempo
        marks = list(score.recurse().getElementsByClass(m21_tempo.MetronomeMark))
        bpm = marks[0].number if marks and marks[0].number else _DEFAULT_TEMPO
    except Exception:
        bpm = _DEFAULT_TEMPO

    total_ql = score.duration.quarterLength if score.duration else 0.0
    estimated_duration_sec = round((total_ql / bpm) * 60) if bpm else None

    return {
        "available": True,
        "work_id": work_id,
        "source_url": url,
        "parts": parts_info,
        "key_estimated": key_estimated,
        "time_signatures": ts_set,
        "num_measures": num_measures,
        "estimated_duration_sec": estimated_duration_sec,
    }
