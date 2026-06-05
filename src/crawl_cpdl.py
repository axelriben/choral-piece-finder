"""
Crawl a single composer's works from the Choral Public Domain Library (CPDL).

Usage:
    python src/crawl_cpdl.py "Giovanni_Pierluigi_da_Palestrina"

The argument is the composer's CPDL page title (underscores or spaces). The
script uses linkshere to enumerate all pages that link to the composer, keeps
only unambiguous work pages (title ends with "(Composer Name)"), fetches each
page's wikitext via action=parse, and writes the results to
data/cpdl_<last-name>.json. A headless Chrome window appears briefly to pass
Cloudflare's bot challenge before API calls begin.
"""

import json
import logging
import re
import sys
import time
from pathlib import Path

from curl_cffi import requests as cffi_requests
from playwright.sync_api import sync_playwright

API_BASE = "https://www2.cpdl.org/wiki/api.php"
WIKI_BASE = "https://www2.cpdl.org/wiki/index.php"
USER_AGENT = "ChoralPieceFinder/0.1 (university IR lab; axel.riben.3208@student.uu.se; https://github.com/axelriben/choral-piece-finder) python-requests/2.x"
REQUEST_DELAY = 2.5

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cloudflare bypass: launch real Chromium, solve the JS challenge, grab cookies
# ---------------------------------------------------------------------------

def get_cf_cookies() -> list[dict]:
    """
    Navigate to CPDL with a real browser, wait for the Cloudflare challenge to
    resolve, and return the cookies needed for subsequent API requests.

    Uses the system Chrome installation (channel='chrome') with the webdriver
    flag masked so Cloudflare cannot trivially detect automation.
    """
    log.info("Launching browser to pass Cloudflare challenge (one-time)…")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            channel="chrome",
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(user_agent=USER_AGENT)
        # Hide the navigator.webdriver flag that Cloudflare checks
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        page = ctx.new_page()

        page.goto("https://www.cpdl.org/wiki/index.php/Main_Page", timeout=60_000)

        # Poll until the Cloudflare interstitial ("Just a moment…" / "Vänta…") is gone
        CF_INTERSTITIALS = ("just a moment", "vänta")
        for _ in range(30):
            title = page.title().lower()
            if not any(s in title for s in CF_INTERSTITIALS):
                break
            log.info("  waiting for Cloudflare challenge… (%s)", page.title())
            time.sleep(2)
        else:
            log.warning("Cloudflare challenge may not have resolved; proceeding anyway")

        cookies = ctx.cookies()
        browser.close()
    log.info("Browser cookies obtained (%d cookies).", len(cookies))
    return cookies


def make_session(cookies: list[dict]) -> cffi_requests.Session:
    """Build a curl_cffi session pre-loaded with the Cloudflare cookies."""
    s = cffi_requests.Session(impersonate="chrome")
    s.headers["User-Agent"] = USER_AGENT
    s.headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
    s.headers["Accept-Language"] = "en-US,en;q=0.9"
    # Convert Playwright cookie dicts to a simple name→value mapping
    for ck in cookies:
        s.cookies.set(ck["name"], ck["value"], domain=ck.get("domain", ""))
    return s


# ---------------------------------------------------------------------------
# CPDL API helpers
# ---------------------------------------------------------------------------

def api_get(session: cffi_requests.Session, params: dict) -> dict:
    params["format"] = "json"
    resp = session.get(API_BASE, params=params, timeout=30)
    if resp.status_code == 403:
        raise RuntimeError(
            "Still getting 403 from CPDL — Cloudflare challenge may not have resolved."
        )
    resp.raise_for_status()
    # Detect CPDL's own maintenance/emergency page (returns text/html instead of JSON)
    ct = resp.headers.get("content-type", "")
    if "text/html" in ct:
        snippet = resp.text[:200].replace("\n", " ")
        raise RuntimeError(
            f"CPDL returned HTML instead of JSON — site may be down for maintenance.\n"
            f"Response snippet: {snippet}"
        )
    return resp.json()


def get_linked_pages(session: cffi_requests.Session, composer_title: str) -> list[str]:
    """
    Return all main-namespace, non-redirect page titles that link to the
    composer's page, following lhcontinue pagination tokens.
    """
    titles = []
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


def get_wikitext(session: cffi_requests.Session, page_title: str) -> str | None:
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
    urls = []
    seen = set()
    # Bracketed external links: [http://... label]
    for m in re.finditer(r"\[((https?://[^\s\]]+))\s*([^\]]*)\]", wikitext):
        url, label = m.group(1), m.group(3).strip()
        fmt = _guess_format(url, label)
        if fmt and url not in seen:
            urls.append({"format": fmt, "url": url})
            seen.add(url)
    # Bare URLs ending in known score extensions
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
    cookies = get_cf_cookies()
    session = make_session(cookies)

    display_name = composer_display_name(composer_title)
    suffix = f"({display_name})"

    log.info("Fetching pages that link to: %s", composer_title)
    all_titles = get_linked_pages(session, composer_title)
    log.info("Found %d linking pages total", len(all_titles))

    work_titles = [t for t in all_titles if t.endswith(suffix)]
    skipped = len(all_titles) - len(work_titles)
    log.info(
        "Keeping %d disambiguated work pages; skipping %d without parenthetical (v1.1)",
        len(work_titles), skipped,
    )

    # Limitation: the cf_clearance cookie obtained at startup has a finite
    # lifetime (empirically ~15 minutes). On long crawls (200+ works at 2.5 s/req
    # ≈ 8+ minutes) the cookie can expire mid-crawl, causing the tail of requests
    # to return 403s and be silently dropped as warnings.
    # TODO (v1.1): refresh the Cloudflare cookie periodically — either on a fixed
    # interval (e.g. every 15 minutes) or on first 403 detection — by re-running
    # get_cf_cookies() and rebuilding the session with make_session().
    works = []
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
