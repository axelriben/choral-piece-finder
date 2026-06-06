"""
Crawl a single composer's works from Swedish Musical Heritage (SMH).

Usage:
    python src/crawl_smh.py stenhammar-wilhelm

The argument is the composer's SMH slug (the path component after /composers/).
SMH is plain server-rendered HTML with no API and no Cloudflare protection, so
this crawler uses requests + BeautifulSoup directly. It fetches the composer's
index page, collects all work links (href matching
/composers/<slug>/SMH-W<id>-<title-slug>), then fetches and parses each work
page. Output is written to data/smh_<slug>.json, and a sanity report is
printed to stdout.
"""

import json
import logging
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup, Tag

BASE_URL = "https://www.swedishmusicalheritage.com"
SWEDISH_DOMAIN = "www.levandemusikarv.se"
USER_AGENT = (
    "ChoralPieceFinder/0.1 (university IR lab; axel.riben2@gmail.com; "
    "https://github.com/axelriben/choral-piece-finder) python-requests/2.x"
)
REQUEST_DELAY = 1.0

WORK_URL_RE = re.compile(r"/composers/([\w-]+)/(SMH-W(\d+)-[\w-]*)")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# Known info-list / section field labels we explicitly map. Anything else
# encountered is reported in the sanity check so the schema can be extended.
KNOWN_INFO_LABELS = {
    "year of composition",
    "work category",
    "instrumentation",
    "soloröster/kör",
    "solo voices/choir",
    "text author",
    "duration",
    "detailed duration",
    "first performed",
    "location autograph",
    "possible call no. and autograph comment",
    "dedication",
    "arrangement/revision",
}
KNOWN_SECTION_LABELS = {
    "instrumentation",
    "solo voices/choir",
    "soloröster/kör",
    "examples of printed editions",
    "description of work",
    "literature",
    "links",
    "location for score and part material",
}


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def fetch(session: requests.Session, url: str) -> requests.Response:
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# Composer index page
# ---------------------------------------------------------------------------

def get_work_urls(session: requests.Session, slug: str) -> list[str]:
    """Return absolute URLs for all work pages listed on a composer's index page."""
    index_url = f"{BASE_URL}/composers/{slug}/"
    log.info("Fetching composer index: %s", index_url)
    resp = fetch(session, index_url)
    soup = BeautifulSoup(resp.text, "html.parser")

    seen = set()
    urls = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = WORK_URL_RE.search(href)
        if m and m.group(1) == slug and href not in seen:
            seen.add(href)
            urls.append(urljoin(BASE_URL, href))
    log.info("Found %d work links", len(urls))
    return urls


# ---------------------------------------------------------------------------
# Field-level parsing helpers
# ---------------------------------------------------------------------------

def _text(tag: Tag | None) -> str | None:
    if tag is None:
        return None
    return tag.get_text(" ", strip=True) or None


BRACKET_RE = re.compile(r"[\(\[]([^\(\)\[\]]+)[\)\]]")


def _parse_title(raw_heading: str) -> tuple[str, list[str]]:
    """
    Split a heading like 'Stemning (Stämning) [Ambience] [Båten gungar...]'
    into a primary title and a list of alternates pulled from (..)/[..] groups.
    Best-effort: SMH headings mix translations, opus context, and alt spellings
    in brackets/parens with no consistent convention.
    """
    alternates = [m.strip() for m in BRACKET_RE.findall(raw_heading) if m.strip()]
    primary = BRACKET_RE.sub("", raw_heading)
    primary = re.sub(r"\s+", " ", primary).strip(" .")
    return primary or raw_heading.strip(), alternates


AUTHOR_DATES_RE = re.compile(r"\((\d{4}[-–]\d{0,4})\)")


def _parse_text_author(raw: str | None) -> tuple[str | None, str | None]:
    """Split 'Henrik Ibsen (1828-1906). The libretto...' into (author, dates)."""
    if not raw:
        return None, None
    m = AUTHOR_DATES_RE.search(raw)
    dates = m.group(1) if m else None
    return raw, dates


DURATION_RANGE_RE = re.compile(r"(\d+)\s*[-–]\s*(\d+)\s*min")
DURATION_SINGLE_RE = re.compile(r"(\d+)\s*min")


