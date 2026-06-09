"""
Build the unified SQLite index from all crawled and parsed data.

Usage:
    python src/build_index.py [--force]

Reads:
  data/cpdl_palestrina_parsed.json
  data/smh_<slug>.json              (one per SMH composer)
  data/smh_orphaned_media.json

Writes:
  data/index.db                     (SQLite, recreated each run)
  data/different_composer_namesakes.json  (if namesakes present)

Pass --force to skip the "overwrite?" confirmation prompt.
"""

import argparse
import json
import logging
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

from utils import normalize_voicing, normalize_title

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
SMH_DOWNLOAD_BASE = "https://www.swedishmusicalheritage.com/downloadMedia.php"


# ---------------------------------------------------------------------------
# Composer metadata
# ---------------------------------------------------------------------------

COMPOSER_NORM_MAP: dict[str, str] = {
    "Giovanni Pierluigi da Palestrina": "Palestrina, Giovanni Pierluigi da",
    "Wilhelm Stenhammar":               "Stenhammar, Wilhelm",
    "Wilhelm Peterson-Berger":          "Peterson-Berger, Wilhelm",
    "Oskar Lindberg":                   "Lindberg, Oskar",
    "Hugo Alfvén":                      "Alfvén, Hugo",
}

COMPOSER_DATES: dict[str, tuple[int, int]] = {
    "Palestrina, Giovanni Pierluigi da": (1525, 1594),
    "Stenhammar, Wilhelm":               (1871, 1927),
    "Peterson-Berger, Wilhelm":          (1867, 1942),
    "Lindberg, Oskar":                   (1887, 1955),
    "Alfvén, Hugo":                      (1872, 1960),
}

COMPOSER_PERIOD: dict[str, str] = {
    "Palestrina, Giovanni Pierluigi da": "Renaissance",
    "Stenhammar, Wilhelm":               "Late Romantic",
    "Peterson-Berger, Wilhelm":          "Late Romantic",
    "Lindberg, Oskar":                   "Late Romantic",
    "Alfvén, Hugo":                      "Late Romantic",
}

SMH_SLUG_TO_COMPOSER: dict[str, str] = {
    "stenhammar-wilhelm":      "Wilhelm Stenhammar",
    "peterson-berger-wilhelm": "Wilhelm Peterson-Berger",
    "lindberg-oskar":          "Oskar Lindberg",
    "alfven-hugo":             "Hugo Alfvén",
}

SMH_DEFAULT_LANGUAGE = "sv"

# CPDL page titles end in "(Giovanni Pierluigi da Palestrina)" etc.
_CPDL_DISAMBIG_RE = re.compile(r"\s*\([^)]+\)\s*$")

