"""
Crawl a single composer's works from the Choral Public Domain Library (CPDL).

Usage:
    python src/crawl_cpdl.py "Giovanni_Pierluigi_da_Palestrina"

The argument is the composer's CPDL page title (underscores or spaces). The
script uses linkshere to enumerate all pages that link to the composer, keeps
only unambiguous work pages (title ends with "(Composer Name)"), fetches each
page's wikitext via action=parse, and writes the results to
data/cpdl_<last-name>.json.

Cloudflare bypass is handled by cpdl_session.CPDLSession, which launches a
real Chrome window once (or whenever the cf_clearance cookie ages past the
configured lifetime) and reuses the authenticated session for all subsequent
requests.  Cookie expiry mid-crawl is no longer a problem.
"""

import json
import logging
import re
import sys
import time
from pathlib import Path

from cpdl_session import CPDLSession, get_cpdl_session

API_BASE = "https://www2.cpdl.org/wiki/api.php"
WIKI_BASE = "https://www2.cpdl.org/wiki/index.php"
REQUEST_DELAY = 2.5

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CPDL API helpers
# ---------------------------------------------------------------------------

def api_get(session: CPDLSession, params: dict) -> dict:
    """Send a CPDL MediaWiki API request and return the decoded JSON.

    Raises RuntimeError on 403 (Cloudflare not resolved) or if the site
    returns HTML (maintenance mode) instead of JSON.
    """
    params["format"] = "json"
    resp = session.get(API_BASE, params=params, timeout=30)
    if resp.status_code == 403:
        raise RuntimeError(
            "Still getting 403 from CPDL — Cloudflare challenge may not have resolved."
        )
    resp.raise_for_status()
    ct = resp.headers.get("content-type", "")
    if "text/html" in ct:
        snippet = resp.text[:200].replace("\n", " ")
        raise RuntimeError(
            f"CPDL returned HTML instead of JSON — site may be down for maintenance.\n"
            f"Response snippet: {snippet}"
        )
    return resp.json()


def get_linked_pages(session: CPDLSession, composer_title: str) -> list[str]:
    """Return all main-namespace, non-redirect page titles linking to the composer page.

    Follows lhcontinue pagination tokens until exhausted.
    """
    titles: list[str] = []
    params = {
        "action": "query",
        "prop": "linkshere",
        "titles": composer_title,
        "lhlimit": "500",
        "lhnamespace": "0",
        "lhshow": "!redirect",
    }
    while True:
        data = api_get(session, params)
        pages = data["query"]["pages"]
        page = next(iter(pages.values()))
        for link in page.get("linkshere", []):
            titles.append(link["title"])
        if "continue" not in data:
            break
        params.update(data["continue"])
        time.sleep(REQUEST_DELAY)
    return titles


def get_wikitext(session: CPDLSession, page_title: str) -> str | None:
    """Fetch raw wikitext for a single page via action=parse. Returns None on failure."""
    params = {
        "action": "parse",
        "page": page_title,
        "prop": "wikitext",
    }
    data = api_get(session, params)
    try:
        return data["parse"]["wikitext"]["*"]
    except (KeyError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Wikitext parsing
# ---------------------------------------------------------------------------

def _field(wikitext: str, key: str) -> str | None:
    """Extract a single |key=value field from a wikitext template."""
    pattern = rf"\|\s*{re.escape(key)}\s*=\s*([^\|}}]+)"
    m = re.search(pattern, wikitext, re.IGNORECASE)
    if m:
        val = m.group(1).strip()
        val = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]", r"\1", val)
        val = re.sub(r"\{\{[^}]*\}\}", "", val)
        val = val.strip()
        return val if val else None
    return None


def _field_list(wikitext: str, key: str) -> list[str]:
    raw = _field(wikitext, key)
    if not raw:
        return []
    return [p.strip() for p in re.split(r"[;,]", raw) if p.strip()]


def _guess_format(url: str, label: str) -> str | None:
    u, lb = url.lower(), label.lower()
    if u.endswith(".pdf") or "pdf" in lb:
        return "PDF"
    if u.endswith((".mxl", ".xml")) or "musicxml" in lb or "mxl" in lb:
        return "MusicXML"
    if u.endswith((".mid", ".midi")) or "midi" in lb:
        return "MIDI"
    if u.endswith(".sib") or "sibelius" in lb:
        return "Sibelius"
    if u.endswith(".mus") or "finale" in lb:
        return "Finale"
    if u.endswith(".ly") or "lilypond" in lb:
        return "LilyPond"
    return None


