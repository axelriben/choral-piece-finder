"""
Parse CPDL wikitext into structured work records using mwparserfromhell.

Standalone usage (re-processes existing crawl data without re-crawling):
    python src/parse_cpdl.py

Reads:  data/cpdl_palestrina.json          (raw_wikitext field required)
Writes: data/cpdl_palestrina_parsed.json
Prints a sanity-check report to stdout.

Can also be imported and used as a library:
    from parse_cpdl import parse_wikitext
"""

import json
import logging
import re
from pathlib import Path

import mwparserfromhell
from mwparserfromhell.nodes import Template, Wikilink

log = logging.getLogger(__name__)

WIKI_BASE = "https://www2.cpdl.org/wiki/index.php"

# Map format-template names and file extensions → canonical format label
_FORMAT_MAP: dict[str, str] = {
    "pdf": "PDF",
    "xml": "MusicXML",
    "mxl": "MusicXML",
    "mid": "MIDI",
    "midi": "MIDI",
    "zip": "ZIP",
    "sib": "Sibelius",
    "mus": "Finale",
    "ly": "LilyPond",
    "ly2": "LilyPond",
    "cap": "Capella",
    "nwc": "NoteWorthy",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _arg(tmpl: Template, key: int | str, default: str | None = None) -> str | None:
    """Return a template argument value as a stripped string, or default."""
    try:
        return str(tmpl.get(key).value).strip() or default
    except (ValueError, KeyError):
        return default


def _plain(wikicode_or_str) -> str:
    """Strip all wiki markup and return plain text."""
    if isinstance(wikicode_or_str, str):
        wikicode_or_str = mwparserfromhell.parse(wikicode_or_str)
    return wikicode_or_str.strip_code().strip()


def _plain_expand(val: str) -> str:
    """
    Like _plain(), but first expands simple templates (e.g. {{cat|SSAATBB}})
    to their first positional argument before stripping remaining markup.
    This prevents {{cat|...}}-style display templates from being silently
    dropped by strip_code(), which would lose the content they carry.
    """
    code = mwparserfromhell.parse(val)
    # Collect snapshot to avoid mutating while iterating
    for tmpl in list(code.filter_templates()):
        try:
            replacement = str(tmpl.get(1).value).strip()
        except (ValueError, KeyError):
            replacement = ""
        try:
            code.replace(tmpl, replacement)
        except ValueError:
            pass  # already replaced by an earlier (outer) substitution
    return code.strip_code().strip()


def _find(templates, name: str) -> Template | None:
    """Return the first template whose name matches (case-insensitive)."""
    nl = name.lower()
    for t in templates:
        if t.name.strip().lower() == nl:
            return t
    return None


def _find_all(templates, name: str) -> list[Template]:
    nl = name.lower()
    return [t for t in templates if t.name.strip().lower() == nl]


def _format_from_ext(filename: str) -> str | None:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _FORMAT_MAP.get(ext)


def _format_from_template_name(tmpl_name: str) -> str | None:
    return _FORMAT_MAP.get(tmpl_name.strip().lower())


# ---------------------------------------------------------------------------
# Music files section parser
# ---------------------------------------------------------------------------

def _parse_music_files(raw_wikitext: str) -> list[dict]:
    """
    Parse the ==Music files== section into a list of edition dicts.

    CPDL wikitext structure (one edition per CPDLno):
        *{{PostedDate|YYYY-MM-DD}} {{CPDLno|NNNNN}}
        :• [[Media:file.pdf|{{pdf}}]] [[Media:file.mxl|{{XML}}]]
        {{Editor|Name|date}}{{Copy|status}}

    PostedDate always appears immediately before CPDLno on the same bullet,
    so we buffer it as a pending value until CPDLno is encountered.
    """
    mf_start = raw_wikitext.find("==Music files==")
    if mf_start < 0:
        mf_start = raw_wikitext.find("== Music files ==")
    if mf_start < 0:
        return []

    mf_text = raw_wikitext[mf_start:]
    # Trim at the next top-level section heading
    next_sec = re.search(r"\n==[^=]", mf_text[15:])
    if next_sec:
        mf_text = mf_text[: 15 + next_sec.start()]

    parsed = mwparserfromhell.parse(mf_text)

    editions: list[dict] = []
    current: dict | None = None
    pending_date: str | None = None  # PostedDate appears before CPDLno

    for node in parsed.nodes:
        if isinstance(node, Template):
            name = node.name.strip().lower()

            if name == "posteddate":
                pending_date = _arg(node, 1)

            elif name == "cpdlno":
                if current is not None:
                    editions.append(current)
                current = {
                    "cpdl_number": _arg(node, 1),
                    "post_date": pending_date,
                    "editor": None,
                    "copyright_status": None,
                    "files": [],
                }
                pending_date = None

            elif name == "editor" and current is not None:
                current["editor"] = _arg(node, 1)

            elif name == "copy" and current is not None:
                current["copyright_status"] = _arg(node, 1)

        elif isinstance(node, Wikilink) and current is not None:
            title_str = str(node.title).strip()
            if not title_str.startswith("Media:"):
                continue
            filename = title_str[len("Media:"):]

            # Format: derived from the display text, which is a format template
            fmt = None
            if node.text:
                inner = mwparserfromhell.parse(str(node.text))
                for t in inner.filter_templates():
                    fmt = _format_from_template_name(t.name.strip())
                    if fmt:
                        break
            if fmt is None:
                fmt = _format_from_ext(filename)

            current["files"].append({
                "format": fmt,
                "filename": filename,
                "url": f"{WIKI_BASE}/Special:FilePath/{filename}",
            })

    if current is not None:
        editions.append(current)

    return editions


# ---------------------------------------------------------------------------
# Main work-page parser
# ---------------------------------------------------------------------------

def parse_wikitext(page_title: str, raw_wikitext: str) -> dict:
    """
    Parse one CPDL work-page wikitext into a structured dict.

    Returns is_index=True (with no work fields) if the page has no
    {{Composer}} template — these are list/disambiguation pages.
    """
    try:
        parsed = mwparserfromhell.parse(raw_wikitext)
    except Exception as exc:
        log.warning("mwparserfromhell failed on %s: %s", page_title, exc)
        return {"page_title": page_title, "is_index": None, "parse_error": str(exc)}

    # Use recursive=False so we only see top-level templates for the page fields.
    # Nested templates (e.g. {{cat|...}} inside Voicing|add=) are handled
    # locally when we call _plain() on the argument value.
    tmpls = parsed.filter_templates(recursive=False)

    composer_tmpl = _find(tmpls, "Composer")
    if composer_tmpl is None:
        return {"page_title": page_title, "is_index": True}

    # --- Title ---
    title_tmpl = _find(tmpls, "Title")
    title = _plain(_arg(title_tmpl, 1, "")) if title_tmpl else page_title

    # --- Composer ---
    composer = _plain(_arg(composer_tmpl, 1, ""))

    # --- Voicing ---
    voicing_tmpl = _find(tmpls, "Voicing")
    voicings: list[str] = []
    number_of_voices: int | None = None
    if voicing_tmpl:
        raw_n = _arg(voicing_tmpl, 1)
        if raw_n:
            try:
                number_of_voices = int(raw_n)
            except ValueError:
                pass
        primary = _arg(voicing_tmpl, 2)
        if primary:
            voicings.append(primary)
        add = _arg(voicing_tmpl, "add")
        if add:
            voicings.append(_plain_expand(add))

    # --- Genre ---
    genre_tmpl = _find(tmpls, "Genre")
    genre_main = _arg(genre_tmpl, 1) if genre_tmpl else None
    genre_sub = _arg(genre_tmpl, 2) if genre_tmpl else None

    # --- Language ---
    lang_tmpl = _find(tmpls, "Language")
    languages: list[str] = []
    if lang_tmpl:
        raw_lang = _arg(lang_tmpl, 1, "")
        languages = [l.strip() for l in raw_lang.split(",") if l.strip()]

    # --- Instruments ---
    instr_tmpl = _find(tmpls, "Instruments")
    instruments = _arg(instr_tmpl, 1) if instr_tmpl else None

    # --- Publication history ---
    pub_tmpls = _find_all(tmpls, "Pub")
    publication_history: list[dict] = []
    year_first_publication: int | None = None
    for pt in pub_tmpls:
        pub_n = _arg(pt, 1)
        year_str = _arg(pt, 2)
        year: int | None = None
        if year_str:
            try:
                year = int(year_str)
            except ValueError:
                pass
        # Collect remaining positional args as source description
        source_parts = []
        for i in range(3, 12):
            v = _arg(pt, i)
            if v is None:
                break
            cleaned = _plain(v)
            if cleaned:
                source_parts.append(cleaned)
        entry = {
            "pub_number": pub_n,
            "year": year,
            "source": ", ".join(source_parts) if source_parts else None,
        }
        publication_history.append(entry)
        if pub_n == "1" and year and year_first_publication is None:
            year_first_publication = year

    # --- Description ---
    descr_tmpl = _find(tmpls, "Descr")
    description = _plain(_arg(descr_tmpl, 1, "")) if descr_tmpl else None

    # --- Incipit (first Text block) ---
    # Text template arguments: {{Text|language|text content}}
    text_tmpls = _find_all(tmpls, "Text")
    incipit: str | None = None
    if text_tmpls:
        raw_text = _arg(text_tmpls[0], 2, "")
        if raw_text:
            plain = _plain(raw_text)
            incipit = plain[:100].strip() or None

    # --- Music file editions ---
    editions = _parse_music_files(raw_wikitext)

    return {
        "page_title": page_title,
        "is_index": False,
        "title": title or None,
        "composer": composer or None,
        "voicings": voicings,
        "number_of_voices": number_of_voices,
        "genre_main": genre_main,
        "genre_sub": genre_sub,
        "languages": languages,
        "instruments": instruments,
        "year_first_publication": year_first_publication,
        "publication_history": publication_history,
        "description": description or None,
        "incipit": incipit,
        "editions": editions,
        # Retain the CPDL page URL from the original crawl record if available
        "cpdl_page_url": None,  # filled in by the reprocessing script below
    }


# ---------------------------------------------------------------------------
# Re-processing entry point
# ---------------------------------------------------------------------------

def reprocess(input_path: Path, output_path: Path) -> None:
    log.info("Loading %s", input_path)
    raw_records = json.loads(input_path.read_text(encoding="utf-8"))
    log.info("Parsing %d records…", len(raw_records))

    parsed_records = []
    errors = 0
    for rec in raw_records:
        page_title = rec.get("title") or rec.get("page_title", "")
        wikitext = rec.get("raw_wikitext", "")
        try:
            result = parse_wikitext(page_title, wikitext)
        except Exception as exc:
            log.warning("Unhandled error on %s: %s", page_title, exc)
            result = {"page_title": page_title, "is_index": None, "parse_error": str(exc)}
            errors += 1
        # Carry over the original crawl URL
        result["cpdl_page_url"] = rec.get("cpdl_page_url")
        parsed_records.append(result)

    output_path.write_text(
        json.dumps(parsed_records, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    log.info("Wrote %d records to %s", len(parsed_records), output_path)

    # Sanity-check report
    work_pages = [r for r in parsed_records if not r.get("is_index")]
    index_pages = [r for r in parsed_records if r.get("is_index")]
    parse_errors = [r for r in parsed_records if r.get("parse_error")]

    has_title = sum(1 for r in work_pages if r.get("title"))
    has_voicing = sum(1 for r in work_pages if r.get("voicings"))
    has_languages = sum(1 for r in work_pages if r.get("languages"))
    has_scores = sum(1 for r in work_pages if r.get("editions"))

    print()
    print("=== Sanity check ===")
    print(f"Total records        : {len(parsed_records)}")
    print(f"  Work pages         : {len(work_pages)}")
    print(f"  Index/list pages   : {len(index_pages)}")
    print(f"  Parse errors       : {errors + len(parse_errors)}")
    print()
    print(f"Among work pages ({len(work_pages)}):")
    print(f"  title populated    : {has_title}  ({100*has_title//max(len(work_pages),1)}%)")
    print(f"  voicing populated  : {has_voicing}  ({100*has_voicing//max(len(work_pages),1)}%)")
    print(f"  languages populated: {has_languages}  ({100*has_languages//max(len(work_pages),1)}%)")
    print(f"  ≥1 score edition   : {has_scores}  ({100*has_scores//max(len(work_pages),1)}%)")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    data_dir = Path(__file__).parent.parent / "data"
    reprocess(
        input_path=data_dir / "cpdl_palestrina.json",
        output_path=data_dir / "cpdl_palestrina_parsed.json",
    )