_EXT_TO_FORMAT: dict[str, str] = {
    "pdf": "PDF", "xml": "MusicXML", "mxl": "MusicXML",
    "mid": "MIDI", "midi": "MIDI", "zip": "ZIP",
    "sib": "Sibelius", "mus": "Finale", "ly": "LilyPond",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slug_normalize(s: str) -> str:
    """NFKD decompose, strip combining chars, lowercase, alphanumeric+spaces only.

    Used internally for stable work_id slug generation. Do not change — any
    change here alters existing work_ids. For search-oriented title normalization
    use utils.normalize_title instead.
    """
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def make_title_slug(title: str, max_len: int = 60) -> str:
    """Hyphenated slug from _slug_normalize, truncated to max_len characters."""
    slug = "-".join(_slug_normalize(title).split())
    return slug[:max_len].rstrip("-")


def make_work_id(composer_norm: str, title: str) -> str:
    """
    Generate base work_id as "{surname_lower}_{title_slug}".
    Diacritics in the surname are stripped via _slug_normalize.
    """
    surname_raw = composer_norm.split(",")[0]
    surname = _slug_normalize(surname_raw).replace(" ", "-")
    return f"{surname}_{make_title_slug(title)}"


def strip_cpdl_disambig(title: str) -> str:
    """Remove trailing '(Giovanni Pierluigi da Palestrina)' from CPDL page titles."""
    # Only strip if the record's title field already had the disambiguator removed;
    # fall back to page_title stripping at call sites.
    return _CPDL_DISAMBIG_RE.sub("", title).strip()


def parse_year(raw: str | None) -> int | None:
    """Extract first plausible 4-digit year from a free-text string."""
    if not raw:
        return None
    m = re.search(r"\b(1[0-9]{3}|20[0-9]{2})\b", raw)
    return int(m.group(1)) if m else None


def infer_genre(work_category: str | None) -> str | None:
    if not work_category:
        return None
    wc = work_category.lower()
    if any(w in wc for w in ("sacred", "mass", "hymn", "psalm", "motet")):
        return "Sacred"
    if any(w in wc for w in ("choir", "chorus", "song", "choral", "part-song", "partsong")):
        return "Secular"
    return None


_CHORAL_INDICATORS = [
    "choir", "kör", "chorus", "vocal ensemble", "vocal group",
    "ttbb", "ssaa", "satb", "ssaattbb", "mixed choir", "male choir",
    "female choir", "children's choir", "youth choir", "a cappella choir",
]

_NON_CHORAL_INDICATORS = [
    "voice and piano", "voice and orchestra", "solo voice",
    "symphony", "concerto", "chamber music", "string quartet", "organ",
    "piano", "orchestra",
]


def infer_is_choral(work_category: str | None, source: str) -> int | None:
    """Return 1 (choral), 0 (non-choral), or None (unknown) for a work.

    CPDL is exclusively choral content → always 1.
    For SMH records, match against keyword lists; choral keywords take
    precedence over non-choral ones so 'choir with piano' → 1.
    Returns None when classification is uncertain (caller should log and
    default to 0).
    """
    if source == "cpdl":
        return 1

    if not work_category:
        return None

    wc = work_category.lower()

    for indicator in _CHORAL_INDICATORS:
        if indicator in wc:
            return 1

    for indicator in _NON_CHORAL_INDICATORS:
        if indicator in wc:
            return 0

    return None


def format_from_ext(filename: str) -> str | None:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _EXT_TO_FORMAT.get(ext)


def smh_download_url(media_id) -> str:
    return f"{SMH_DOWNLOAD_BASE}?m={media_id}"


# ---------------------------------------------------------------------------
# SMH voicing parser
# ---------------------------------------------------------------------------

# Looks like "S.A.T.B." or "SATB" or "S.S.A.T.T.B." etc.
_VOICING_TOKEN_RE = re.compile(r"^[SsAaTtBbCcMm][.SsAaTtBbCcMm ]*$")


def parse_smh_voicings(record: dict) -> list[tuple[str, bool]]:
    """
    Return [(voicing_string, is_primary)] from solo_voices_choir or instrumentation
    or work_category as fallback. Best-effort.
    """
    raw = (record.get("solo_voices_choir") or record.get("instrumentation") or "").strip()

    if raw:
        # Try comma-separated short tokens that look like voicing strings
        parts = [p.strip() for p in raw.split(",")]
        voicing_parts = [
            p for p in parts
            if p and _VOICING_TOKEN_RE.match(p.replace(".", "").replace(" ", ""))
        ]
        if voicing_parts:
            return [(vp, i == 0) for i, vp in enumerate(voicing_parts)]

        # Take the first non-empty line (covers "S.A.T.B." on its own line, etc.)
        first_line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), None)
        if first_line:
            return [(first_line[:120], True)]

    # Coarse fallback: use work_category as a single voicing string
    wc = (record.get("work_category") or "").strip()
    if wc:
        return [(wc, True)]

    return []


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_DDL = """\
CREATE TABLE works (
    work_id              TEXT PRIMARY KEY,
    composer_norm        TEXT NOT NULL,
    composer_birth_year  INTEGER,
    composer_death_year  INTEGER,
    title_primary        TEXT NOT NULL,
    title_normalized     TEXT,
    title_alternates_json TEXT,
    incipit              TEXT,
    text_author          TEXT,
    text_author_dates    TEXT,
    text_language        TEXT,
    year_composition     INTEGER,
    year_composition_raw TEXT,
    period               TEXT,
    genre_main           TEXT,
    genre_sub            TEXT,
    instruments          TEXT,
    duration_min_sec     INTEGER,
    duration_max_sec     INTEGER,
    key_text             TEXT,
    parent_work_id       TEXT REFERENCES works(work_id),
    has_free_score       BOOLEAN NOT NULL DEFAULT 0,
    is_choral            BOOLEAN NOT NULL DEFAULT 0,
    description          TEXT,
    raw_record_blob      TEXT
);

CREATE TABLE sources (
    source_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id              TEXT NOT NULL REFERENCES works(work_id),
    source_name          TEXT NOT NULL,
    source_url           TEXT NOT NULL,
    source_work_id       TEXT
);

CREATE TABLE voicings (
    voicing_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id              TEXT NOT NULL REFERENCES works(work_id),
    voicing_string       TEXT NOT NULL,
    voicing_normalized   TEXT,
    num_voices           INTEGER,
    is_primary           BOOLEAN NOT NULL DEFAULT 1,
    notes                TEXT
);

CREATE TABLE media_files (
    media_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id              TEXT NOT NULL REFERENCES works(work_id),
    format               TEXT NOT NULL,
    url                  TEXT NOT NULL,
    source               TEXT NOT NULL,
    source_media_id      TEXT,
    discovered_via       TEXT NOT NULL,
    notes                TEXT
);

CREATE INDEX idx_works_composer ON works(composer_norm);
CREATE INDEX idx_works_title_norm ON works(title_normalized);
CREATE INDEX idx_works_period ON works(period);
CREATE INDEX idx_voicings_string ON voicings(voicing_string);
CREATE INDEX idx_voicings_normalized ON voicings(voicing_normalized);
CREATE INDEX idx_voicings_work ON voicings(work_id);
CREATE INDEX idx_media_format ON media_files(format);
CREATE INDEX idx_media_work ON media_files(work_id);
CREATE INDEX idx_sources_work ON sources(work_id);
CREATE INDEX idx_works_is_choral ON works(is_choral);\
"""


