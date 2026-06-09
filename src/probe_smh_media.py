"""
Probe SMH's downloadMedia endpoint to discover all available media files
by sequential ID enumeration.

Usage:
    python src/probe_smh_media.py [--start N] [--end N]

Defaults: --start 1, --end 5000.

NOTE: The SMH server always returns HTTP 200, regardless of whether a file
exists. Found files have a 'Content-Disposition: attachment; filename=...'
header; missing IDs return text/html with no such header. This script
records both cases. 'found' means Content-Disposition was present.

Results are written incrementally to data/smh_media_probe.json every 50
probes so a crash doesn't lose work. If that file already exists, already-
probed IDs are skipped (resumable).
"""

import argparse
import json
import logging
import re
import time
from pathlib import Path

import requests

USER_AGENT = (
    "ChoralPieceFinder/0.1 (university IR lab; axel.riben2@gmail.com; "
    "https://github.com/axelriben/choral-piece-finder) python-requests/2.x"
)
BASE_URL = "https://www.swedishmusicalheritage.com"
PROBE_URL = f"{BASE_URL}/downloadMedia.php"
REQUEST_DELAY = 0.5          # 2 requests per second
SAVE_EVERY = 50
RETRY_WAIT = 5

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

CONTENT_DISP_RE = re.compile(r'filename=["\']?([^"\';\r\n]+)["\']?', re.IGNORECASE)


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def _parse_filename(content_disposition: str) -> str | None:
    m = CONTENT_DISP_RE.search(content_disposition)
    return m.group(1).strip() if m else None


def probe_one(session: requests.Session, media_id: int) -> dict:
    url = f"{PROBE_URL}?m={media_id}"
    for attempt in range(2):
        try:
            r = session.head(url, timeout=15, allow_redirects=True)
            cd = r.headers.get("content-disposition", "")
            ct = r.headers.get("content-type", "")
            cl = r.headers.get("content-length")
            found = bool(cd and "filename=" in cd.lower())
            filename = _parse_filename(cd) if found else None
            return {
                "media_id": media_id,
                "http_status": r.status_code,
                "found": found,
                "filename": filename,
                "content_type": ct.split(";")[0].strip() if ct else None,
                "content_length": int(cl) if cl and cl.isdigit() else None,
                "error": None,
            }
        except requests.RequestException as exc:
            if attempt == 0:
                log.warning("ID %d: %s — retrying in %ds", media_id, exc, RETRY_WAIT)
                time.sleep(RETRY_WAIT)
            else:
                log.warning("ID %d: %s — giving up", media_id, exc)
                return {
                    "media_id": media_id,
                    "http_status": None,
                    "found": False,
                    "filename": None,
                    "content_type": None,
                    "content_length": None,
                    "error": str(exc),
                }


def load_existing(path: Path) -> dict[int, dict]:
    if not path.exists():
        return {}
    existing = json.loads(path.read_text(encoding="utf-8"))
    return {r["media_id"]: r for r in existing}


def save(path: Path, results: dict[int, dict]) -> None:
    records = sorted(results.values(), key=lambda r: r["media_id"])
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def sanity_report(results: dict[int, dict]) -> None:
    records = sorted(results.values(), key=lambda r: r["media_id"])
    total = len(records)
    found = sum(1 for r in records if r["found"])
    not_found = sum(1 for r in records if not r["found"] and not r["error"])
    errors = sum(1 for r in records if r["error"])

    print()
    print("=== Probe sanity report ===")
    print(f"Total IDs probed : {total}")
    print(f"Found (files)    : {found}")
    print(f"Not found (gaps) : {not_found}")
    print(f"Errors           : {errors}")
    print()

    # Histogram in 500-ID buckets
    if not records:
        return
    max_id = max(r["media_id"] for r in records)
    bucket_size = 500
    print("ID density (files found per 500-ID bucket):")
    for bucket_start in range(0, max_id + bucket_size, bucket_size):
        bucket_end = bucket_start + bucket_size - 1
        bucket_found = sum(
            1 for r in records
            if bucket_start <= r["media_id"] <= bucket_end and r["found"]
        )
        bucket_total = sum(
            1 for r in records
            if bucket_start <= r["media_id"] <= bucket_end
        )
        if bucket_total == 0:
            continue
        bar = "█" * bucket_found
        print(f"  {bucket_start:5d}–{bucket_end:5d}: {bucket_found:4d} / {bucket_total}  {bar}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe SMH downloadMedia endpoint")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=5000)
    args = parser.parse_args()

    out_path = Path(__file__).parent.parent / "data" / "smh_media_probe.json"

    results = load_existing(out_path)
    already = len(results)
    log.info("Loaded %d existing probe results from %s", already, out_path)

    to_probe = [i for i in range(args.start, args.end + 1) if i not in results]
    log.info("Will probe %d IDs (%d–%d, skipping %d already done)",
             len(to_probe), args.start, args.end, already)

    session = make_session()
    new_since_save = 0

    for i, media_id in enumerate(to_probe, 1):
        result = probe_one(session, media_id)
        results[media_id] = result
        new_since_save += 1

        if result["found"]:
            log.info("[%d/%d] ID %d: FOUND — %s",
                     i, len(to_probe), media_id, (result["filename"] or "")[:60])
        else:
            log.debug("[%d/%d] ID %d: not found", i, len(to_probe), media_id)

        if new_since_save >= SAVE_EVERY:
            save(out_path, results)
            log.info("Saved %d total results to %s", len(results), out_path)
            new_since_save = 0

        time.sleep(REQUEST_DELAY)

    save(out_path, results)
    log.info("Final save: %d total results written to %s", len(results), out_path)

    sanity_report(results)


if __name__ == "__main__":
    main()