def _parse_duration(raw: str | None) -> tuple[int | None, int | None]:
    """Parse '3 min' / 'Approx.  30-40 min' into (min_sec, max_sec)."""
    if not raw:
        return None, None
    m = DURATION_RANGE_RE.search(raw)
    if m:
        return int(m.group(1)) * 60, int(m.group(2)) * 60
    m = DURATION_SINGLE_RE.search(raw)
    if m:
        v = int(m.group(1)) * 60
        return v, v
    return None, None


def _info_list(soup: BeautifulSoup) -> dict[str, str]:
    """Parse <ul class="info-list"><li>Label: value</li></ul> into {lower_label: value}."""
    result: dict[str, str] = {}
    for li in soup.select("ul.info-list li"):
        text = li.get_text(" ", strip=True)
        if ":" in text:
            label, _, value = text.partition(":")
            result[label.strip().lower()] = value.strip()
    return result


def _subheading_sections(soup: BeautifulSoup) -> dict[str, str]:
    """Parse <h3 class="subheading"> + following <p class="readmore"> into {lower_heading: text}."""
    result: dict[str, str] = {}
    for h3 in soup.select("h3.subheading"):
        key = h3.get_text(strip=True).lower()
        parts = []
        node = h3.find_next_sibling()
        while node and not (isinstance(node, Tag) and node.name == "h3"):
            if isinstance(node, Tag) and "readmore" in (node.get("class") or []):
                parts.append(node.get_text("\n", strip=True))
            node = node.find_next_sibling()
        if parts:
            result[key] = "\n\n".join(parts)
    return result


def _libretto_text(soup: BeautifulSoup) -> str | None:
    """
    Parse the 'Libretto/text' section: an <h2><strong>Libretto/text</strong></h2>
    followed by a <div class="format-field readmore"> containing the text.
    """
    for h2 in soup.find_all("h2"):
        strong = h2.find("strong")
        if strong and strong.get_text(strip=True).lower().startswith("libretto"):
            node = h2.find_next_sibling()
            while node:
                if isinstance(node, Tag) and "format-field" in (node.get("class") or []):
                    return node.get_text("\n", strip=True) or None
                node = node.find_next_sibling()
    return None


def _media_files(soup: BeautifulSoup) -> list[dict]:
    """Extract /downloadMedia.php links: {media_id, filename, format, url}."""
    files = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "downloadMedia.php" not in href:
            continue
        params = parse_qs(urlparse(href).query)
        media_id = params.get("m", [None])[0]
        filename = params.get("f", [None])[0]
        fmt = None
        if filename and "." in filename:
            fmt = filename.rsplit(".", 1)[-1].upper()
        files.append({
            "media_id": media_id,
            "filename": filename,
            "format": fmt,
            "url": urljoin(BASE_URL, href),
        })
    return files


def _linked_editions(printed_editions_text: str | None) -> list[str]:
    """
    Best-effort split of the free-text 'Examples of printed editions' section
    into individual edition mentions (one per line/entry).
    """
    if not printed_editions_text:
        return []
    entries = [e.strip() for e in printed_editions_text.split("\n") if e.strip()]
    return entries


# ---------------------------------------------------------------------------
# Work page parsing
# ---------------------------------------------------------------------------