def build_schema(conn: sqlite3.Connection) -> None:
    for stmt in _DDL.split(";"):
        stmt = stmt.strip()
        if stmt:
            conn.execute(stmt)
    conn.commit()


# ---------------------------------------------------------------------------
# Collision tracker
# ---------------------------------------------------------------------------

class CollisionTracker:
    def __init__(self):
        self._used: dict[str, int] = {}
        self.collisions: list[tuple[str, str]] = []

    def assign(self, base_id: str) -> str:
        n = self._used.get(base_id, 0)
        self._used[base_id] = n + 1
        if n == 0:
            return base_id
        assigned = f"{base_id}_v{n + 1}"
        self.collisions.append((base_id, assigned))
        log.warning("work_id collision: %s → assigned %s", base_id, assigned)
        return assigned


# ---------------------------------------------------------------------------
# CPDL ingest
# ---------------------------------------------------------------------------

def ingest_cpdl(conn: sqlite3.Connection, records: list[dict], tracker: CollisionTracker) -> dict:
    stats = {"works": 0, "skipped_index": 0, "voicings": 0, "media_files": 0, "sources": 0}

    for rec in records:
        if rec.get("is_index"):
            stats["skipped_index"] += 1
            continue
        if rec.get("parse_error"):
            log.warning("CPDL parse error on %s — skipping", rec.get("page_title"))
            continue

        # Title: record.title already has the disambiguator stripped by parse_cpdl.py
        # for most records, but page_title still has it. Apply strip defensively.
        raw_title = rec.get("title") or strip_cpdl_disambig(rec.get("page_title") or "")
        title = strip_cpdl_disambig(raw_title)
        if not title:
            log.warning("CPDL record has no title: %s", rec.get("page_title"))
            continue

        raw_composer = rec.get("composer") or "Giovanni Pierluigi da Palestrina"
        composer_norm = COMPOSER_NORM_MAP.get(raw_composer, raw_composer)
        birth, death = COMPOSER_DATES.get(composer_norm, (None, None))
        period = COMPOSER_PERIOD.get(composer_norm)

        base_id = make_work_id(composer_norm, title)
        work_id = tracker.assign(base_id)

        year = rec.get("year_first_publication")

        try:
            conn.execute(
                """
                INSERT INTO works (
                    work_id, composer_norm, composer_birth_year, composer_death_year,
                    title_primary, title_normalized, incipit, text_language,
                    year_composition, year_composition_raw, period,
                    genre_main, genre_sub, instruments, description, raw_record_blob,
                    is_choral
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    work_id, composer_norm, birth, death,
                    title,
                    normalize_title(title),
                    rec.get("incipit"),
                    (rec.get("languages") or [None])[0],
                    year,
                    str(year) if year is not None else None,
                    period,
                    rec.get("genre_main"),
                    rec.get("genre_sub"),
                    rec.get("instruments"),
                    rec.get("description"),
                    json.dumps(rec, ensure_ascii=False),
                    1,  # CPDL is exclusively choral
                ),
            )
        except sqlite3.IntegrityError as exc:
            log.error("Failed to insert CPDL work %s: %s", work_id, exc)
            continue

        stats["works"] += 1

        conn.execute(
            "INSERT INTO sources (work_id, source_name, source_url, source_work_id) VALUES (?,?,?,?)",
            (work_id, "cpdl", rec.get("cpdl_page_url") or "", rec.get("page_title")),
        )
        stats["sources"] += 1

        voicings = rec.get("voicings") or []
        for i, vs in enumerate(voicings):
            if not vs:
                continue
            conn.execute(
                "INSERT INTO voicings (work_id, voicing_string, voicing_normalized, num_voices, is_primary) VALUES (?,?,?,?,?)",
                (work_id, vs, normalize_voicing(vs), rec.get("number_of_voices") if i == 0 else None, i == 0),
            )
            stats["voicings"] += 1

        for edition in rec.get("editions") or []:
            for f in edition.get("files") or []:
                fmt = f.get("format")
                url = f.get("url")
                if not fmt or not url:
                    continue
                conn.execute(
                    """
                    INSERT INTO media_files
                        (work_id, format, url, source, source_media_id, discovered_via)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (work_id, fmt, url, "cpdl", edition.get("cpdl_number"), "work_page"),
                )
                stats["media_files"] += 1

    conn.commit()
    return stats