def _score_urls(wikitext: str) -> list[dict]:
    urls: list[dict] = []
    seen: set[str] = set()
    for m in re.finditer(r"\[((https?://[^\s\]]+))\s*([^\]]*)\]", wikitext):
        url, label = m.group(1), m.group(3).strip()
        fmt = _guess_format(url, label)
        if fmt and url not in seen:
            urls.append({"format": fmt, "url": url})
            seen.add(url)
    for m in re.finditer(
        r"(https?://\S+\.(?:pdf|xml|mxl|midi?|sib|mus|ly))", wikitext, re.IGNORECASE
    ):
        url = m.group(1).rstrip(".,;)")
        fmt = _guess_format(url, "")
        if fmt and url not in seen:
            urls.append({"format": fmt, "url": url})
            seen.add(url)
    return urls


def parse_work(page_title: str, wikitext: str, cpdl_url: str) -> dict:
    """Parse raw wikitext into a structured work record."""
    def f(*keys):
        for k in keys:
            v = _field(wikitext, k)
            if v:
                return v
        return None

    def fl(*keys):
        for k in keys:
            v = _field_list(wikitext, k)
            if v:
                return v
        return []

    return {
        "title": f("title", "Title") or page_title,
        "composer": f("composer", "Composer"),
        "voicing": f("voicing", "Voicing", "scoring", "Scoring"),
        "number_of_voices": f("voices", "Voices", "numberofvoices"),
        "languages": fl("language", "Language", "languages") or None,
        "genre": f("genre", "Genre", "type", "Type"),
        "year_composition": f("year", "Year", "composed", "Composed"),
        "text_author": f("textauthor", "TextAuthor", "text", "Text", "poet", "Poet"),
        "incipit": f("incipit", "Incipit", "firstline", "FirstLine"),
        "score_urls": _score_urls(wikitext),
        "cpdl_page_url": cpdl_url,
        "raw_wikitext": wikitext,
    }


def page_url(page_title: str) -> str:
    return f"{WIKI_BASE}/{page_title.replace(' ', '_')}"


def slug_from_composer(composer_title: str) -> str:
    """'Giovanni_Pierluigi_da_Palestrina' → 'palestrina'"""
    name = composer_title.replace("_", " ").strip()
    return name.split()[-1].lower()


def composer_display_name(composer_title: str) -> str:
    """'Giovanni_Pierluigi_da_Palestrina' → 'Giovanni Pierluigi da Palestrina'"""
    return composer_title.replace("_", " ").strip()


# ---------------------------------------------------------------------------
# Main crawl loop
# ---------------------------------------------------------------------------

def crawl(composer_title: str) -> list[dict]:
    """Crawl all disambiguated work pages for *composer_title* and return records.

    Cookie refresh is now automatic: CPDLSession re-runs Playwright whenever
    the cf_clearance cookie ages past cookie_lifetime_minutes (default 20 min),
    so long crawls no longer suffer 403s in their tail.
    """
    session = get_cpdl_session()

    display_name = composer_display_name(composer_title)
    suffix = f"({display_name})"

    log.info("Fetching pages that link to: %s", composer_title)
    all_titles = get_linked_pages(session, composer_title)
    log.info("Found %d linking pages total", len(all_titles))

    work_titles = [t for t in all_titles if t.endswith(suffix)]
    skipped = len(all_titles) - len(work_titles)
    log.info(
        "Keeping %d disambiguated work pages; skipping %d without parenthetical (v1.1 TODO)",
        len(work_titles),
        skipped,
    )

    works: list[dict] = []
    for i, title in enumerate(work_titles, 1):
        url = page_url(title)
        log.info("[%d/%d] %s", i, len(work_titles), title)
        try:
            time.sleep(REQUEST_DELAY)
            wikitext = get_wikitext(session, title)
            if wikitext is None:
                log.warning("No wikitext for %s — skipping", title)
                continue
            works.append(parse_work(title, wikitext, url))
        except Exception as exc:
            log.warning("Failed to process %s: %s", title, exc)
    return works


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} <composer-page-title>")
        print('Example: python src/crawl_cpdl.py "Giovanni_Pierluigi_da_Palestrina"')
        sys.exit(1)

    composer_title = sys.argv[1]
    slug = slug_from_composer(composer_title)

    out_dir = Path(__file__).parent.parent / "data"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"cpdl_{slug}.json"

    works = crawl(composer_title)
    log.info("Writing %d works to %s", len(works), out_path)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(works, fh, ensure_ascii=False, indent=2)
    log.info("Done.")


if __name__ == "__main__":
    main()
