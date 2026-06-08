"""
Cross-reference SMH media probe results against crawled work records.

Usage:
    python src/match_orphaned_media.py

Reads:
  data/smh_media_probe.json     — output of probe_smh_media.py
  data/smh_<slug>.json          — one file per crawled composer

For each probe-found file, classifies it into one of three buckets:
  matched         — media_id is already listed in a crawled record
  linked_elsewhere — composer matches one of our slugs AND title fuzzy-matches
                     a known record, but that record's media_files doesn't
                     include this media_id (unreferenced edition)
  orphaned        — composer unknown to our crawl data, or composer known
                     but title can't be matched to any record

Output: data/smh_orphaned_media.json
"""

import glob
import json
import logging
import re
import unicodedata
from pathlib import Path

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

DATA_DIR = Path(__file__).parent.parent / "data"

# Composers in our crawl scope, keyed by slug.
# Used to distinguish "same surname, different given name" from true orphans.
SCOPE_COMPOSERS: dict[str, dict[str, str]] = {
    "stenhammar-wilhelm":      {"surname": "Stenhammar",      "given": "Wilhelm"},
    "peterson-berger-wilhelm": {"surname": "Peterson-Berger", "given": "Wilhelm"},
    "lindberg-oskar":          {"surname": "Lindberg",        "given": "Oskar"},
    "alfven-hugo":             {"surname": "Alfvén",          "given": "Hugo"},
}

# ---------------------------------------------------------------------------
# Filename parsing
# ---------------------------------------------------------------------------

# Standard pattern: Surname_Firstname-TitleSlug-(type)-(edition)-(SMH-M<id>).ext
# "Peterson-Berger" surname contains a hyphen, so we split on first underscore.
FILENAME_STANDARD_RE = re.compile(
    r"^(?P<surname>[^_]+)_(?P<firstname>[^-]+)-(?P<title_slug>.+?)"
    r"-\([^)]+\)-\([^)]*\)-\(SMH-M(?P<smh_id>\d+)\)\.(?P<ext>\w+)$"
)
# Lenient fallback: at minimum grab surname and firstname
FILENAME_LENIENT_RE = re.compile(r"^(?P<surname>[^_]+)_(?P<firstname>[^-]+)-(?P<rest>.+)$")


def parse_filename(filename: str | None) -> dict:
    """Extract surname, firstname, title_slug from a media filename."""
    if not filename:
        return {}
    m = FILENAME_STANDARD_RE.match(filename)
    if m:
        return m.groupdict()
    m = FILENAME_LENIENT_RE.match(filename)
    if m:
        d = m.groupdict()
        d["title_slug"] = d.pop("rest", "")
        d["smh_id"] = None
        d["ext"] = filename.rsplit(".", 1)[-1] if "." in filename else None
        return d
    # Could not parse at all; try to get at least the surname
    parts = filename.split("_", 1)
    return {"surname": parts[0], "firstname": None, "title_slug": None,
            "smh_id": None, "ext": None}


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    """Lowercase, replace underscores/hyphens with spaces, collapse whitespace."""
    return re.sub(r"\s+", " ", text.lower().replace("_", " ").replace("-", " ")).strip()


def normalize_title(s: str) -> str:
    """
    Canonicalise a title string for diacritic/punctuation-insensitive comparison:
      1. NFKD decompose then drop all combining characters (å→a, ö→o, ä→a, é→e …)
      2. Lowercase
      3. Replace every non-alphanumeric character with a single space
      4. Collapse and strip whitespace
    """
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return s.strip()


def _token_overlap(a: str, b: str) -> float:
    """Jaccard similarity on word-token sets."""
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


TITLE_MATCH_THRESHOLD = 0.25   # Jaccard; lower = more permissive