# ---------------------------------------------------------------------------
# SMH ingest
# ---------------------------------------------------------------------------

def ingest_smh(
    conn: sqlite3.Connection,
    slug: str,
    records: list[dict],
    tracker: CollisionTracker,
) -> dict:
    stats = {"works": 0, "voicings": 0, "media_files": 0, "sources": 0}

    raw_composer = SMH_SLUG_TO_COMPOSER.get(slug, slug)
    composer_norm = COMPOSER_NORM_MAP.get(raw_composer, raw_composer)
    birth, death = COMPOSER_DATES.get(composer_norm, (None, None))
    period = COMPOSER_PERIOD.get(composer_norm)

    for rec in records:
        title = (rec.get("title") or "").strip()
        if not title:
            log.warning("SMH %s record %s has no title — skipping", slug, rec.get("smh_work_id"))
            continue

        incipit = None
        libretto = rec.get("libretto_or_text")
        if libretto:
            first_line = libretto.splitlines()[0].strip()
            incipit = (first_line[:100] or None)

        genre_main = infer_genre(rec.get("work_category"))
        year_raw = rec.get("year_composition")
        year_int = parse_year(year_raw)

        alternates = rec.get("title_alternates") or []
        alternates_json = json.dumps(alternates, ensure_ascii=False) if alternates else None

        base_id = make_work_id(composer_norm, title)
        work_id = tracker.assign(base_id)

        work_category = rec.get("work_category")
        is_choral_val = infer_is_choral(work_category, "smh")
        if is_choral_val is None:
            log.debug(
                "SMH %s (%s): is_choral unknown for work_category=%r — defaulting to 0",
                work_id, title, work_category,
            )
            is_choral_val = 0

        # Exclude raw_html from the blob (it's large and redundant).
        blob = {k: v for k, v in rec.items() if k != "raw_html"}

        try:
            conn.execute(
                """
                INSERT INTO works (
                    work_id, composer_norm, composer_birth_year, composer_death_year,
                    title_primary, title_normalized, title_alternates_json, incipit,
                    text_author, text_author_dates, text_language,
                    year_composition, year_composition_raw, period,
                    genre_main, instruments, duration_min_sec, duration_max_sec,
                    description, raw_record_blob, is_choral
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    work_id, composer_norm, birth, death,
                    title,
                    normalize_title(title),
                    alternates_json,
                    incipit,
                    rec.get("text_author"),
                    rec.get("text_author_dates"),
                    SMH_DEFAULT_LANGUAGE,
                    year_int,
                    year_raw,
                    period,
                    genre_main,
                    rec.get("instrumentation"),
                    rec.get("duration_min_sec"),
                    rec.get("duration_max_sec"),
                    rec.get("description"),
                    json.dumps(blob, ensure_ascii=False),
                    is_choral_val,
                ),
            )
        except sqlite3.IntegrityError as exc:
            log.error("Failed to insert SMH work %s (%s): %s", work_id, title, exc)
            continue

        stats["works"] += 1

        conn.execute(
            "INSERT INTO sources (work_id, source_name, source_url, source_work_id) VALUES (?,?,?,?)",
            (work_id, "smh", rec.get("smh_url") or "", rec.get("smh_work_id")),
        )
        stats["sources"] += 1

        for vs, is_primary in parse_smh_voicings(rec):
            conn.execute(
                "INSERT INTO voicings (work_id, voicing_string, voicing_normalized, is_primary) VALUES (?,?,?,?)",
                (work_id, vs, normalize_voicing(vs), is_primary),
            )
            stats["voicings"] += 1

        for mf in rec.get("media_files") or []:
            fmt = mf.get("format")
            url = mf.get("url")
            if not fmt or not url:
                continue
            conn.execute(
                """
                INSERT INTO media_files
                    (work_id, format, url, source, source_media_id, discovered_via)
                VALUES (?,?,?,?,?,?)
                """,
                (work_id, fmt, url, "smh", str(mf.get("media_id") or ""), "work_page"),
            )
            stats["media_files"] += 1

    conn.commit()
    return stats


# ---------------------------------------------------------------------------
# Orphan attachment
# ---------------------------------------------------------------------------

def attach_orphans(conn: sqlite3.Connection, orphan_data: dict) -> dict:
    stats = {"linked_elsewhere": 0, "namesakes_logged": 0}

    # Build {smh_work_id: db_work_id} from the sources table
    rows = conn.execute(
        "SELECT work_id, source_work_id FROM sources WHERE source_name = 'smh'"
    ).fetchall()
    smh_work_map: dict[str, str] = {src_wid: db_wid for db_wid, src_wid in rows if src_wid}

    for entry in orphan_data.get("linked_elsewhere") or []:
        media_id = entry.get("media_id")
        matched_work_id = entry.get("matched_work_id")  # e.g. "W1238"
        filename = entry.get("filename") or ""

        db_work_id = smh_work_map.get(matched_work_id)
        if not db_work_id:
            log.warning(
                "orphan attachment: no db work found for SMH %s (media_id=%s)",
                matched_work_id, media_id,
            )
            continue

        fmt = format_from_ext(filename) or "PDF"
        url = smh_download_url(media_id)

        conn.execute(
            """
            INSERT INTO media_files
                (work_id, format, url, source, source_media_id, discovered_via, notes)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                db_work_id, fmt, url, "smh", str(media_id), "orphan_probe",
                "Additional edition discovered via media-ID probe",
            ),
        )
        stats["linked_elsewhere"] += 1

    conn.commit()

    # Export different-composer namesakes to JSON for later reference.
    namesakes = orphan_data.get("different_composer_same_surname") or []
    if namesakes:
        out_path = DATA_DIR / "different_composer_namesakes.json"
        out_path.write_text(json.dumps(namesakes, ensure_ascii=False, indent=2), encoding="utf-8")
        stats["namesakes_logged"] = len(namesakes)
        log.info("Wrote %d namesake records to %s", len(namesakes), out_path)

    return stats