def parse_work_page(url: str, html: str, unknown_labels: set[str]) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    article = soup.find("article", id="main-content") or soup.find("main") or soup

    title_tag = article.find("h1")
    raw_title = _text(title_tag) or ""
    title, title_alternates = _parse_title(raw_title)

    composer_tag = article.find("h2")
    composer = _text(composer_tag)

    info = _info_list(article)
    sections = _subheading_sections(article)

    # Track any field labels we haven't explicitly mapped, for the sanity report
    for label in info:
        if label not in KNOWN_INFO_LABELS:
            unknown_labels.add(f"info-list: {label}")
    for label in sections:
        if label not in KNOWN_SECTION_LABELS:
            unknown_labels.add(f"subheading: {label}")

    raw_author = info.get("text author")
    text_author, text_author_dates = _parse_text_author(raw_author)

    raw_duration = info.get("duration")
    duration_min_sec, duration_max_sec = _parse_duration(raw_duration)

    instrumentation = info.get("instrumentation") or sections.get("instrumentation")
    solo_voices_choir = (
        info.get("soloröster/kör")
        or info.get("solo voices/choir")
        or sections.get("solo voices/choir")
        or sections.get("soloröster/kör")
    )

    printed_editions_text = sections.get("examples of printed editions")

    m = WORK_URL_RE.search(url)
    smh_work_id = f"W{m.group(3)}" if m else None

    return {
        "smh_work_id": smh_work_id,
        "smh_url": url,
        "smh_url_swedish": url.replace(urlparse(url).netloc, SWEDISH_DOMAIN),
        "title": title,
        "title_alternates": title_alternates,
        "composer": composer,
        "year_composition": info.get("year of composition"),
        "work_category": info.get("work category"),
        "instrumentation": instrumentation,
        "solo_voices_choir": solo_voices_choir,
        "text_author": text_author,
        "text_author_dates": text_author_dates,
        "duration_text": raw_duration,
        "duration_min_sec": duration_min_sec,
        "duration_max_sec": duration_max_sec,
        "description": sections.get("description of work"),
        "libretto_or_text": _libretto_text(article),
        # Download links live in a <div class="media-list"> that sits
        # *outside* <article id="main-content">, so search the whole page.
        "media_files": _media_files(soup),
        "linked_editions": _linked_editions(printed_editions_text),
        "raw_html": html,
    }


# ---------------------------------------------------------------------------
# Main crawl loop
# ---------------------------------------------------------------------------

def crawl(slug: str) -> tuple[list[dict], set[str]]:
    session = make_session()
    work_urls = get_work_urls(session, slug)
    unknown_labels: set[str] = set()

    works = []
    for i, url in enumerate(work_urls, 1):
        log.info("[%d/%d] %s", i, len(work_urls), url)
        try:
            time.sleep(REQUEST_DELAY)
            resp = fetch(session, url)
            works.append(parse_work_page(url, resp.text, unknown_labels))
        except Exception as exc:
            log.warning("Failed to process %s: %s", url, exc)
    return works, unknown_labels


def _pct(n: int, total: int) -> int:
    return 100 * n // total if total else 0


def print_sanity_report(works: list[dict], unknown_labels: set[str]) -> None:
    total = len(works)

    def has(field):
        return sum(1 for w in works if w.get(field))

    print()
    print("=== Sanity check ===")
    print(f"Total works processed     : {total}")
    print()
    for field in (
        "title", "year_composition", "work_category", "instrumentation",
        "solo_voices_choir", "text_author", "duration_text", "description",
        "libretto_or_text", "linked_editions",
    ):
        n = has(field)
        print(f"  {field:<20}: {n:>4}  ({_pct(n, total)}%)")

    n_media = sum(1 for w in works if w.get("media_files"))
    print(f"  {'≥1 media file':<20}: {n_media:>4}  ({_pct(n_media, total)}%)")

    n_dur_parsed = sum(1 for w in works if w.get("duration_min_sec") is not None)
    print(f"  {'duration parsed':<20}: {n_dur_parsed:>4}  ({_pct(n_dur_parsed, total)}%)")

    print()
    if unknown_labels:
        print("Unknown field labels encountered (consider extending the schema):")
        for lbl in sorted(unknown_labels):
            print(f"  - {lbl}")
    else:
        print("No unknown field labels encountered.")


def main() -> None:
    if len(sys.argv) != 2:
        print(f"Usage: python {sys.argv[0]} <smh-composer-slug>")
        print("Example: python src/crawl_smh.py stenhammar-wilhelm")
        sys.exit(1)

    slug = sys.argv[1]

    out_dir = Path(__file__).parent.parent / "data"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"smh_{slug}.json"

    works, unknown_labels = crawl(slug)
    log.info("Writing %d works to %s", len(works), out_path)
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(works, fh, ensure_ascii=False, indent=2)
    log.info("Done.")

    print_sanity_report(works, unknown_labels)


if __name__ == "__main__":
    main()