def title_matches(slug_text: str, work_title: str) -> tuple[bool, str]:
    """
    Return (matched, method) where method is one of:
      'strict'     — existing _normalise + containment/Jaccard logic
      'normalized' — diacritic-stripped substring match
      'compact'    — spaces-removed substring match (handles run-together words
                     like 'VarnattLenznacht' vs 'varnatt lenznacht')
      ''           — no match
    """
    # Pass 1: strict (preserves all previously correct matches unchanged)
    ns = _normalise(slug_text)
    nt = _normalise(work_title)
    if ns in nt or nt in ns:
        return True, "strict"
    if _token_overlap(ns, nt) >= TITLE_MATCH_THRESHOLD:
        return True, "strict"

    # Pass 2: diacritic/punctuation-stripped substring
    nn_slug = normalize_title(slug_text)
    nn_title = normalize_title(work_title)
    if nn_slug in nn_title or nn_title in nn_slug:
        return True, "normalized"

    # Pass 3: compact (remove all spaces) — handles run-together filename tokens
    if nn_slug.replace(" ", "") in nn_title.replace(" ", "") or \
       nn_title.replace(" ", "") in nn_slug.replace(" ", ""):
        return True, "compact"

    return False, ""


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_probe(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(f"Probe file not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


_NON_CRAWL = {"smh_media_probe", "smh_orphaned_media"}


def load_crawl_data() -> dict[str, list[dict]]:
    """Return {slug: [work_records]} for all smh_<slug>.json composer crawl files."""
    crawl: dict[str, list[dict]] = {}
    for fpath in sorted(glob.glob(str(DATA_DIR / "smh_*.json"))):
        stem = Path(fpath).stem  # e.g. "smh_stenhammar-wilhelm"
        if stem in _NON_CRAWL:
            continue
        slug = stem[len("smh_"):]  # e.g. "stenhammar-wilhelm"
        crawl[slug] = json.loads(Path(fpath).read_text(encoding="utf-8"))
    return crawl


def build_media_id_index(crawl: dict[str, list[dict]]) -> dict[str, tuple[str, str]]:
    """
    Build {media_id_str: (slug, smh_work_id)} from all crawled records.
    Allows O(1) lookup of whether a probe-found ID is already matched.
    """
    idx: dict[str, tuple[str, str]] = {}
    for slug, works in crawl.items():
        for work in works:
            for mf in work.get("media_files", []):
                mid = mf.get("media_id")
                if mid:
                    idx[str(mid)] = (slug, work.get("smh_work_id", ""))
    return idx


def build_scope_given_index() -> dict[str, str]:
    """
    Return {slug: normalize_title(given_name)} for each in-scope composer,
    so that given names extracted from filenames can be compared leniently.
    """
    return {slug: normalize_title(info["given"]) for slug, info in SCOPE_COMPOSERS.items()}


def build_surname_index(crawl: dict[str, list[dict]]) -> dict[str, str]:
    """
    Derive {normalised_surname: slug} from actual filenames in crawled records.
    Falls back to a heuristic for slugs with no media files (e.g. lindberg-oskar).
    """
    idx: dict[str, str] = {}
    for slug, works in crawl.items():
        found = False
        for work in works:
            for mf in work.get("media_files", []):
                fn = mf.get("filename", "") or ""
                parsed = parse_filename(fn)
                surname = parsed.get("surname", "")
                if surname:
                    idx[_normalise(surname)] = slug
                    found = True
        if not found:
            # Heuristic: slug is "lastname-firstname" or "last-last-firstname";
            # the firstname is the final hyphen-segment.
            parts = slug.split("-")
            if len(parts) >= 2:
                surname_parts = parts[:-1]  # drop firstname
                surname = " ".join(p.capitalize() for p in surname_parts)
                idx[_normalise(surname)] = slug

    log.info("Surname index: %s", {k: v for k, v in sorted(idx.items())})
    return idx


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify(
    probe_records: list[dict],
    crawl: dict[str, list[dict]],
    media_id_index: dict[str, tuple[str, str]],
    surname_index: dict[str, str],
    scope_given_index: dict[str, str],
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """
    Returns (matched, linked_elsewhere, different_composer_same_surname, orphaned).
    Only probe records with found=True are classified.
    """
    matched: list[dict] = []
    linked_elsewhere: list[dict] = []
    different_composer_same_surname: list[dict] = []
    orphaned: list[dict] = []

    # Pre-build per-slug title lookup: {slug: [(work_id, raw_title)]}
    # Store raw titles so both _normalise and normalize_title can be applied.
    title_lookup: dict[str, list[tuple[str, str]]] = {}
    for slug, works in crawl.items():
        title_lookup[slug] = [
            (w.get("smh_work_id", ""), w.get("title", "") or "")
            for w in works
            if w.get("title")
        ]

    for rec in probe_records:
        if not rec.get("found"):
            continue

        mid = str(rec["media_id"])
        filename = rec.get("filename") or ""

        # --- Bucket 1: already matched in crawl data ---
        if mid in media_id_index:
            slug, work_id = media_id_index[mid]
            matched.append({**rec, "matched_slug": slug, "matched_work_id": work_id})
            continue

        # --- Parse filename for composer + title ---
        parsed = parse_filename(filename)
        raw_surname = parsed.get("surname", "")
        title_slug = parsed.get("title_slug", "") or ""
        norm_surname = _normalise(raw_surname)

        # --- Is this composer in our crawl? ---
        slug = surname_index.get(norm_surname)
        if slug is None:
            orphaned.append({**rec, "reason": "composer_not_in_crawl",
                             "parsed_surname": raw_surname})
            continue

        # --- Title match (strict first, then normalized fallback) ---
        best_match_work_id = None
        best_match_method = ""
        for work_id, raw_title in title_lookup.get(slug, []):
            matched_flag, method = title_matches(title_slug, raw_title)
            if matched_flag:
                best_match_work_id = work_id
                best_match_method = method
                break

        if best_match_work_id is not None:
            linked_elsewhere.append({
                **rec,
                "matched_slug": slug,
                "matched_work_id": best_match_work_id,
                "reason": "title_matches_but_id_missing_from_record",
                "match_method": best_match_method,
            })
        else:
            # Check whether the surname matched but the given name belongs to a
            # different in-scope composer (e.g. Per Ulrik vs Wilhelm Stenhammar).
            raw_given = (parsed.get("firstname") or "").replace("_", " ")
            norm_given_filename = normalize_title(raw_given)
            norm_given_scope = scope_given_index.get(slug, "")

            if norm_given_filename and norm_given_scope and \
               norm_given_filename != norm_given_scope:
                scope_info = SCOPE_COMPOSERS.get(slug, {})
                different_composer_same_surname.append({
                    **rec,
                    "reason": "different_composer_same_surname",
                    "filename_composer": f"{raw_surname} {raw_given}".strip(),
                    "scope_composer": f"{scope_info.get('given', '')} {scope_info.get('surname', '')}".strip(),
                    "matched_slug": slug,
                })
            else:
                orphaned.append({
                    **rec,
                    "reason": "composer_known_but_no_title_match",
                    "matched_slug": slug,
                    "parsed_surname": raw_surname,
                    "parsed_title_slug": title_slug,
                })

    return matched, linked_elsewhere, different_composer_same_surname, orphaned


# ---------------------------------------------------------------------------
# Sanity report
# ---------------------------------------------------------------------------

def sanity_report(
    matched: list[dict],
    linked_elsewhere: list[dict],
    different_composer_same_surname: list[dict],
    orphaned: list[dict],
    crawl: dict[str, list[dict]],
    surname_index: dict[str, str],
) -> None:
    our_slugs = set(crawl.keys())

    def _our(records: list[dict]) -> list[dict]:
        return [r for r in records
                if surname_index.get(_normalise(parse_filename(r.get("filename","")).get("surname","")))
                in our_slugs]

    total = len(matched) + len(linked_elsewhere) + len(different_composer_same_surname) + len(orphaned)
    print()
    print("=== Match sanity report ===")
    print(f"Total probe-found files classified       : {total}")
    print(f"  matched                                : {len(matched)}")
    print(f"  linked_elsewhere                       : {len(linked_elsewhere)}")
    print(f"  different_composer_same_surname        : {len(different_composer_same_surname)}")
    print(f"  orphaned                               : {len(orphaned)}")
    print()

    our_linked = _our(linked_elsewhere)
    our_orphaned = [r for r in orphaned if r.get("matched_slug") in our_slugs]

    via_normalized = sum(1 for r in linked_elsewhere if r.get("match_method") in ("normalized", "compact"))
    print("For our crawled composers specifically:")
    print(f"  linked_elsewhere (missing editions)          : {len(our_linked)}")
    print(f"    of which promoted by normalized match      : {via_normalized}")
    print(f"  different_composer_same_surname              : {len(different_composer_same_surname)}")
    print(f"  orphaned (unlinked from any record)          : {len(our_orphaned)}")

    if our_linked:
        print()
        print("Linked-elsewhere files (sample, up to 10):")
        for r in our_linked[:10]:
            print(f"  ID {r['media_id']:5d} → {r['matched_slug']} / {r['matched_work_id']}")
            print(f"           file: {(r.get('filename') or '')[:70]}")

    if different_composer_same_surname:
        print()
        print("Different-composer-same-surname files (sample, up to 5):")
        for r in different_composer_same_surname[:5]:
            print(f"  ID {r['media_id']:5d}  filename_composer={r.get('filename_composer')}  scope_composer={r.get('scope_composer')}")
            print(f"           file: {(r.get('filename') or '')[:70]}")

    if our_orphaned:
        print()
        print("Orphaned files for our composers (sample, up to 10):")
        for r in our_orphaned[:10]:
            print(f"  ID {r['media_id']:5d}  slug={r.get('matched_slug')}  reason={r['reason']}")
            print(f"           file: {(r.get('filename') or '')[:70]}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    probe_path = DATA_DIR / "smh_media_probe.json"
    out_path = DATA_DIR / "smh_orphaned_media.json"

    probe_records = load_probe(probe_path)
    found_count = sum(1 for r in probe_records if r.get("found"))
    log.info("Loaded %d probe records (%d found)", len(probe_records), found_count)

    crawl = load_crawl_data()
    log.info("Loaded crawl data for slugs: %s", list(crawl.keys()))

    media_id_index = build_media_id_index(crawl)
    log.info("Media ID index: %d IDs from crawled records", len(media_id_index))

    surname_index = build_surname_index(crawl)
    scope_given_index = build_scope_given_index()

    matched, linked_elsewhere, different_composer_same_surname, orphaned = classify(
        probe_records, crawl, media_id_index, surname_index, scope_given_index
    )

    output = {
        "matched": matched,
        "linked_elsewhere": linked_elsewhere,
        "different_composer_same_surname": different_composer_same_surname,
        "orphaned": orphaned,
    }
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("Wrote classification results to %s", out_path)

    sanity_report(matched, linked_elsewhere, different_composer_same_surname, orphaned, crawl, surname_index)


if __name__ == "__main__":
    main()