# ---------------------------------------------------------------------------
# has_free_score update
# ---------------------------------------------------------------------------

def update_has_free_score(conn: sqlite3.Connection) -> int:
    conn.execute(
        "UPDATE works SET has_free_score = 1 "
        "WHERE work_id IN (SELECT DISTINCT work_id FROM media_files)"
    )
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM works WHERE has_free_score = 1").fetchone()[0]


# ---------------------------------------------------------------------------
# Sanity report
# ---------------------------------------------------------------------------

def sanity_report(
    conn: sqlite3.Connection,
    all_stats: dict,
    tracker: CollisionTracker,
) -> None:
    total_works    = conn.execute("SELECT COUNT(*) FROM works").fetchone()[0]
    total_voicings = conn.execute("SELECT COUNT(*) FROM voicings").fetchone()[0]
    total_media    = conn.execute("SELECT COUNT(*) FROM media_files").fetchone()[0]
    total_sources  = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]

    media_by_via = conn.execute(
        "SELECT discovered_via, COUNT(*) FROM media_files GROUP BY discovered_via"
    ).fetchall()
    media_by_src = conn.execute(
        "SELECT source, COUNT(*) FROM media_files GROUP BY source"
    ).fetchall()
    per_composer = conn.execute(
        "SELECT composer_norm, COUNT(*) FROM works GROUP BY composer_norm ORDER BY composer_norm"
    ).fetchall()

    is_choral_rows = conn.execute(
        "SELECT is_choral, COUNT(*) FROM works GROUP BY is_choral ORDER BY is_choral DESC"
    ).fetchall()
    works_free     = conn.execute("SELECT COUNT(*) FROM works WHERE has_free_score = 1").fetchone()[0]
    no_title       = conn.execute(
        "SELECT COUNT(*) FROM works WHERE title_primary IS NULL OR title_primary = ''"
    ).fetchone()[0]
    no_voicings    = conn.execute(
        "SELECT COUNT(*) FROM works WHERE work_id NOT IN (SELECT DISTINCT work_id FROM voicings)"
    ).fetchone()[0]
    no_genre       = conn.execute("SELECT COUNT(*) FROM works WHERE genre_main IS NULL").fetchone()[0]

    cpdl_stats = all_stats.get("cpdl", {})

    print()
    print("=== build_index.py sanity report ===")
    print(f"Total works inserted          : {total_works}")
    for comp, cnt in per_composer:
        print(f"  {comp:<45}: {cnt}")
    if cpdl_stats.get("skipped_index"):
        print(f"  CPDL index pages skipped    : {cpdl_stats['skipped_index']}")
    print()
    print(f"Total voicings rows           : {total_voicings}")
    print(f"Total media_files rows        : {total_media}")
    for via, cnt in sorted(media_by_via):
        print(f"  discovered_via={via:<15}: {cnt}")
    for src, cnt in sorted(media_by_src):
        print(f"  source={src:<22}: {cnt}")
    print(f"Total sources rows            : {total_sources}")
    print()
    print("is_choral distribution:")
    for flag, cnt in is_choral_rows:
        label = "choral" if flag else "non-choral/unknown"
        print(f"  is_choral={flag} ({label:<20}): {cnt}")
    print(f"has_free_score = 1            : {works_free}")
    print(f"Missing critical fields:")
    print(f"  no title                    : {no_title}")
    print(f"  no voicings                 : {no_voicings}")
    print(f"  no genre                    : {no_genre}")
    print()
    print(f"work_id collisions            : {len(tracker.collisions)}")
    if tracker.collisions:
        print("  Sample (up to 5):")
        for base, assigned in tracker.collisions[:5]:
            print(f"    {base} → {assigned}")

    namesakes_path = DATA_DIR / "different_composer_namesakes.json"
    if namesakes_path.exists():
        n = len(json.loads(namesakes_path.read_text(encoding="utf-8")))
        print(f"Namesakes logged              : {n} (different_composer_namesakes.json)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Build unified SQLite index from crawled data")
    parser.add_argument("--force", action="store_true",
                        help="Overwrite existing index.db without prompting")
    args = parser.parse_args()

    db_path = DATA_DIR / "index.db"

    if db_path.exists():
        if not args.force:
            answer = input(f"{db_path} already exists. Delete and recreate? [y/N] ").strip().lower()
            if answer != "y":
                print("Aborted.")
                sys.exit(0)
        db_path.unlink()
        log.info("Deleted existing %s", db_path)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    build_schema(conn)
    log.info("Schema created in %s", db_path)

    tracker = CollisionTracker()
    all_stats: dict = {}

    # CPDL
    cpdl_path = DATA_DIR / "cpdl_palestrina_parsed.json"
    if cpdl_path.exists():
        records = json.loads(cpdl_path.read_text(encoding="utf-8"))
        log.info("Ingesting %d CPDL records…", len(records))
        s = ingest_cpdl(conn, records, tracker)
        all_stats["cpdl"] = s
        log.info(
            "CPDL done: %d works, %d skipped (index), %d voicings, %d media_files",
            s["works"], s["skipped_index"], s["voicings"], s["media_files"],
        )
    else:
        log.warning("CPDL parsed file not found: %s", cpdl_path)

    # SMH composers
    smh_slugs = ["stenhammar-wilhelm", "peterson-berger-wilhelm", "lindberg-oskar", "alfven-hugo"]
    for slug in smh_slugs:
        smh_path = DATA_DIR / f"smh_{slug}.json"
        if not smh_path.exists():
            log.warning("SMH file not found: %s", smh_path)
            continue
        records = json.loads(smh_path.read_text(encoding="utf-8"))
        log.info("Ingesting %d SMH records for %s…", len(records), slug)
        s = ingest_smh(conn, slug, records, tracker)
        all_stats[f"smh_{slug}"] = s
        log.info(
            "SMH %s done: %d works, %d voicings, %d media_files",
            slug, s["works"], s["voicings"], s["media_files"],
        )

    # Orphan attachment
    orphan_path = DATA_DIR / "smh_orphaned_media.json"
    if orphan_path.exists():
        orphan_data = json.loads(orphan_path.read_text(encoding="utf-8"))
        log.info("Attaching orphaned media…")
        s = attach_orphans(conn, orphan_data)
        all_stats["orphans"] = s
        log.info(
            "Orphans: %d linked_elsewhere attached, %d namesakes logged",
            s["linked_elsewhere"], s["namesakes_logged"],
        )
    else:
        log.warning("Orphaned media file not found: %s", orphan_path)

    # has_free_score
    free_count = update_has_free_score(conn)
    log.info("has_free_score updated: %d works have at least one media file", free_count)

    conn.close()
    log.info("Index written to %s", db_path)

    conn = sqlite3.connect(db_path)
    sanity_report(conn, all_stats, tracker)
    conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    main()
